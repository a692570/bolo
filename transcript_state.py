#!/usr/bin/env python3

import threading
from dataclasses import dataclass, field


@dataclass
class TranscriptState:
    committed_text: str = ""
    unstable_text: str = ""
    visible_text: str = ""
    final_text: str = ""
    final_source: str = ""
    first_partial_at: float = None
    first_final_at: float = None
    first_visible_at: float = None
    stream_finalized_at: float = None
    batch_started_at: float = None
    batch_finished_at: float = None
    chunked_started_at: float = None
    chunked_finished_at: float = None
    chunked_segments: int = 0
    rate_limited: bool = False
    reconcile_started_at: float = None
    reconcile_finished_at: float = None
    cleanup_started_at: float = None
    cleanup_finished_at: float = None
    correction_target: str = ""
    stream_failed: bool = False
    stream_error: str = ""
    stop_requested_at: float = None
    closed: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def display_text(self) -> str:
        return merge_transcript(self.committed_text, self.unstable_text)


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
