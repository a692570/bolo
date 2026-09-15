#!/usr/bin/env python3
"""Runner for the Bolo cleanup-prompt eval. Live calls, JSONL, leaderboard.

Calls Bolo's production LLM endpoint exactly as src/main.rs does (same model,
temperature 0, enable_thinking false, cleanup_max_tokens scaling), scores each
(candidate, pair) against the builtin corpus, writes one JSONL line per result
under results/, and prints a leaderboard: overall exact-match rate, token F1,
and per-failure-class breakdown.

Usage:
  python3 program.py [--candidates a,b] [--source builtin] [--limit N]
                      [--concurrency 4] [--out results/]

Requires TELNYX_API_KEY in the environment; falls back to reading
~/.claude/.env. Refuses to run live calls without a key. The key is never
printed, logged, or written to results.
"""

import argparse
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from candidates import CANDIDATES, candidate_names, get_candidate  # noqa: E402
from corpus import CLASSES, CORPUS  # noqa: E402
from metrics import exact_match, exact_rate, mean_f1, token_f1  # noqa: E402

# Production facts, mirrored from src/main.rs.
ENDPOINT = "https://api.telnyx.com/v2/ai/chat/completions"
MODEL = "Qwen/Qwen3-235B-A22B"
REQUEST_TIMEOUT_S = 60
MAX_TRIES = 3
MAX_CONCURRENCY = 4
ENV_FILE = os.path.expanduser("~/.claude/.env")

# Mirrors CLEANUP_ARTIFACT_LABELS in src/main.rs.
ARTIFACT_LABELS = ("CLEAN:", "Clean:", "clean:", "TRANSCRIPT:", "Transcript:", "transcript:")
_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z]*\r?\n?")


def cleanup_max_tokens(transcript):
    # type: (str) -> int
    """Mirror of cleanup_max_tokens in src/main.rs: words*12 clamped to
    [1200, 3000], with an empty transcript counting as one word."""
    word_count = max(1, len(transcript.split()))
    return min(3000, max(1200, word_count * 12))


def strip_cleanup_artifacts(text):
    # type: (str) -> str
    """Mirror of strip_cleanup_artifacts in src/main.rs: strip code fences,
    CLEAN:/TRANSCRIPT: labels, and one wrapping double-quote pair, so the
    score reflects what production would actually insert."""
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_OPEN_RE.sub("", text, count=1).replace("```", "").strip()
    for label in ARTIFACT_LABELS:
        if text.startswith(label):
            text = text[len(label):].strip()
            break
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    return text


def load_api_key():
    # type: () -> str
    """TELNYX_API_KEY from the environment, else parsed from ENV_FILE
    (~/.claude/.env, KEY=VALUE and export KEY=VALUE lines). Returns None when
    unavailable. The key is never printed by this module."""
    key = os.environ.get("TELNYX_API_KEY", "").strip()
    if key:
        return key
    if not os.path.isfile(ENV_FILE):
        return None
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                name, sep, value = line.partition("=")
                if sep and name.strip() == "TELNYX_API_KEY":
                    value = value.strip().strip('"').strip("'")
                    if value:
                        return value
    except OSError:
        return None
    return None


def _chat_once(system_prompt, transcript, api_key, timeout_s):
    # type: (str, str, str, int) -> tuple
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript},
        ],
        "max_tokens": cleanup_max_tokens(transcript),
        "temperature": 0,
        "enable_thinking": False,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data["choices"][0]["message"].get("content")
    if not content:
        return None, "empty content in response"
    return content, None


def call_cleanup_llm(system_prompt, transcript, api_key, timeout_s=REQUEST_TIMEOUT_S):
    # type: (str, str, str, int) -> tuple
    """One (system, transcript) call with retry on 429/5xx/network errors:
    3 tries, exponential backoff. Returns (output, error); output is None on
    error. Error strings never include the API key."""
    last_error = "unknown error"
    for attempt in range(1, MAX_TRIES + 1):
        try:
            return _chat_once(system_prompt, transcript, api_key, timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                last_error = "http %d (try %d/%d)" % (exc.code, attempt, MAX_TRIES)
                if attempt < MAX_TRIES:
                    time.sleep((2 ** (attempt - 1)) + random.random())
                    continue
            else:
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001 - best-effort error detail
                    detail = ""
                return None, "http %d: %s" % (exc.code, detail)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last_error = "%s: %s (try %d/%d)" % (
                exc.__class__.__name__,
                exc,
                attempt,
                MAX_TRIES,
            )
            if attempt < MAX_TRIES:
                time.sleep((2 ** (attempt - 1)) + random.random())
                continue
        except (ValueError, KeyError, IndexError) as exc:
            # Malformed JSON or unexpected response shape: fail the pair.
            return None, "bad response: %r" % (exc,)
    return None, last_error


def evaluate_pair(candidate, pair, api_key, timeout_s=REQUEST_TIMEOUT_S):
    # type: (dict, dict, str, int) -> dict
    """One (candidate, pair) evaluation. Returns the JSONL record."""
    started = time.time()
    output, error = call_cleanup_llm(
        candidate["system_prompt"], pair["disfluent"], api_key, timeout_s
    )
    latency_ms = int((time.time() - started) * 1000)
    record = {
        "candidate": candidate["name"],
        "pair_id": pair["id"],
        "class": pair["class"],
        "disfluent": pair["disfluent"],
        "reference": pair["reference"],
        "output": None,
        "exact": None,
        "f1": None,
        "latency_ms": latency_ms,
        "error": error,
    }
    if output is not None:
        cleaned = strip_cleanup_artifacts(output)
        record["output"] = cleaned
        record["exact"] = exact_match(cleaned, pair["reference"])
        record["f1"] = round(token_f1(cleaned, pair["reference"]), 6)
    else:
        record["f1"] = 0.0
    return record


def parse_args(argv=None):
    # type: (list) -> argparse.Namespace
    parser = argparse.ArgumentParser(
        description="Run the Bolo cleanup-prompt eval against the builtin corpus."
    )
    parser.add_argument(
        "--candidates",
        default=",".join(candidate_names()),
        help="comma-separated candidate names (default: all, registry order)",
    )
    parser.add_argument(
        "--source", default="builtin", help="corpus source (v1 supports: builtin)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="evaluate only the first N pairs"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="worker pool size, clamped to %d to respect rate limits" % MAX_CONCURRENCY,
    )
    parser.add_argument(
        "--out",
        default=os.path.join(_THIS_DIR, "results"),
        help="output directory for results.jsonl (default: evals/cleanup-prompt/results)",
    )
    return parser.parse_args(argv)


def _resolve_candidates(spec):
    # type: (str) -> list
    names = [name.strip() for name in spec.split(",") if name.strip()]
    if not names:
        raise SystemExit("error: --candidates got no names (have: %s)" % ", ".join(candidate_names()))
    resolved = []
    for name in names:
        try:
            resolved.append(get_candidate(name))
        except KeyError as exc:
            raise SystemExit("error: %s" % exc)
    return resolved


def print_leaderboard(records, ordered_candidates, out_path, error_count):
    # type: (list, list, str, int) -> None
    by_candidate = {}
    for record in records:
        by_candidate.setdefault(record["candidate"], []).append(record)

    rows = []
    for candidate in ordered_candidates:
        recs = by_candidate.get(candidate["name"], [])
        if not recs:
            continue
        rows.append(
            {
                "name": candidate["name"],
                "n": len(recs),
                "exact": exact_rate(recs),
                "f1": mean_f1(recs),
                "errors": sum(1 for rec in recs if rec["error"]),
            }
        )
    rows.sort(key=lambda row: (-row["exact"], -row["f1"], row["name"]))

    print("")
    print("cleanup-prompt eval: %d pairs x %d candidates, model %s" % (
        len(records) and max(row["n"] for row in rows) or 0,
        len(rows),
        MODEL,
    ))
    print("endpoint: %s (temperature 0, enable_thinking false)" % ENDPOINT)
    print("results: %s (%d network errors)" % (out_path, error_count))
    print("")
    print("leaderboard (sorted by exact match, then token F1):")
    for row in rows:
        print("  %-22s exact %5.3f  f1 %5.3f  errors %d  n %d" % (
            row["name"], row["exact"], row["f1"], row["errors"], row["n"],
        ))

    print("")
    print("by class (exact rate):")
    for cls in CLASSES:
        pair_ids = {pair["id"] for pair in CORPUS if pair["class"] == cls}
        cells = []
        for row in rows:
            recs = [rec for rec in by_candidate[row["name"]] if rec["pair_id"] in pair_ids]
            if recs:
                cells.append("%s %.3f" % (row["name"], exact_rate(recs)))
        print("  %-22s n=%-3d %s" % (cls, len(pair_ids), " | ".join(cells)))

    print("")
    print("false_positive_trap failures (candidate: failed pair ids):")
    trap_ids = {pair["id"] for pair in CORPUS if pair["class"] == "false_positive_trap"}
    any_failure = False
    for row in rows:
        failed = [
            rec["pair_id"]
            for rec in by_candidate[row["name"]]
            if rec["pair_id"] in trap_ids and not rec["exact"]
        ]
        if failed:
            any_failure = True
            print("  %-22s %s" % (row["name"], ", ".join(failed)))
    if not any_failure:
        print("  none")


def main(argv=None):
    # type: (list) -> int
    args = parse_args(argv)

    if args.source != "builtin":
        print("error: unknown --source %r (v1 supports: builtin)" % args.source, file=sys.stderr)
        return 2

    api_key = load_api_key()
    if not api_key:
        print(
            "error: TELNYX_API_KEY not set and not found in %s.\n"
            "Live evals call Bolo's production LLM endpoint; export the key "
            "first, e.g.: set -a; source ~/.claude/.env; set +a" % ENV_FILE,
            file=sys.stderr,
        )
        return 2

    if args.limit is not None and args.limit <= 0:
        print("error: --limit must be positive", file=sys.stderr)
        return 2

    candidates = _resolve_candidates(args.candidates)
    pairs = CORPUS if args.limit is None else CORPUS[: args.limit]
    concurrency = max(1, min(MAX_CONCURRENCY, args.concurrency))

    out_dir = args.out if os.path.isabs(args.out) else os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "results.jsonl")

    jobs = [(candidate, pair) for candidate in candidates for pair in pairs]
    print(
        "running %d calls (%d candidates x %d pairs, concurrency %d)..." % (
            len(jobs), len(candidates), len(pairs), concurrency,
        )
    )
    records = []
    with open(out_path, "w", encoding="utf-8") as handle, ThreadPoolExecutor(
        max_workers=concurrency
    ) as pool:
        futures = {
            pool.submit(evaluate_pair, candidate, pair, api_key): (candidate, pair)
            for candidate, pair in jobs
        }
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()

    error_count = sum(1 for record in records if record["error"])
    print_leaderboard(records, candidates, out_path, error_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
