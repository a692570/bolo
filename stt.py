"""
stt.py — Bolo streaming STT components.

Classes:
    SilenceDetector       — RMS-based end-of-utterance detection (ported from Swift).
    TelnyxStreamingSTT    — WebSocket streaming STT via Telnyx/Deepgram API.
"""

import asyncio
import json
import queue
import struct
import threading
from typing import Optional, Tuple


# ── SilenceDetector ───────────────────────────────────────────────────────────

class SilenceDetector:
    """
    Detects speech start and end-of-utterance from a stream of PCM16 chunks.

    Ported from Swift silence detection logic:
      - speech_threshold = 0.02  (RMS on 0.0-1.0 scale)
      - required_silence_duration = disabled by default in Bolo's current UX
    """

    SPEECH_THRESHOLD = 0.02
    REQUIRED_SILENCE_DURATION = 9999.0

    def __init__(self):
        self._has_speech: bool = False
        self._trailing_silence: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def process(self, pcm16_bytes: bytes, elapsed_seconds: float) -> Optional[str]:
        """
        Process one audio chunk.

        Args:
            pcm16_bytes:     Raw PCM 16-bit little-endian audio bytes.
            elapsed_seconds: Duration represented by this chunk in seconds
                             (e.g. chunk_frames / sample_rate).

        Returns:
            "speech_start"     — first chunk above threshold (once per utterance).
            "end_of_utterance" — silence long enough after speech detected.
            None               — no state transition.
        """
        rms = self._compute_rms(pcm16_bytes)

        if rms >= self.SPEECH_THRESHOLD:
            # Audio is above threshold: active speech
            was_silent = not self._has_speech
            self._has_speech = True
            self._trailing_silence = 0.0
            if was_silent:
                return "speech_start"
        else:
            # Audio is below threshold (silence)
            if self._has_speech:
                self._trailing_silence += elapsed_seconds
                if self._trailing_silence >= self.REQUIRED_SILENCE_DURATION:
                    return "end_of_utterance"

        return None

    def reset(self) -> None:
        """Reset all internal state (call between utterances)."""
        self._has_speech = False
        self._trailing_silence = 0.0

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_rms(pcm16_bytes: bytes) -> float:
        """Compute RMS on a 0.0-1.0 scale from raw PCM16-LE bytes."""
        if not pcm16_bytes:
            return 0.0
        n_samples = len(pcm16_bytes) // 2
        if n_samples == 0:
            return 0.0
        # Unpack as signed 16-bit integers
        samples = struct.unpack(f"<{n_samples}h", pcm16_bytes[:n_samples * 2])
        # Normalise to -1.0..1.0 and compute RMS
        mean_sq = sum(s * s for s in samples) / n_samples
        rms_int = mean_sq ** 0.5
        return rms_int / 32768.0


# ── TelnyxStreamingSTT ────────────────────────────────────────────────────────

_WS_ENDPOINT = (
    "wss://api.telnyx.com/v2/speech-to-text/transcription"
    "?transcription_engine=Deepgram"
    "&model=deepgram/nova-3"
    "&input_format=wav"
    "&interim_results=true"
    "&language=en-US"
)

# 44-byte WAV header: PCM, 16 kHz, mono, 16-bit, streaming (data size = 0xFFFFFFFF)
_SAMPLE_RATE  = 16000
_CHANNELS     = 1
_BITS         = 16
_BYTE_RATE    = _SAMPLE_RATE * _CHANNELS * _BITS // 8
_BLOCK_ALIGN  = _CHANNELS * _BITS // 8
_DATA_SIZE    = 0xFFFFFFFF  # sentinel for streaming WAV

def _build_wav_header(first_chunk: bytes) -> bytes:
    """
    Construct a 44-byte streaming WAV header followed by the first PCM chunk.

    Layout (little-endian):
      Offset  Size  Field
       0       4    ChunkID        "RIFF"
       4       4    ChunkSize      0xFFFFFFFF  (streaming)
       8       4    Format         "WAVE"
      12       4    Subchunk1ID    "fmt "
      16       4    Subchunk1Size  16
      20       2    AudioFormat    1  (PCM)
      22       2    NumChannels    1
      24       4    SampleRate     16000
      28       4    ByteRate       32000
      32       2    BlockAlign     2
      34       2    BitsPerSample  16
      36       4    Subchunk2ID    "data"
      40       4    Subchunk2Size  0xFFFFFFFF  (streaming)
    """
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        _DATA_SIZE,          # ChunkSize  (streaming sentinel)
        b"WAVE",
        b"fmt ",
        16,                  # Subchunk1Size
        1,                   # AudioFormat  PCM
        _CHANNELS,
        _SAMPLE_RATE,
        _BYTE_RATE,
        _BLOCK_ALIGN,
        _BITS,
        b"data",
        _DATA_SIZE,          # Subchunk2Size (streaming sentinel)
    )
    return header + first_chunk


class TelnyxStreamingSTT:
    """
    Non-blocking WebSocket streaming STT client for the Telnyx/Deepgram API.

    Usage:
        stt = TelnyxStreamingSTT()
        stt.connect(api_key="KEY_HERE")
        stt.send_audio(pcm16_bytes)
        result = stt.get_transcript()   # (transcript, is_final) or None
        stt.close()
    """

    def __init__(self):
        self._ws = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._send_queue: "asyncio.Queue[Optional[bytes]]" = None
        self._transcript_queue: queue.Queue = queue.Queue()
        self._connected = threading.Event()
        self._connect_error: Optional[Exception] = None
        self._first_chunk_sent = False
        self._api_key: str = ""

    # ── Public API ────────────────────────────────────────────────────────────

    def connect(self, api_key: str) -> None:
        """
        Open a WebSocket connection in a background thread.
        Blocks until the connection is established (or raises on failure).
        """
        self._api_key = api_key
        self._first_chunk_sent = False
        self._connect_error = None
        self._connected.clear()

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        if not self._connected.wait(timeout=10.0):
            raise TimeoutError("TelnyxStreamingSTT: WebSocket connection timed out.")
        if self._connect_error is not None:
            raise RuntimeError(
                f"TelnyxStreamingSTT: failed to connect: {self._connect_error}"
            ) from self._connect_error

    def send_audio(self, pcm16_bytes: bytes) -> None:
        """
        Send a PCM16-LE audio chunk.  The first call automatically prepends
        the streaming WAV header.
        """
        if self._loop is None or self._send_queue is None:
            raise RuntimeError("Not connected — call connect() first.")

        if not self._first_chunk_sent:
            payload = _build_wav_header(pcm16_bytes)
            self._first_chunk_sent = True
        else:
            payload = pcm16_bytes

        self._loop.call_soon_threadsafe(self._send_queue.put_nowait, payload)

    def get_transcript(self, timeout: float = 0.05) -> Optional[Tuple[str, bool]]:
        """
        Non-blocking retrieval of the next available transcript.

        Returns:
            (transcript: str, is_final: bool) if available within `timeout`.
            None if the queue is empty.
        """
        try:
            return self._transcript_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self, timeout: float = 5.0) -> None:
        """Close the WebSocket connection and stop the background thread."""
        if self._loop and self._send_queue:
            # Signal the sender coroutine to stop
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, None)
        if self._thread:
            self._thread.join(timeout=timeout)
        self._loop = None
        self._thread = None
        self._send_queue = None

    # ── Async internals ───────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Entry point for the background thread: creates and runs the event loop."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_session())
        finally:
            loop.close()
            if self._loop is loop:
                self._loop = None

    async def _ws_session(self) -> None:
        """Manages the full WebSocket lifecycle: connect, send, receive."""
        try:
            import websockets
        except ImportError as exc:
            raise ImportError(
                "websockets library is required: pip install websockets"
            ) from exc

        headers = {"Authorization": f"Bearer {self._api_key}"}
        self._send_queue = asyncio.Queue()

        try:
            async with websockets.connect(_WS_ENDPOINT, additional_headers=headers) as ws:
                self._ws = ws
                self._connected.set()

                sender = asyncio.ensure_future(self._sender(ws))
                receiver = asyncio.ensure_future(self._receiver(ws))

                done, pending = await asyncio.wait(
                    [sender, receiver],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        except Exception as exc:
            self._connect_error = exc
            self._connected.set()
            raise

    async def _sender(self, ws) -> None:
        """Drains the send queue and forwards binary frames to the WebSocket."""
        while True:
            payload = await self._send_queue.get()
            if payload is None:
                # Graceful shutdown signal
                break
            await ws.send(payload)

    async def _receiver(self, ws) -> None:
        """Receives JSON text messages and enqueues parsed transcripts."""
        try:
            async for message in ws:
                if not isinstance(message, str):
                    continue
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue

                transcript, is_final = self._parse_transcript(data)
                if transcript:
                    self._transcript_queue.put((transcript, is_final))
        except Exception:
            return

    @staticmethod
    def _parse_transcript(data: dict) -> Tuple[str, bool]:
        """
        Parse both Telnyx flat format and Deepgram nested format.

        Flat:    {"transcript": "...", "is_final": true}
        Nested:  {"channel": {"alternatives": [{"transcript": "..."}]}, "is_final": true}
        """
        is_final: bool = bool(data.get("is_final", False))

        # Flat format
        transcript = data.get("transcript", "")
        if transcript:
            return transcript.strip(), is_final

        # Deepgram nested format
        channel = data.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if alternatives:
            transcript = alternatives[0].get("transcript", "")
            if transcript:
                return transcript.strip(), is_final

        return "", is_final
