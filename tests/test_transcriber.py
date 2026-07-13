"""Unit tests for the Transcriber module: decision rules, the stream-vs-batch
race, backoff/auth availability, chunked retry, and the warm pool.

FakeStream and a fake batch transport stand in at the module's seams, so the
whole race runs with no mic, network, or API key."""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transcriber import (
    Transcriber,
    build_stt_prompt,
    prefer_chunked_transcript,
    should_accept_stream_result,
    should_retry_chunked_batch,
    to_wav_bytes,
    SAMPLE_RATE,
)


class FakeStream:
    def __init__(self, transcripts=None, fail=None):
        self.sent = []
        self.closed = False
        self._queue = list(transcripts or [])
        self._fail = fail

    def connect(self, api_key, keywords=None):
        if self._fail:
            raise self._fail

    def send_audio(self, pcm):
        self.sent.append(pcm)

    def get_transcript(self, timeout=0.05):
        if self._queue:
            return self._queue.pop(0)
        time.sleep(min(timeout, 0.01))
        return None

    def close(self, timeout=5.0):
        self.closed = True


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def transport_returning(*texts_or_statuses):
    """Each call pops the next item: a str becomes a 200 with that text,
    an int becomes that status code."""
    remaining = list(texts_or_statuses)

    def _post(url, headers=None, files=None, data=None, timeout=None):
        item = remaining.pop(0) if remaining else texts_or_statuses[-1]
        if isinstance(item, int):
            return FakeResponse(item)
        return FakeResponse(200, {"text": item})

    return _post


def make_transcriber(stream=None, transport=None, on_error=None):
    return Transcriber(
        "test-key",
        lambda: ["Telnyx", "Wispr Flow"],
        on_error=on_error,
        stream_factory=lambda: stream if stream is not None else FakeStream(),
        batch_transport=transport or transport_returning("batch result"),
    )


def run_session(transcriber, duration=5.0, wav=None, partials=None):
    session = transcriber.begin_session(
        "context", on_partial=partials.append if partials is not None else None
    )
    deadline = time.time() + 2.0
    while session.timings["stream_connected_at"] is None and time.time() < deadline:
        time.sleep(0.01)
    session.feed(b"\x00\x00")
    time.sleep(0.05)  # let the drain loop consume queued transcripts
    session.request_stop()
    wav = wav if wav is not None else to_wav_bytes(np.zeros(SAMPLE_RATE, dtype=np.int16))
    return session, session.finish(wav, duration)


# ── pure decision rules ──────────────────────────────────────────────────


def test_accept_stream_requires_text():
    assert should_accept_stream_result("", 1.0, True) is False


def test_accept_stream_rejects_long_utterances():
    assert should_accept_stream_result("plenty of words here", 26.0, True) is False


def test_accept_stream_trusts_finals():
    assert should_accept_stream_result("hello world", 5.0, True) is True


def test_accept_stream_partial_only_when_very_short():
    assert should_accept_stream_result("hi", 0.8, False) is True
    assert should_accept_stream_result("hi there friend", 1.5, False) is False


def test_chunked_retry_only_for_long_truncated_audio():
    assert should_retry_chunked_batch("short one", 5.0) is False
    assert should_retry_chunked_batch("It ended cleanly.", 20.0) is False
    assert should_retry_chunked_batch("I was thinking about the", 20.0) is True


def test_prefer_chunked_wants_meaningfully_longer():
    assert prefer_chunked_transcript("original text", "") is False
    assert prefer_chunked_transcript("original text", "orig") is False
    assert prefer_chunked_transcript("I was thinking about the", "I was thinking about the future of this") is True


def test_build_stt_prompt_vocab_and_context_tail():
    prompt = build_stt_prompt(["Telnyx"], "some earlier text in the field")
    assert prompt.startswith("Telnyx")
    assert prompt.endswith("field")
    assert len(build_stt_prompt(["x" * 2000], "")) <= 224 * 4


# ── the race ─────────────────────────────────────────────────────────────


def test_stream_final_wins_and_batch_is_skipped():
    partials = []
    stream = FakeStream([("hello there", False), ("hello there world", True)])
    t = make_transcriber(stream=stream, transport=transport_returning("batch text"))
    session, result = run_session(t, duration=5.0, partials=partials)
    assert result.preview == "hello there world"
    assert session.had_final is True
    text, source = result.final()
    assert (text, source) == ("hello there world", "stream")
    assert partials  # overlay preview callback fired
    assert stream.closed is True


def test_batch_wins_when_stream_has_no_final():
    stream = FakeStream([("hello", False)])
    t = make_transcriber(stream=stream, transport=transport_returning("hello there my friend"))
    session, result = run_session(t, duration=5.0)
    assert session.had_final is False
    text, source = result.final()
    assert (text, source) == ("hello there my friend", "batch")


def test_empty_batch_falls_back_to_stream_preview():
    stream = FakeStream([("partial words", False)])
    t = make_transcriber(stream=stream, transport=transport_returning(500))
    session, result = run_session(t, duration=5.0)
    text, source = result.final()
    assert text == "partial words"
    assert source == "batch"


def test_rate_limit_sets_backoff_and_reports():
    errors = []
    t = make_transcriber(
        stream=FakeStream(), transport=transport_returning(429, 429), on_error=errors.append
    )
    session, result = run_session(t, duration=5.0)
    text, source = result.final()
    assert text == ""
    assert session.timings["rate_limited"] is True
    ok, reason, wait = t.availability()
    assert ok is False and reason == "rate_limit" and wait > 0
    assert "Rate limited" in errors[0]


def test_auth_failure_freezes_transcriber():
    errors = []
    t = make_transcriber(
        stream=FakeStream(), transport=transport_returning(401), on_error=errors.append
    )
    session, result = run_session(t, duration=5.0)
    text, _ = result.final()
    assert text == ""
    ok, reason, _ = t.availability()
    assert ok is False and reason == "auth"
    assert "Invalid API key" in errors[0]


def test_chunked_retry_improves_truncated_long_batch():
    truncated = "so I was thinking about the"
    chunk_one = "so I was thinking about the future of"
    chunk_two = "the future of dictation apps on the Mac"
    t = make_transcriber(
        stream=FakeStream(),
        transport=transport_returning(truncated, chunk_one, chunk_two),
    )
    wav = to_wav_bytes(np.zeros(SAMPLE_RATE * 15, dtype=np.int16))
    session, result = run_session(t, duration=15.0, wav=wav)
    text, source = result.final()
    assert source == "batch+chunked"
    assert text == "so I was thinking about the future of dictation apps on the Mac"
    assert session.timings["chunked_segments"] == 2


def test_cancel_skips_batch():
    t = make_transcriber(stream=FakeStream([("scratch that", True)]))
    session, result = run_session(t, duration=1.0)
    result.cancel()  # command fast path: no exception, no batch needed
    assert result.preview == "scratch that"


# ── warm pool ────────────────────────────────────────────────────────────


def test_begin_session_claims_warm_stream():
    streams = []

    def factory():
        stream = FakeStream()
        streams.append(stream)
        return stream

    t = Transcriber(
        "test-key", lambda: [], stream_factory=factory,
        batch_transport=transport_returning("x"),
    )
    t.warm()
    deadline = time.time() + 2.0
    while t._warm_stream is None and time.time() < deadline:
        time.sleep(0.01)
    assert t._warm_stream is not None

    session = t.begin_session("", on_partial=None)
    time.sleep(0.05)
    assert len(streams) == 1  # warm stream reused, no second connect
    assert session.timings["stream_connected_at"] is not None
    session.request_stop()
    session.abandon()


def test_availability_ok_by_default():
    t = make_transcriber()
    assert t.availability() == (True, "", 0)
