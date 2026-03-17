#!/usr/bin/env python3
"""
Small evaluation harness for Bolo transcript quality.

Usage:
  python3 eval_dictation.py init
  python3 eval_dictation.py prompts
  python3 eval_dictation.py score eval_results.json

`init` prints a JSON template you can save and fill with observed transcripts.
`prompts` prints the phrase list in a speakable format.
`score` compares results against expected phrases and reports exact/normalized matches.
"""

import argparse
import json
import re
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PHRASES_FILE = BASE_DIR / "eval_phrases.json"


def load_phrases():
    return json.loads(PHRASES_FILE.read_text(encoding="utf-8"))


def normalize(text: str) -> str:
    text = (text or "").strip().casefold()
    contractions = {
        "i'm": "i am",
        "it's": "it is",
        "that's": "that is",
        "what's": "what is",
        "can't": "cannot",
        "won't": "will not",
        "don't": "do not",
        "didn't": "did not",
        "i've": "i have",
        "you're": "you are",
    }
    for src, dst in contractions.items():
        text = text.replace(src, dst)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def init_template():
    template = []
    for item in load_phrases():
        template.append(
            {
                "id": item["id"],
                "expected": item["expected"],
                "actual": "",
                "notes": "",
            }
        )
    print(json.dumps(template, indent=2))


def print_prompts():
    for idx, item in enumerate(load_phrases(), start=1):
        print(f"{idx}. [{item['category']}] {item['expected']}")


def score_results(results_path: Path):
    phrases = {item["id"]: item for item in load_phrases()}
    results = json.loads(results_path.read_text(encoding="utf-8"))

    rows = []
    exact = 0
    normalized = 0
    for result in results:
        phrase = phrases.get(result["id"])
        if not phrase:
            continue
        expected = phrase["expected"]
        actual = result.get("actual", "")
        exact_match = actual.strip() == expected.strip()
        normalized_match = normalize(actual) == normalize(expected)
        exact += int(exact_match)
        normalized += int(normalized_match)
        rows.append(
            {
                "id": phrase["id"],
                "category": phrase["category"],
                "exact": exact_match,
                "normalized": normalized_match,
                "expected": expected,
                "actual": actual,
            }
        )

    print(f"phrases: {len(rows)}")
    print(f"exact_match: {exact}/{len(rows)}")
    print(f"normalized_match: {normalized}/{len(rows)}")
    long_form_rows = [row for row in rows if row["category"] == "long_form"]
    if long_form_rows:
        long_form_ok = sum(int(row["normalized"]) for row in long_form_rows)
        print(f"long_form_match: {long_form_ok}/{len(long_form_rows)}")
    print("")
    for row in rows:
        status = "OK" if row["normalized"] else "MISS"
        print(f"{status} {row['id']} [{row['category']}]")
        if not row["normalized"]:
            print(f"  expected: {row['expected']}")
            print(f"  actual:   {row['actual']}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("prompts")
    score = sub.add_parser("score")
    score.add_argument("results")
    args = parser.parse_args()

    if args.cmd == "init":
        init_template()
        return
    if args.cmd == "prompts":
        print_prompts()
        return
    if args.cmd == "score":
        score_results(Path(args.results))
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
