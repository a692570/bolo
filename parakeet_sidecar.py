#!/usr/bin/env python3
"""Parakeet MLX sidecar: the top rung of Bolo's offline STT ladder.

One persistent process owns the local parakeet-tdt-0.6b-v3 model (loaded
once, lazily, through the parakeet-mlx package on Apple Silicon Metal),
and answers line-delimited JSON transcribe requests for the rest of the
session. The Rust runtime (``src/parakeet_sidecar.rs``) spawns this script
with the parakeet venv interpreter (``~/.bolo/parakeet-venv/bin/python3``
or ``BOLO_PARAKEET_PYTHON``), reads ``{"type":"ready",...}`` off stdout
as the health-check handshake, then sends one request per dictation.

Protocol: line-delimited JSON on stdin (requests) and stdout (responses),
exactly one response line per request:

* after the model finishes loading:
  ``{"type":"ready","model":"mlx-community/parakeet-tdt-0.6b-v3"}`` (or
  ``"model":"fake"`` under the test seam below)
* ``{"type":"transcribe","id":N,"wav_path":"..."}`` ->
  ``{"type":"result","id":N,"text":"...","speech":true}``
* a clip whose energy never reaches the speech threshold is answered
  without inference: ``{"type":"result","id":N,"text":"","speech":false}``
* an unreadable wav answers
  ``{"type":"error","id":N,"message":"..."}``
* ``{"type":"stop"}`` -> exits 0

Unknown request types answer ``{"type":"error","message":...}`` and the
loop continues; malformed lines are skipped with no response. Stdout
carries only protocol responses; everything else goes to stderr.

The energy gate mirrors Bolo's Rust ``speech_stats`` exactly (the same
20 ms frames, the same RMS normalization, the same 0.006 threshold) as
defense in depth: the Rust pipeline already drops no-speech recordings
before STT, and laneG measured that parakeet invents words on 12/12
silent/noise clips when that gate is missing. The gate reads the wav
with the stdlib ``wave`` module only, so this file imports nothing heavy
before the model load.

Test seam: when ``BOLO_PARAKEET_FAKE_TRANSCRIPT`` is set, the model load
is replaced by a stub that returns that canned string. Used only by the
protocol test suite (``tests/test_parakeet_sidecar.py``) so it can drive
the real request loop without MLX installed; production runs never set it.

The model is English-trained; parakeet emits English-shaped text for
non-English audio (laneG: a Devanagari clip came back as "Jeff."), so the
Rust rung only consults this sidecar for English language hints.

Memory notes (laneG, 2026-10-17, M4 Max): warm HF-cache load (cold start
to first transcript) is ~0.755 s, ~118 MB RSS after load because MLX
lazily mmaps the 2.4 GB weights, ~1.6 GB peak RSS while transcribing,
0.086 s median per clip. The sidecar exits when its parent Rust runtime
goes away (``BOLO_PARENT_PID``, mirroring accessibility_daemon.py) or when
stdin closes.
"""

import array
import json
import math
import os
import sys
import wave

MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v3"

# Mirror of Bolo's Rust speech gate (src/main.rs): one RMS value per
# SPEECH_FRAME_MS frame over samples normalized to [-1, 1], and a clip
# has speech when any single frame reaches SPEECH_RMS_THRESHOLD. Any
# change to the Rust constants must be mirrored here (and vice versa).
SPEECH_FRAME_MS = 20
SPEECH_RMS_THRESHOLD = 0.006
I16_FULL_SCALE = 32768.0

PARENT_PID = int(os.environ.get("BOLO_PARENT_PID") or "0")


def parent_is_alive():
    if PARENT_PID <= 0:
        return True
    try:
        os.kill(PARENT_PID, 0)
    except OSError:
        return False
    return True


def read_wav_mono_i16(path):
    """Read one 16-bit PCM wav as ``(samples, sample_rate)``.

    Assumes mono input; a multi-channel file raises ValueError, matching
    the Rust rung, which always writes a mono 16 kHz file.
    """
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("wav must be 16-bit PCM")
        if handle.getnchannels() != 1:
            raise ValueError("wav must be mono")
        sample_rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        # WAV data is little-endian; array.frombytes is native-order.
        samples.byteswap()
    return samples, sample_rate


def frame_rms_series(samples, sample_rate):
    """One RMS value per 20 ms frame, the Rust gate's exact framing."""
    if not samples or sample_rate <= 0:
        return []
    frame_len = max(1, (sample_rate * SPEECH_FRAME_MS) // 1000)
    series = []
    max_i = len(samples)
    for start in range(0, max_i, frame_len):
        frame = samples[start : min(start + frame_len, max_i)]
        if not frame:
            break
        square_sum = 0.0
        for sample in frame:
            value = sample / I16_FULL_SCALE
            square_sum += value * value
        series.append(math.sqrt(square_sum / len(frame)))
    return series


def has_speech(series):
    """True when any frame reaches the threshold, matching Rust has_speech."""
    return any(rms >= SPEECH_RMS_THRESHOLD for rms in series)


def make_fake_model(reply_text):
    """Test seam: an object with the parakeet-mlx transcribe surface."""

    class FakeResult:
        text = reply_text

    class FakeModel:
        def transcribe(self, wav_path):
            return FakeResult()

    return FakeModel()


def load_model():
    """Load parakeet once at startup; the canned seam under tests."""
    fake_reply = os.environ.get("BOLO_PARAKEET_FAKE_TRANSCRIPT")
    if fake_reply is not None:
        return make_fake_model(fake_reply), "fake"
    import parakeet_mlx as pm

    return pm.from_pretrained(MODEL_ID), MODEL_ID


def respond(response):
    sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def log(message):
    print(f"[parakeet] {message}", file=sys.stderr, flush=True)


class Sidecar:
    def __init__(self):
        self.model, self.model_id = load_model()

    def handle_transcribe(self, request):
        request_id = request.get("id")
        wav_path = request.get("wav_path")
        if not isinstance(wav_path, str) or not wav_path:
            return {
                "type": "error",
                "id": request_id,
                "message": "transcribe requires a wav_path",
            }
        try:
            samples, sample_rate = read_wav_mono_i16(wav_path)
        except Exception as error:  # noqa: BLE001 - one error reply, loop lives on
            log(f"wav read failed for {wav_path}: {error}")
            return {"type": "error", "id": request_id, "message": str(error)}
        if not has_speech(frame_rms_series(samples, sample_rate)):
            log(f"energy gate closed for {wav_path}; skipping inference")
            return {"type": "result", "id": request_id, "text": "", "speech": False}
        try:
            text = self.model.transcribe(wav_path).text
        except Exception as error:  # noqa: BLE001 - one error reply, loop lives on
            log(f"transcribe failed for {wav_path}: {error}")
            return {"type": "error", "id": request_id, "message": str(error)}
        return {"type": "result", "id": request_id, "text": text, "speech": True}

    def handle(self, request):
        kind = request.get("type")
        if kind == "transcribe":
            return self.handle_transcribe(request)
        return {"type": "error", "message": f"unknown request type: {kind!r}"}


def main() -> int:
    sidecar = Sidecar()
    respond({"type": "ready", "model": sidecar.model_id})
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        if not parent_is_alive():
            break
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            log(f"malformed request line skipped: {line[:120]}")
            continue
        if not isinstance(request, dict):
            continue
        if request.get("type") == "stop":
            return 0
        try:
            respond(sidecar.handle(request))
        except Exception as error:  # noqa: BLE001 - unexpected handler bugs
            respond({"type": "error", "message": f"{type(error).__name__}: {error}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
