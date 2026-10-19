#!/usr/bin/env python3
"""Qwen3-ASR MLX sidecar: the multilingual rung of Bolo's offline STT ladder.

One persistent process owns the local qwen3-asr-0.6B model (loaded once,
lazily, through the qwen3-asr-mlx package on Apple Silicon Metal), and
answers line-delimited JSON transcribe requests for the rest of the
session. The Rust runtime (``src/qwen_sidecar.rs``) spawns this script
with the qwen venv interpreter (``~/.bolo/qwen-venv/bin/python3`` or
``BOLO_QWEN_PYTHON``), reads ``{"type":"ready",...}`` off stdout
as the health-check handshake, then sends one request per dictation.
The protocol mirrors ``parakeet_sidecar.py`` exactly, plus two optional
transcribe fields the qwen model actually uses:

* ``language``: Bolo's configured hint (``en-IN``, ``hi``, ``auto``);
  the sidecar maps it onto qwen's language names (``en*`` -> ``English``,
  ``hi*`` -> ``Hindi``, anything else -> ``None``, qwen's auto-detect).
* ``context``: a free-form vocabulary string passed straight to
  ``transcribe(context=...)`` (qwen places it in the system prompt).

Line-delimited JSON on stdin (requests) and stdout (responses), exactly
one response line per request:

* after the model finishes loading:
  ``{"type":"ready","model":"mlx-community/Qwen3-ASR-0.6B-bf16"}`` (or
  ``"model":"fake"`` under the test seam below, or the ``BOLO_QWEN_MODEL``
  override when the Rust rung points at a different snapshot)
* ``{"type":"transcribe","id":N,"wav_path":"..."}`` ->
  ``{"type":"result","id":N,"text":"...","speech":true,"language":...}``
  (``language`` is the output text language the model reports; the Rust
  exchange currently consumes only ``text``)
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
before STT, and qwen3-asr is autoregressive like whisper, so without the
gate it invents words on silent clips. The gate reads the wav with the
stdlib ``wave`` module only, so this file imports nothing heavy before
the model load.

Weights download on first use through ``huggingface_hub.snapshot_download``
(the same HF cache parakeet rides): a warm cache resolves instantly, a
cold machine downloads the ~1.2 GB snapshot once, and the ready handshake
bounds the whole load (the Rust rung abandons a wedged child and retries
on the next dictation).

Test seam: when ``BOLO_QWEN_FAKE_TRANSCRIPT`` is set, the model load is
replaced by a stub that returns that canned string and records the
``language``/``context`` arguments it was called with. Used only by the
protocol test suite (``tests/test_qwen_sidecar.py``) so it can drive the
real request loop without MLX installed; production runs never set it.

Why this rung exists (benchmark 2026-10-18,
~/Projects/bolo-model-bench/RESULTS.md): qwen3-asr-0.6B measured 52.0%
WER on conversational Hindi with native Devanagari output (best of the
field; whisper-small 60.4%, large-v3-turbo 68.5%, parakeet romanizes and
is disqualified), English content accuracy on par with
whisper-large-v3-turbo on real sentences, 0.166 s warm per clip (near
parakeet's 0.116 s), ~0.34 s load from the HF snapshot, Apache-2.0.

Memory notes (benchmark 2026-10-18, M4 Max): the resident child lazily
mmaps the ~1.2 GB bf16 weights; the sidecar exits when its parent Rust
runtime goes away (``BOLO_PARENT_PID``, mirroring accessibility_daemon.py)
or when stdin closes, and frees the model (``model.close()``) on every
exit path.
"""

import array
import json
import math
import os
import sys
import wave

# The benchmark model (mlx-community/Qwen3-ASR-0.6B-bf16, ~1.2 GB,
# Apache-2.0). The Rust rung passes its configured repo id through
# BOLO_QWEN_MODEL at spawn time; direct runs use this default.
MODEL_ID = "mlx-community/Qwen3-ASR-0.6B-bf16"

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


def configured_model_id():
    """The model this run loads: BOLO_QWEN_MODEL override, else default."""
    override = os.environ.get("BOLO_QWEN_MODEL", "").strip()
    return override or MODEL_ID


def qwen_language(hint):
    """Map Bolo's language hint onto qwen's language names.

    ``en*`` -> ``"English"``, ``hi*`` -> ``"Hindi"``; everything else
    (``auto``, empty, unknown codes) maps to ``None``, which is qwen's
    auto-detect. The Rust rung only engages this sidecar for hints it
    knows qwen supports, so unreachable names are the model's business.
    """
    if not isinstance(hint, str):
        return None
    base = hint.strip().lower().replace("_", "-")
    primary = base.split("-")[0] if base else ""
    if primary == "en":
        return "English"
    if primary == "hi":
        return "Hindi"
    return None


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
    """Test seam: an object with the qwen3-asr transcribe surface.

    Records every (language, context) pair it is called with so the
    protocol tests can assert the request fields reach the model.
    """

    class FakeResult:
        def __init__(self, text, language):
            self.text = text
            self.language = language

    class FakeModel:
        def __init__(self):
            self.calls = []

        def transcribe(self, wav_path, language=None, context=None):
            self.calls.append((language, context))
            return FakeResult(reply_text, language)

        def close(self):
            pass

    return FakeModel()


def load_model():
    """Load qwen once at startup: fake seam under tests, else the MLX
    model resolved from the HF cache (downloading on first use)."""
    fake_reply = os.environ.get("BOLO_QWEN_FAKE_TRANSCRIPT")
    if fake_reply is not None:
        return make_fake_model(fake_reply), "fake"
    model_id = configured_model_id()
    from huggingface_hub import snapshot_download

    path = snapshot_download(model_id)
    from qwen3_asr_mlx import Qwen3ASR

    model = Qwen3ASR.from_pretrained(path)
    model.warm_up()
    return model, model_id


def respond(response):
    sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def log(message):
    print(f"[qwen] {message}", file=sys.stderr, flush=True)


class Sidecar:
    def __init__(self):
        self.model, self.model_id = load_model()

    def close(self):
        """Free the resident weights on every exit path, best effort."""
        close = getattr(self.model, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as error:  # noqa: BLE001 - never block an exit
            log(f"model close failed: {error}")

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
        language = qwen_language(request.get("language"))
        context = request.get("context")
        if not isinstance(context, str):
            context = None
        try:
            result = self.model.transcribe(
                wav_path, language=language, context=context
            )
            text = result.text
            output_language = getattr(result, "language", None)
        except Exception as error:  # noqa: BLE001 - one error reply, loop lives on
            log(f"transcribe failed for {wav_path}: {error}")
            return {"type": "error", "id": request_id, "message": str(error)}
        response = {"type": "result", "id": request_id, "text": text, "speech": True}
        if isinstance(output_language, str) and output_language:
            response["language"] = output_language
        return response

    def handle(self, request):
        kind = request.get("type")
        if kind == "transcribe":
            return self.handle_transcribe(request)
        return {"type": "error", "message": f"unknown request type: {kind!r}"}


def main() -> int:
    sidecar = Sidecar()
    respond({"type": "ready", "model": sidecar.model_id})
    try:
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
    finally:
        sidecar.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
