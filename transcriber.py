"""Transcription module: audio in, final transcript out.

Owns everything between the mic and the finished transcript: the streaming
STT connection with its warm pool, the batch HTTP client with 429 fallback
and chunked retry, the stream-vs-batch race, and STT backoff/auth state.
Interface: Transcriber(availability/warm/begin_session) and the session
(feed/request_stop/finish/abandon) returning a two-phase result
(.preview immediately, .final() for the race, .cancel() to shortcut).
"""

import concurrent.futures
import io
import json
import threading
import time
import wave

import numpy as np
import requests

from stt import TelnyxStreamingSTT
from transcript_state import merge_transcript

STT_ENDPOINT = "https://api.telnyx.com/v2/ai/audio/transcriptions"
SAMPLE_RATE = 16000
CHANNELS = 1
STREAM_DRAIN_SECONDS = 0.35
SILENCE_PADDING = bytes(int(SAMPLE_RATE * 0.35 * 2))  # 350ms of 16kHz mono 16-bit
RATE_LIMIT_BACKOFF_SECONDS = 45.0
AUTH_BACKOFF_SECONDS = 86400.0
WARM_STREAM_MAX_AGE_SECONDS = 45.0

# STT prompt limit: Whisper uses a 224-token window; 4 chars/token is a safe approximation.
_STT_PROMPT_MAX_CHARS = 224 * 4


def build_stt_prompt(vocab_terms: list, context_text: str = "") -> str:
    """Vocabulary terms first, then a 120-char tail of the active text field,
    capped to Whisper's prompt window. Empty string when nothing useful."""
    parts = []
    if vocab_terms:
        parts.append(", ".join(vocab_terms))
    if context_text:
        tail = context_text.strip()[-120:]
        if tail:
            parts.append(tail)
    prompt = ". ".join(parts) if parts else ""
    return prompt[:_STT_PROMPT_MAX_CHARS]


def to_wav_bytes(audio) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


def should_accept_stream_result(transcript, duration_seconds, had_final) -> bool:
    """Stream wins when Deepgram sent a final, or the utterance is very short."""
    transcript = (transcript or "").strip()
    if not transcript:
        return False
    if duration_seconds > 25.0:
        return False
    if had_final:
        return True
    return duration_seconds <= 0.9 and len(transcript.split()) >= 1


def should_retry_chunked_batch(transcript, duration_seconds) -> bool:
    """Long utterance whose transcript looks truncated (dangling conjunction
    or implausibly low word rate) warrants a chunked re-transcription."""
    transcript = (transcript or "").strip()
    if duration_seconds < 12.0 or not transcript:
        return False
    if transcript.endswith((".", "!", "?", "\"")):
        return False
    trailing_tokens = {"a", "an", "and", "or", "but", "the", "to", "of", "on", "in", "for", "with", "at", "by"}
    words = transcript.split()
    if not words:
        return False
    return words[-1].lower().strip(",.!?\"'") in trailing_tokens or len(words) < int(duration_seconds * 1.7)


def prefer_chunked_transcript(original, chunked) -> bool:
    original = (original or "").strip()
    chunked = (chunked or "").strip()
    if not chunked:
        return False
    if len(chunked) <= len(original):
        return False
    if original.endswith((".", "!", "?")) and len(original.split()) >= len(chunked.split()) - 2:
        return False
    return True


class TranscriptionResult:
    """Two-phase result: .preview is available immediately after finish();
    .final() blocks on the stream-vs-batch race; .cancel() shortcuts it."""

    def __init__(self, preview, had_final, duration_seconds, batch_future, executor, log):
        self.preview = preview
        self._had_final = had_final
        self._duration_seconds = duration_seconds
        self._batch_future = batch_future
        self._executor = executor
        self._log = log

    def final(self):
        """Returns (transcript, source). source is one of stream / batch /
        batch+chunked / stream_fallback."""
        accepted = should_accept_stream_result(
            self.preview, self._duration_seconds, self._had_final
        )
        word_count = len((self.preview or "").split())
        kind = "final" if self._had_final else "partial"
        self._log(
            f"[stream] {kind} candidate words={word_count} "
            f"duration_s={self._duration_seconds:.2f} accepted={accepted}"
        )
        if accepted:
            self.cancel()
            return self.preview, "stream"
        self._executor.shutdown(wait=False)
        try:
            transcript, source = self._batch_future.result(timeout=9.0)
        except Exception as e:
            self._log(f"[pipeline] batch future failed: {e}")
            return self.preview or "", "stream_fallback"
        if not transcript:
            transcript = self.preview
        return transcript, source

    def cancel(self):
        self._batch_future.cancel()
        self._executor.shutdown(wait=False)


class TranscriptionSession:
    """One dictation's transcription. feed() PCM while recording,
    request_stop() on key release, finish() from the pipeline thread."""

    def __init__(self, transcriber, context_text, on_partial):
        self._transcriber = transcriber
        self._context_text = context_text or ""
        self._on_partial = on_partial or (lambda text: None)
        self._lock = threading.Lock()
        self._stream = None
        self._pending_pcm = []
        self._stopping = False
        self._done = threading.Event()
        self._committed_text = ""
        self._unstable_text = ""
        self.timings = {
            "stream_connected_at": None,
            "first_partial_at": None,
            "first_final_at": None,
            "stream_finalized_at": None,
            "stop_requested_at": None,
            "batch_started_at": None,
            "batch_finished_at": None,
            "chunked_started_at": None,
            "chunked_finished_at": None,
            "chunked_segments": 0,
            "rate_limited": False,
            "stream_failed": False,
        }

    @property
    def had_final(self):
        return self.timings["first_final_at"] is not None

    def display_text(self):
        with self._lock:
            return merge_transcript(self._committed_text, self._unstable_text)

    def feed(self, pcm_bytes):
        with self._lock:
            stream = self._stream
            if stream is None:
                self._pending_pcm.append(pcm_bytes)
                return
        try:
            stream.send_audio(pcm_bytes)
        except Exception:
            pass

    def request_stop(self):
        """Called on key release: stamps the stop time and pads the stream
        with silence so the provider's VAD finalizes the utterance."""
        if self.timings["stop_requested_at"] is None:
            self.timings["stop_requested_at"] = time.time()
        self._stopping = True
        with self._lock:
            stream = self._stream
        if stream is not None:
            try:
                stream.send_audio(SILENCE_PADDING)
            except Exception:
                pass

    def finish(self, wav_bytes, duration_seconds):
        """Start the batch request, drain and close the stream, return the
        two-phase result. Call from the pipeline thread after request_stop()."""
        if self.timings["stop_requested_at"] is None:
            self.timings["stop_requested_at"] = time.time()
        self._stopping = True
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        batch_future = executor.submit(
            self._transcriber._batch_transcribe,
            wav_bytes,
            duration_seconds,
            self._context_text,
            self.timings,
        )
        self._done.wait(timeout=STREAM_DRAIN_SECONDS + 0.5)
        self._close_stream()
        self.timings["stream_finalized_at"] = time.time()
        self._transcriber.end_session(self)
        self._transcriber.warm()
        preview = self.display_text().strip()
        return TranscriptionResult(
            preview,
            self.had_final,
            duration_seconds,
            batch_future,
            executor,
            self._transcriber._log,
        )

    def abandon(self):
        """Discard the session without transcribing (empty or too-short recording)."""
        self._stopping = True
        self._close_stream()
        self._transcriber.end_session(self)
        self._transcriber.warm()

    def _close_stream(self):
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.close(timeout=0.35)
            except Exception as e:
                self._transcriber._log(f"[stream] close error: {e}")

    # ── internal: called by the Transcriber ──────────────────────────────

    def _attach_stream(self, stream):
        """Wire up a connected stream, flush buffered audio, start draining."""
        with self._lock:
            arrived_too_late = self._stopping
            if not arrived_too_late:
                pending = self._pending_pcm
                self._pending_pcm = []
                self._stream = stream
        if arrived_too_late:
            try:
                stream.close()
            except Exception:
                pass
            return
        if pending:
            try:
                stream.send_audio(b"".join(pending))
            except Exception:
                pass
        self.timings["stream_connected_at"] = time.time()
        threading.Thread(target=self._drain_loop, daemon=True).start()

    def _drain_loop(self):
        while not self._stopping:
            with self._lock:
                stream = self._stream
            if stream is None:
                break
            item = stream.get_transcript(timeout=0.1)
            if item is None:
                continue
            self._handle_transcript(*item)
        stop_at = self.timings["stop_requested_at"]
        if stop_at is not None:
            deadline = stop_at + STREAM_DRAIN_SECONDS
            while time.time() < deadline:
                with self._lock:
                    stream = self._stream
                if stream is None:
                    break
                item = stream.get_transcript(timeout=0.05)
                if item is None:
                    continue
                self._handle_transcript(*item)
        self._done.set()

    def _handle_transcript(self, transcript, is_final):
        if not transcript:
            return
        now = time.time()
        with self._lock:
            if self.timings["first_partial_at"] is None:
                self.timings["first_partial_at"] = now
            if is_final:
                self._committed_text = merge_transcript(self._committed_text, transcript)
                self._unstable_text = ""
                if self.timings["first_final_at"] is None:
                    self.timings["first_final_at"] = now
            else:
                self._unstable_text = transcript.strip()
            preview = merge_transcript(self._committed_text, self._unstable_text)
        self._on_partial(preview)

    def _mark_stream_failed(self):
        self.timings["stream_failed"] = True
        self._done.set()


class Transcriber:
    def __init__(self, api_key, vocab_terms, log=None, on_error=None,
                 stream_factory=TelnyxStreamingSTT, batch_transport=requests.post):
        """vocab_terms is a zero-arg callable returning the current term list.
        stream_factory and batch_transport are the test seam."""
        self._api_key = api_key
        self._vocab_terms = vocab_terms
        self._log = log or (lambda message: None)
        self._on_error = on_error or (lambda message: None)
        self._stream_factory = stream_factory
        self._batch_transport = batch_transport
        self._backoff_until = 0.0
        self._auth_failed = False
        self._active_session = None
        self._warm_lock = threading.Lock()
        self._warm_stream = None
        self._warm_connected_at = 0.0
        self._warm_connecting = False

    # ── availability ─────────────────────────────────────────────────────

    def availability(self):
        """Returns (ok, reason, wait_seconds); reason is auth or rate_limit."""
        if self._auth_failed:
            return False, "auth", 0
        remaining = self._backoff_until - time.time()
        if remaining > 0:
            return False, "rate_limit", int(remaining) + 1
        return True, "", 0

    # ── warm pool ────────────────────────────────────────────────────────

    def warm(self):
        if self._auth_failed:
            return  # API key is bad - don't hammer the endpoint
        with self._warm_lock:
            if self._active_session or self._warm_stream or self._warm_connecting:
                return
            self._warm_connecting = True
        threading.Thread(target=self._warm_worker, daemon=True).start()

    def _warm_worker(self):
        stream = self._stream_factory()
        try:
            stream.connect(self._api_key, keywords=self._vocab_terms())
        except Exception as e:
            self._log(f"[stream] warm connect failed: {e}")
            self._note_stream_failure(e)
            with self._warm_lock:
                self._warm_connecting = False
            return
        with self._warm_lock:
            if self._active_session or self._warm_stream:
                self._warm_connecting = False
                try:
                    stream.close()
                except Exception:
                    pass
                return
            self._warm_stream = stream
            self._warm_connected_at = time.time()
            self._warm_connecting = False
        self._log("[stream] warm connection ready")

    def _claim_warm_stream(self):
        with self._warm_lock:
            stream = self._warm_stream
            age = time.time() - self._warm_connected_at
            if stream is not None and age > WARM_STREAM_MAX_AGE_SECONDS:
                self._log(f"[stream] warm stream stale ({age:.1f}s), discarding")
                try:
                    stream.close()
                except Exception:
                    pass
                stream = None
            self._warm_stream = None
            self._warm_connecting = False
            return stream

    def _note_stream_failure(self, error):
        message = str(error)
        if "401" in message or "Unauthorized" in message or "auth" in message.lower():
            self._auth_failed = True
            self._backoff_until = time.time() + AUTH_BACKOFF_SECONDS
            self._log("[stream] 401 auth failure - disabling stream. Fix API key and restart.")
            self._on_error("Invalid API key - check ~/.bolo/env")

    # ── sessions ─────────────────────────────────────────────────────────

    def begin_session(self, context_text="", on_partial=None):
        session = TranscriptionSession(self, context_text, on_partial)
        with self._warm_lock:
            self._active_session = session
        stream = self._claim_warm_stream()
        if stream is not None:
            session._attach_stream(stream)
        else:
            threading.Thread(
                target=self._connect_async, args=(session,), daemon=True
            ).start()
        return session

    def end_session(self, session):
        with self._warm_lock:
            if self._active_session is session:
                self._active_session = None

    def _connect_async(self, session):
        stream = self._stream_factory()
        try:
            stream.connect(self._api_key, keywords=self._vocab_terms())
        except Exception as e:
            self._log(f"[stream] async connect failed: {e}")
            self._note_stream_failure(e)
            session._mark_stream_failed()
            return
        if session._stopping:
            try:
                stream.close()
            except Exception:
                pass
            return
        session._attach_stream(stream)
        self._log("[stream] async connected")

    def shutdown(self):
        """Close the active session's stream and the warm stream (app quit)."""
        with self._warm_lock:
            session = self._active_session
            self._active_session = None
            warm = self._warm_stream
            self._warm_stream = None
            self._warm_connecting = False
        if session is not None:
            session._stopping = True
            session._close_stream()
        if warm is not None:
            try:
                warm.close()
            except Exception:
                pass

    # ── batch ────────────────────────────────────────────────────────────

    def _batch_request(self, wav_bytes, context_text):
        self._log("[stt] using batch fallback")
        try:
            vocab_terms = self._vocab_terms()
            model_config = {"smart_format": True, "punctuate": True}
            if vocab_terms:
                model_config["keyterms"] = vocab_terms[:50]
            stt_prompt = build_stt_prompt(vocab_terms, context_text)
            primary_data = {
                "model": "deepgram/nova-3",
                "language": "en",
                "model_config": json.dumps(model_config),
            }
            if stt_prompt:
                primary_data["prompt"] = stt_prompt
                self._log(f"[stt] prompt injected ({len(stt_prompt)} chars)")
            resp = self._batch_transport(
                STT_ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data=primary_data,
                timeout=8,
            )
            if resp.status_code == 401:
                self._log("[stt] 401 Unauthorized - check TELNYX_API_KEY in ~/.bolo/env")
                raise RuntimeError("STT auth failed (401): invalid or missing API key")
            rate_limited = resp.status_code == 429
            if resp.status_code == 429:
                # Rate-limited fallback: lighter model, still include prompt
                fallback_data = {"model": "distil-whisper/distil-large-v2"}
                if stt_prompt:
                    fallback_data["prompt"] = stt_prompt
                resp = self._batch_transport(
                    STT_ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                    data=fallback_data,
                    timeout=8,
                )
                if resp.status_code == 429:
                    return "", True
            if resp.status_code == 200:
                return resp.json().get("text", "").strip(), rate_limited
            self._log(f"[stt error] {resp.status_code}: {resp.text[:200]}")
            return "", rate_limited
        except RuntimeError:
            raise
        except Exception as e:
            self._log(f"[stt exception] {e}")
            # Retry once on transient network/SSL errors
            try:
                time.sleep(0.3)
                retry_data = {
                    "model": "deepgram/nova-3",
                    "language": "en",
                    "model_config": json.dumps({"smart_format": True, "punctuate": True}),
                }
                retry_prompt = build_stt_prompt(self._vocab_terms(), context_text)
                if retry_prompt:
                    retry_data["prompt"] = retry_prompt
                resp = self._batch_transport(
                    STT_ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                    data=retry_data,
                    timeout=8,
                )
                if resp.status_code == 200:
                    self._log("[stt] retry succeeded")
                    return resp.json().get("text", "").strip(), False
            except Exception as e2:
                self._log(f"[stt retry failed] {e2}")
            raise

    def _batch_transcribe(self, wav_bytes, duration_seconds, context_text, timings):
        timings["batch_started_at"] = time.time()
        try:
            transcript, rate_limited = self._batch_request(wav_bytes, context_text)
        except RuntimeError as e:
            timings["batch_finished_at"] = time.time()
            msg = str(e)
            if "401" in msg or "auth failed" in msg.lower():
                # Auth failure: freeze backoff so no further requests are made.
                # User must fix the key and restart.
                self._auth_failed = True
                self._backoff_until = time.time() + AUTH_BACKOFF_SECONDS
                self._on_error("Invalid API key - check ~/.bolo/env")
            else:
                self._on_error(f"STT failed: {e}")
            return "", "batch"
        except Exception as e:
            timings["batch_finished_at"] = time.time()
            self._on_error(f"STT failed: {e}")
            return "", "batch"
        timings["batch_finished_at"] = time.time()
        if not transcript:
            if rate_limited:
                timings["rate_limited"] = True
                self._backoff_until = time.time() + RATE_LIMIT_BACKOFF_SECONDS
                self._on_error("Rate limited, try again shortly")
            else:
                self._on_error("STT error")
            return "", "batch"
        if rate_limited:
            self._log("[stt] primary model rate limited")
            timings["rate_limited"] = True
            self._backoff_until = time.time() + RATE_LIMIT_BACKOFF_SECONDS
            return transcript, "batch"
        if should_retry_chunked_batch(transcript, duration_seconds):
            chunked = self._batch_transcribe_chunked(wav_bytes, context_text, timings)
            if prefer_chunked_transcript(transcript, chunked):
                self._log("[stt] accepted chunked batch retry")
                return chunked, "batch+chunked"
        return transcript, "batch"

    def _batch_transcribe_chunked(self, wav_bytes, context_text, timings):
        try:
            pcm = np.frombuffer(wav_bytes[44:], dtype=np.int16).copy()
        except Exception as e:
            self._log(f"[stt] chunked decode failed: {e}")
            return ""
        total_samples = len(pcm)
        if total_samples <= 0:
            return ""
        chunk_samples = int(SAMPLE_RATE * 10.0)
        overlap_samples = int(SAMPLE_RATE * 1.25)
        segments = []
        start = 0
        while start < total_samples:
            end = min(total_samples, start + chunk_samples)
            segments.append(pcm[start:end])
            if end >= total_samples:
                break
            start = max(0, end - overlap_samples)
        if len(segments) <= 1:
            return ""

        self._log(f"[stt] chunked batch retry segments={len(segments)}")
        timings["chunked_started_at"] = time.time()
        timings["chunked_segments"] = len(segments)
        transcripts = []
        for segment in segments:
            chunk_wav = to_wav_bytes(segment)
            try:
                text, rate_limited = self._batch_request(chunk_wav, context_text)
            except Exception as e:
                self._log(f"[stt] chunked request failed: {e}")
                text = ""
                rate_limited = False
            if rate_limited:
                self._log("[stt] chunked retry aborted due to rate limit")
                transcripts = []
                break
            if text:
                transcripts.append(text.strip())
        timings["chunked_finished_at"] = time.time()
        if not transcripts:
            return ""
        merged = ""
        for text in transcripts:
            merged = merge_transcript(merged, text)
        return merged.strip()
