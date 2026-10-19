"""Protocol tests for qwen_sidecar.py, Bolo's offline multilingual MLX sidecar.

Drives the real sidecar script over its line-delimited JSON protocol using
the test interpreter. No MLX, GPU, qwen venv, or network is involved:
the fake-backend seam (``BOLO_QWEN_FAKE_TRANSCRIPT``) replaces the model
load with a canned answer, so these tests exercise the protocol, the
energy gate, the language mapping, and the process lifecycle exactly as
the Rust runtime (``src/qwen_sidecar.rs``) sees them.

The energy-gate tests assert the same constants the Rust pipeline uses
(``SPEECH_FRAME_MS = 20``, ``SPEECH_RMS_THRESHOLD = 0.006``, i16
normalized by 32768): a wav with a loud sine frame goes through to
inference, a near-digital-silence clip is answered empty with
``"speech": false`` without ever touching the model, and a clip whose
only energy lives in a single 20 ms frame still counts as speech,
mirroring Rust's ``SpeechStats::has_speech``.
"""

import json
import math
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR = REPO_ROOT / "qwen_sidecar.py"

sys.path.insert(0, str(REPO_ROOT))
import qwen_sidecar  # noqa: E402

SIDECAR_STARTUP_TIMEOUT = 10.0
SAMPLE_RATE = 16000


class SidecarSession:
    """One spawned sidecar (fake backend) plus bounded line helpers."""

    def __init__(self, fake_text="canned sidecar text", parent_pid=None):
        env = dict(os.environ)
        env["BOLO_QWEN_FAKE_TRANSCRIPT"] = fake_text
        if parent_pid is not None:
            env["BOLO_PARENT_PID"] = str(parent_pid)
        self.process = subprocess.Popen(
            [sys.executable, str(SIDECAR)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

    def write(self, raw_line):
        self.process.stdin.write(raw_line + "\n")
        self.process.stdin.flush()

    def request(self, payload, timeout=SIDECAR_STARTUP_TIMEOUT):
        self.write(json.dumps(payload))
        return self.read_response(timeout)

    def read_response(self, timeout):
        """Read one response line, bounded so a wedged sidecar fails fast."""
        import selectors

        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0, "sidecar produced no response within the timeout"
                if selector.select(remaining):
                    line = self.process.stdout.readline()
                    assert line.strip(), "sidecar stdout closed before a response line"
                    return json.loads(line)
        finally:
            selector.close()

    def close(self):
        if self.process.poll() is None:
            try:
                self.write('{"type": "stop"}')
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def write_wav(path, samples, sample_rate=SAMPLE_RATE):
    """Write a mono 16-bit PCM wav from i16-ish sample values."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for sample in samples:
            clipped = max(-32768, min(32767, int(sample)))
            frames += clipped.to_bytes(2, "little", signed=True)
        handle.writeframes(bytes(frames))


def sine_clip(amplitude, seconds, sample_rate=SAMPLE_RATE, freq=220.0):
    """A tone loud enough to clear the gate when amplitude >= ~0.03."""
    count = int(seconds * sample_rate)
    return [
        amplitude * 32767 * math.sin(2 * math.pi * freq * index / sample_rate)
        for index in range(count)
    ]


def quiet_clip(level, seconds, sample_rate=SAMPLE_RATE):
    """A constant near-silence wav: constant ``level`` i16 value."""
    return [level for _ in range(int(seconds * sample_rate))]


def test_ready_handshake_reports_the_benchmark_model():
    with SidecarSession() as sidecar:
        ready = sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        assert ready == {"type": "ready", "model": "fake"}
        # The production model id is the benchmark model.
        assert (
            qwen_sidecar.MODEL_ID == "mlx-community/Qwen3-ASR-0.6B-bf16"
        )


def test_model_id_resolves_from_the_environment_overlay():
    with SidecarSession() as sidecar:
        ready = sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        assert ready == {"type": "ready", "model": "fake"}
    # The resolved model the child loads: BOLO_QWEN_MODEL beats the
    # benchmark default, and an absent overlay keeps it. The Rust rung
    # passes its configured repo id through exactly this environment
    # variable at spawn time (src/qwen_sidecar.rs QwenSidecar::start).
    env = dict(os.environ)
    env["BOLO_QWEN_MODEL"] = "org/Other-ASR"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import qwen_sidecar; print(qwen_sidecar.configured_model_id())",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=30,
    )
    assert probe.stdout.strip() == "org/Other-ASR"
    env.pop("BOLO_QWEN_MODEL")
    probe_default = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import qwen_sidecar; print(qwen_sidecar.configured_model_id())",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=30,
    )
    assert (
        probe_default.stdout.strip() == "mlx-community/Qwen3-ASR-0.6B-bf16"
    )


def test_transcribe_speech_clip_returns_text_with_matching_id(tmp_path):
    clip = tmp_path / "speech.wav"
    write_wav(clip, sine_clip(amplitude=0.2, seconds=1.0))
    with SidecarSession(fake_text="canned sidecar text") as sidecar:
        sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        result = sidecar.request(
            {"type": "transcribe", "id": 41, "wav_path": str(clip), "language": "en-IN"}
        )
        assert result == {
            "type": "result",
            "id": 41,
            "text": "canned sidecar text",
            "speech": True,
            "language": "English",
        }


def test_transcribe_without_a_language_answer_without_the_language_key(tmp_path):
    clip = tmp_path / "speech.wav"
    write_wav(clip, sine_clip(amplitude=0.2, seconds=1.0))
    with SidecarSession(fake_text="hi text") as sidecar:
        sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        result = sidecar.request({"type": "transcribe", "id": 8, "wav_path": str(clip)})
        assert result == {
            "type": "result",
            "id": 8,
            "text": "hi text",
            "speech": True,
        }


def test_energy_gate_closes_on_digital_silence(tmp_path):
    """Silence is answered without inference: empty text, speech False."""
    clip = tmp_path / "silence.wav"
    write_wav(clip, quiet_clip(level=20, seconds=1.0))
    with SidecarSession() as sidecar:
        sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        result = sidecar.request(
            {"type": "transcribe", "id": 7, "wav_path": str(clip), "language": "hi"}
        )
        assert result == {"type": "result", "id": 7, "text": "", "speech": False}


def test_single_loud_frame_counts_as_speech_mirroring_rust(tmp_path):
    """Rust counts speech at the first frame above threshold; one 20 ms
    loud frame inside a quiet clip must clear the Python gate too."""
    seconds = 1.0
    samples = quiet_clip(level=10, seconds=seconds)
    frame_len = (SAMPLE_RATE * qwen_sidecar.SPEECH_FRAME_MS) // 1000
    for index in range(frame_len):
        samples[index + 5 * frame_len] = int(0.2 * 32767)
    clip = tmp_path / "one-frame.wav"
    write_wav(clip, samples)
    with SidecarSession(fake_text="one frame heard") as sidecar:
        sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        result = sidecar.request(
            {"type": "transcribe", "id": 9, "wav_path": str(clip), "language": "auto"}
        )
        assert result["speech"] is True
        assert result["text"] == "one frame heard"


def test_missing_wav_answers_an_error_and_the_loop_survives(tmp_path):
    with SidecarSession() as sidecar:
        sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        error = sidecar.request(
            {"type": "transcribe", "id": 1, "wav_path": str(tmp_path / "nope.wav")}
        )
        assert error["type"] == "error"
        assert error["id"] == 1
        assert error["message"]
        # The loop survived: the next request still serves.
        clip = tmp_path / "speech.wav"
        write_wav(clip, sine_clip(amplitude=0.2, seconds=0.5))
        ok = sidecar.request({"type": "transcribe", "id": 2, "wav_path": str(clip)})
        assert ok["type"] == "result"


def test_malformed_lines_are_skipped_without_a_response(tmp_path):
    clip = tmp_path / "speech.wav"
    write_wav(clip, sine_clip(amplitude=0.2, seconds=0.5))
    with SidecarSession() as sidecar:
        sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        sidecar.write("this is not json")
        sidecar.write('{"type": "transcribe", "id": 3, "wav_path": ')
        result = sidecar.request({"type": "transcribe", "id": 4, "wav_path": str(clip)})
        assert result == {
            "type": "result",
            "id": 4,
            "text": "canned sidecar text",
            "speech": True,
        }


def test_unknown_request_type_answers_error_and_survives():
    with SidecarSession() as sidecar:
        sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        error = sidecar.request({"type": "orbital_laser"})
        assert error["type"] == "error"
        assert "orbital_laser" in error["message"]


def test_stop_request_exits_zero():
    with SidecarSession() as sidecar:
        sidecar.read_response(SIDECAR_STARTUP_TIMEOUT)
        sidecar.write('{"type": "stop"}')
        code = sidecar.process.wait(timeout=10)
        assert code == 0


def test_sidecar_exits_when_parent_pid_is_dead():
    """Orphan protection: a sidecar whose parent died exits instead of
    lingering with its resident model."""
    # A pid that is guaranteed dead (reaped): spawn and wait out a leaf.
    gone = subprocess.Popen(["/bin/sleep", "0"])
    gone.wait()
    with SidecarSession(parent_pid=gone.pid) as sidecar:
        # stdin open, parent dead: the next request's read-loop pass must
        # break and exit, without answering the request.
        sidecar.write('{"type": "transcribe", "id": 1, "wav_path": "/nowhere.wav"}')
        code = sidecar.process.wait(timeout=15)
        assert code == 0


def test_read_wav_returns_i16_samples_and_rejects_stereo(tmp_path):
    clip = tmp_path / "mono.wav"
    write_wav(clip, [1000, -1000, 12345])
    samples, sample_rate = qwen_sidecar.read_wav_mono_i16(clip)
    assert list(samples) == [1000, -1000, 12345]
    assert sample_rate == SAMPLE_RATE

    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00" * 8)
    with pytest.raises(ValueError):
        qwen_sidecar.read_wav_mono_i16(stereo)


def test_frame_rms_series_matches_the_rust_gate_math():
    # A 220 Hz sine at amplitude 0.2*32767: RMS over any full frame is
    # amplitude/sqrt(2) = 0.1414, far above the 0.006 threshold.
    samples = sine_clip(amplitude=0.2, seconds=0.1)
    series = qwen_sidecar.frame_rms_series(samples, SAMPLE_RATE)
    assert len(series) == 5  # 0.1 s at 20 ms frames
    assert all(abs(rms - 0.2 / math.sqrt(2)) < 0.01 for rms in series)
    assert qwen_sidecar.has_speech(series)

    # Digital silence (constant 20) sits below the threshold.
    quiet = qwen_sidecar.frame_rms_series(
        quiet_clip(level=20, seconds=0.1), SAMPLE_RATE
    )
    assert all(rms < qwen_sidecar.SPEECH_RMS_THRESHOLD for rms in quiet)
    assert not qwen_sidecar.has_speech(quiet)

    # Empty input or a zero rate frame to nothing, never speech.
    assert qwen_sidecar.frame_rms_series([], SAMPLE_RATE) == []
    assert not qwen_sidecar.has_speech([])


def test_constants_mirror_the_rust_pipeline():
    assert qwen_sidecar.SPEECH_FRAME_MS == 20
    assert qwen_sidecar.SPEECH_RMS_THRESHOLD == 0.006
    assert qwen_sidecar.PARENT_PID >= 0


def test_qwen_language_maps_bolo_hints_onto_model_names():
    # English family.
    assert qwen_sidecar.qwen_language("en-IN") == "English"
    assert qwen_sidecar.qwen_language("en-US") == "English"
    assert qwen_sidecar.qwen_language("en") == "English"
    assert qwen_sidecar.qwen_language(" EN ") == "English"
    assert qwen_sidecar.qwen_language("en_GB") == "English"
    # Hindi family.
    assert qwen_sidecar.qwen_language("hi") == "Hindi"
    assert qwen_sidecar.qwen_language("hi-IN") == "Hindi"
    # Auto-detect and the rest map to None.
    assert qwen_sidecar.qwen_language("auto") is None
    assert qwen_sidecar.qwen_language("") is None
    assert qwen_sidecar.qwen_language("de-DE") is None
    assert qwen_sidecar.qwen_language(None) is None


def test_language_and_context_fields_reach_the_model(tmp_path, monkeypatch):
    """The request fields ride straight into transcribe(): in-process
    check against the fake backend, which records each call."""
    clip = tmp_path / "speech.wav"
    write_wav(clip, sine_clip(amplitude=0.2, seconds=0.5))
    monkeypatch.setenv("BOLO_QWEN_FAKE_TRANSCRIPT", "recorded")
    sidecar = qwen_sidecar.Sidecar()
    response = sidecar.handle_transcribe(
        {
            "type": "transcribe",
            "id": 5,
            "wav_path": str(clip),
            "language": "hi-IN",
            "context": "Vocabulary: Quilter, apostle.",
        }
    )
    assert response == {
        "type": "result",
        "id": 5,
        "text": "recorded",
        "speech": True,
        "language": "Hindi",
    }
    assert sidecar.model.calls == [
        ("Hindi", "Vocabulary: Quilter, apostle.")
    ]


def test_bad_context_type_is_ignored_not_fatal(tmp_path, monkeypatch):
    """A non-string context (an old runtime sending junk) is dropped
    before inference instead of poisoning the request."""
    clip = tmp_path / "speech.wav"
    write_wav(clip, sine_clip(amplitude=0.2, seconds=0.5))
    monkeypatch.setenv("BOLO_QWEN_FAKE_TRANSCRIPT", "recorded")
    sidecar = qwen_sidecar.Sidecar()
    response = sidecar.handle_transcribe(
        {
            "type": "transcribe",
            "id": 6,
            "wav_path": str(clip),
            "context": ["not", "a", "string"],
        }
    )
    assert response["type"] == "result"
    assert response["speech"] is True
    assert sidecar.model.calls == [(None, None)]
