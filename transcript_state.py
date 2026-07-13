#!/usr/bin/env python3
"""UI-side state for one dictation, plus transcript text helpers.

TranscriptState tracks what the app has shown and inserted (visible text,
final text and its source, correction target, LLM timing marks). All
transcription-side state and timings live in the Transcriber session
(transcriber.py). merge_transcript and longest_common_prefix are pure
helpers shared by the transcription and insertion modules."""

from dataclasses import dataclass


@dataclass
class TranscriptState:
    visible_text: str = ""
    final_text: str = ""
    final_source: str = ""
    first_visible_at: float = None
    rate_limited: bool = False
    reconcile_started_at: float = None
    reconcile_finished_at: float = None
    cleanup_started_at: float = None
    cleanup_finished_at: float = None
    correction_target: str = ""


def merge_transcript(base: str, incoming: str) -> str:
    base = base.strip()
    incoming = incoming.strip()
    if not base:
        return incoming
    if not incoming:
        return base
    if incoming.startswith(base):
        return incoming
    if base.startswith(incoming):
        return base

    base_words = base.split()
    incoming_words = incoming.split()
    max_overlap = min(len(base_words), len(incoming_words))
    for overlap in range(max_overlap, 0, -1):
        if base_words[-overlap:] == incoming_words[:overlap]:
            return " ".join(base_words + incoming_words[overlap:])
    return f"{base} {incoming}".strip()


def longest_common_prefix(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    idx = 0
    while idx < limit and left[idx] == right[idx]:
        idx += 1
    return idx
