# Cleanup-prompt eval harness (Bolo)

Offline-capable eval for Bolo's LLM cleanup prompt: scores candidate system prompts
against a built-in 52-pair synthetic disfluency corpus by calling Bolo's PRODUCTION
LLM endpoint (api.telnyx.com/v2/ai/chat/completions, Qwen/Qwen3-235B-A22B,
temperature 0, enable_thinking false, cleanup_max_tokens parity with src/main.rs).
Run live: `cd evals/cleanup-prompt && python3 program.py` (needs TELNYX_API_KEY in
env or ~/.claude/.env; concurrency <= 4, retries on 429/5xx).
Run offline tests: `python3 -m pytest evals/cleanup-prompt/test_eval.py -q`
(zero network, no key, runs in CI's pytest -q on Python 3.9 and 3.13).
Scores: normalized exact-match rate plus multiset token F1, overall and broken down
by failure class (filler, self_correction, false_positive_trap, punctuation,
article_contraction, multi_sentence, edge_case) — the per-class table is the output
that matters. Results land in `evals/cleanup-prompt/results/results.jsonl`.

## What it is

The cleanup prompt in `src/main.rs` was changed twice this quarter with manual curl
spot-checks of 4 cases each. This harness replaces that with a repeatable eval:
every candidate prompt runs against every corpus pair through the exact production
call path, and the leaderboard shows which prompt instruction actually earns its
score. The pattern is absorbed from AssemblyAI/blurt's DSPy eval (candidate cleanup
instructions scored against a hand-annotated disfluency corpus), not their stack:
Bolo v1 is stdlib-only Python, a synthetic corpus, and a JSONL artifact of record.

## Usage

```sh
# all candidates x full corpus (the first-leaderboard command)
python3 program.py

# subset
python3 program.py --candidates production-current,production-previous \
    --limit 20 --concurrency 4 --out results/

# offline tests (no key, no network)
python3 -m pytest evals/cleanup-prompt/test_eval.py -q
```

Flags: `--candidates a,b` (default: all, registry order), `--source builtin` (only
source in v1), `--limit N` (first N pairs), `--concurrency N` (clamped to 4),
`--out DIR` (default: `results/` next to program.py). The runner refuses to start
without TELNYX_API_KEY and never prints it.

## Files

- `corpus.py` — 52 (disfluent, reference) pairs with `class` tags. Quotas:
  self_correction >= 10, false_positive_trap >= 6 (pairs where a quoted/actual
  "no" or a narrative "actually" must SURVIVE).
- `candidates.py` — registry. `production-current` is the Default profile prompt
  copied byte-for-byte from `src/main.rs` (HEAD 533fe29); `production-previous`
  is the same prompt before commit f1fb06e added the self-correction sentence;
  the three alternatives splice only that instruction, so any score delta versus
  production-previous is attributable to one sentence.
- `metrics.py` — normalize (lowercase, curly quotes to straight, collapse
  whitespace, strip trailing punctuation and wrapping quote pairs), normalized
  exact match, multiset token F1. Pure functions, no IO.
- `program.py` — the runner. Production-parity details: same endpoint/model,
  temperature 0, `enable_thinking: false`, `max_tokens = clamp(words*12, 1200,
  3000)`, and output is passed through the same artifact stripping as production
  (code fences, CLEAN:/TRANSCRIPT: labels, wrapping quotes) before scoring.
- `test_eval.py` — offline pytest suite (corpus integrity, normalization,
  asserted F1 math, registry shape, key handling).

## Adding a candidate

Append to `CANDIDATES` in `candidates.py`:

```python
{"name": "my-variant", "system_prompt": _with_self_correction("...your sentence...")},
```

Names are lowercase and dash-separated (usable in `--candidates`). Keep the
production strings untouched; build variants with `_with_self_correction()` so
everything but the self-correction instruction stays byte-identical to production
and score differences stay attributable. Then run:

```sh
python3 program.py --candidates production-current,my-variant
```

## Reading the leaderboard

- Overall exact match is the strict metric (punctuation style matters).
- Token F1 credits partial matches; a gap between F1 and exact usually means
  punctuation or number formatting drift, not content loss.
- `by class` is the decision table: self_correction rates show what the
  self-correction sentence buys; false_positive_trap failures list the pair ids
  each candidate collapsed (a candidate dropping a narrative "actually" or a
  real "no" is a regression, regardless of its overall score).

## Future upgrade: real corpus

The builtin corpus is synthetic. The upgrade path (v2) is the hand-annotated
disfluency corpus blurt uses: Switchboard-derived `nyralabs/disfluency_speech_english`
(~5k utterances). Add a `--source hf` loader that maps annotated disfluency tags
to (disfluent, reference) pairs; keep `builtin` as the offline default. v1
deliberately ships without it: no network dependency, no dataset drift between
runs, corpus readable in one file.

## Artifact of record

`results/results.jsonl` is committed after meaningful runs: one line per
(candidate, pair) with output, latency, error, and both scores. It contains only
model outputs over synthetic corpus content — no real user dictations, no API
keys — so it is safe to commit. Reruns overwrite the file; git history is the
record of past runs. When comparing prompts, commit the results file alongside
any prompt change so the delta is auditable.
