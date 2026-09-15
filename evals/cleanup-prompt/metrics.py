"""Pure scoring functions for the cleanup-prompt eval. No IO, no network.

normalize()           : lowercase, curly quotes to straight, collapse whitespace,
                        strip trailing punctuation (and wrapping quote pairs).
exact_match()         : normalize both sides, compare strings.
token_f1()            : multiset (bag-of-words) F1 over normalized tokens.
exact_rate/mean_f1    : aggregates over result dicts.

Tokenization for F1 splits the normalized text on whitespace and strips
punctuation from token edges, so F1 measures word overlap, not punctuation
style. Exact match stays the strict metric; F1 credits partial matches.

Trailing-punctuation stripping removes sentence-final . ! ? , ; : and, like
production's strip_wrapping_quotes, one wrapping quote pair (both ends
matching), applied repeatedly so '"no".' reduces to 'no'. Interior quotes
such as 'say "no"' are preserved.

Run tests: python3 -m pytest evals/cleanup-prompt/test_eval.py
"""

import re
from collections import Counter

_CURLY_DOUBLE_RE = re.compile(r"[\u201c\u201d\u201e\u201f]")
_CURLY_SINGLE_RE = re.compile(r"[\u2018\u2019\u201a\u201b\u2032\u2035]")
_WHITESPACE_RE = re.compile(r"\s+")
# Trailing punctuation stripped from the normalized string, repeatedly.
_TRAILING_PUNCT_CHARS = ".!?,;:"
_TRAILING_QUOTE_CHARS = "\"'"
# Per-token edge punctuation stripped before F1 token counting.
_TOKEN_EDGE_CHARS = ".,!?:;\"'()"


def normalize(text):
    # type: (str) -> str
    """Lowercase, straighten curly quotes, collapse whitespace, strip trailing
    punctuation and wrapping quote pairs."""
    if not text:
        return ""
    text = _CURLY_DOUBLE_RE.sub('"', text)
    text = _CURLY_SINGLE_RE.sub("'", text)
    text = text.lower()
    text = _WHITESPACE_RE.sub(" ", text).strip()
    while True:
        stripped = text.rstrip(_TRAILING_PUNCT_CHARS)
        if len(stripped) >= 2 and stripped[0] in _TRAILING_QUOTE_CHARS and stripped[-1] == stripped[0]:
            stripped = stripped[1:-1].rstrip(_TRAILING_PUNCT_CHARS)
        stripped = stripped.strip()
        if stripped == text:
            return text
        text = stripped


def _tokens(text):
    # type: (str) -> list
    return [token.strip(_TOKEN_EDGE_CHARS) for token in normalize(text).split()]


def exact_match(candidate_text, reference_text):
    # type: (str, str) -> bool
    """Normalized exact match: both sides reduced by normalize(), then compared."""
    return normalize(candidate_text) == normalize(reference_text)


def token_f1(candidate_text, reference_text):
    # type: (str, str) -> float
    """Multiset token F1. Precision and recall over word bags; repeated words
    count with multiplicity (min of the two counts is the overlap)."""
    candidate_counts = Counter(_tokens(candidate_text))
    reference_counts = Counter(_tokens(reference_text))
    candidate_total = sum(candidate_counts.values())
    reference_total = sum(reference_counts.values())
    if candidate_total == 0 or reference_total == 0:
        return 0.0
    overlap = sum(
        min(count, reference_counts[token])
        for token, count in candidate_counts.items()
    )
    precision = overlap / candidate_total
    recall = overlap / reference_total
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def exact_rate(results):
    # type: (list) -> float
    """Exact-match rate over a list of {"exact": bool} dicts; errors count as miss."""
    if not results:
        return 0.0
    return sum(1 for item in results if item.get("exact")) / len(results)


def mean_f1(results):
    # type: (list) -> float
    """Mean token F1 over a list of {"f1": float} dicts; errors count as 0."""
    if not results:
        return 0.0
    return sum(item.get("f1") or 0.0 for item in results) / len(results)
