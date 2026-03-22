#!/usr/bin/env python3
"""
Bolo — Telnyx voice dictation menubar app.
Hold Right Option anywhere to dictate. Release to transcribe and inject.
"""

import concurrent.futures
import io
import json
import os
import re
import signal
import subprocess
import threading
import time
import traceback
import wave

import collections
import numpy as np
import requests
import rumps
import sounddevice as sd
from commands import parse_command
from corrections import CorrectionStore
from overlay_controller import RecordingOverlay
from stt import SilenceDetector, TelnyxStreamingSTT
from transcript_state import TranscriptState, longest_common_prefix, merge_transcript
from vocabulary import VocabularyStore

from AppKit import (
    NSPasteboard,
    NSPasteboardTypeString,
    NSWorkspace,
)

from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSetFlags,
    CGEventSourceFlagsState,
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetFlags,
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    kCFRunLoopDefaultMode,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGHIDEventTap,
    kCGEventSourceStateCombinedSessionState,
    kCGEventTapOptionListenOnly,
    kCGEventFlagsChanged,
)
import HIServices

# ── Config ────────────────────────────────────────────────────────────────────

def _load_env_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value

    env_file = os.path.expanduser("~/.codex/.env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, raw = line.split("=", 1)
                    if key.strip() == name:
                        return raw.strip().strip("\"'")
        except OSError:
            pass

    shell_file = os.path.expanduser("~/.zshrc")
    if os.path.exists(shell_file):
        try:
            with open(shell_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith(f"export {name}="):
                        continue
                    return line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            pass

    return ""


TELNYX_API_KEY = _load_env_value("TELNYX_API_KEY")
STT_ENDPOINT   = "https://api.telnyx.com/v2/ai/audio/transcriptions"
LLM_ENDPOINT   = "https://api.telnyx.com/v2/ai/chat/completions"
SAMPLE_RATE    = 16000
CHANNELS       = 1
CORRECTION_WINDOW_SECONDS = 3.0
STREAM_DRAIN_SECONDS = 0.15
_SILENCE_PADDING = bytes(int(16000 * 0.35 * 2))  # 350ms of silence at 16kHz mono 16-bit
LLM_CLEANUP_MODE = _load_env_value("BOLO_LLM_CLEANUP").strip().lower() or "auto"
DELETE_KEYCODE = 51
RATE_LIMIT_BACKOFF_SECONDS = 45.0
MAX_RECORDING_SECONDS = 90.0  # force-stop if stuck recording longer than this
AUTO_SILENCE_SECONDS = 5.0    # stop after this many seconds of silence
AUTO_SILENCE_MAX_SECONDS = 5.0  # flat threshold — no extension logic
AUTO_SILENCE_EXTEND_STEP = 2.0  # unused, kept for reference
AUTO_SILENCE_MIN_SPEAKING = 2.0  # only trigger auto-stop after user has been speaking this long
BOLO_PREFS_FILE = os.path.expanduser("~/.bolo_prefs.json")

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
ICON_IDLE        = os.path.join(BASE_DIR, "icon_idle.png")
ICON_REC         = os.path.join(BASE_DIR, "icon_recording.png")
CORRECTIONS_FILE = os.path.expanduser("~/.bolo_corrections.json")
BUILT_IN_VOCAB_FILE = os.path.join(BASE_DIR, "vocabulary.json")
USER_VOCAB_FILE = os.path.expanduser("~/.bolo_vocabulary.json")
CORRECTION_STORE = CorrectionStore(CORRECTIONS_FILE)
VOCAB_STORE = VocabularyStore(BUILT_IN_VOCAB_FILE, USER_VOCAB_FILE)
CODE_APPS = {
    "Code",
    "Visual Studio Code",
    "Cursor",
    "Xcode",
    "Terminal",
    "iTerm2",
    "Warp",
}

SYSTEM_PROMPT = (
    "You are a transcription formatter. "
    "Your only job is to apply minimal capitalization and punctuation fixes to a raw speech transcript. "
    "Do not rewrite meaning. Do not summarize. Do not add or remove claims. "
    "If the input is already good, return it unchanged. "
    "Remove filler words and verbal tics that add no meaning: "
    "'um', 'uh', 'hmm', 'like' when used as filler (not as a meaningful word), "
    "'you know', 'I mean', 'sort of', 'kind of', 'basically', 'literally' when used as filler, "
    "'and all', 'and everything', 'right?' at end of sentences when rhetorical. "
    "Be conservative: only remove when the word is clearly filler with no semantic value. "
    "Do not remove 'like' when it means 'similar to' or has real meaning. "
    "Also fix these known brand name and term misrecognitions when context makes them obvious: "
    "'whisper flow', 'wisper flow', 'Voisei', or 'Voisey' -> 'Wispr Flow'; "
    "'telnyx' or close variants -> 'Telnyx'; "
    "'bolo' -> 'Bolo'; "
    "'remotion', 'emotion', 'emotions' (when referring to a framework) -> 'Remotion'; "
    "'nova three' or 'nova 3' (when referring to a model) -> 'nova-3'; "
    "'Quen', 'Queue When', or 'Kyuen' (when referring to the AI model) -> 'Qwen'. "
    "Output only the cleaned transcript."
)

RECONCILE_PROMPT = (
    "You reconcile two speech transcripts of the same utterance. "
    "Choose the more accurate wording or combine them conservatively. "
    "Do not add facts that are not present in either transcript. "
    "Prefer the more complete ending, better grammar only when clearly supported, and preserve the speaker's meaning. "
    "Output only the final transcript."
)

KNOWN_TERM_PATTERNS = (
    (re.compile(r"\bwhisper flow\b|\bwhisper of four\b|\bwisper flow\b|\bvoisei\b|\bvoisey\b", re.IGNORECASE), "Wispr Flow"),
    (re.compile(r"\btelnyx\b|\btelenix\b|\btennis\b|\btennix\b", re.IGNORECASE), "Telnyx"),
    (re.compile(r"\bbolo\b|\bbollo\b", re.IGNORECASE), "Bolo"),
    (re.compile(r"\bremotion\b|\bemotion\b|\bemotions\b|\bmotion\b", re.IGNORECASE), "Remotion"),
    (re.compile(r"\bgrokwise\b|\bcrocawise\b|\bgrok wise\b|\bcroca wise\b", re.IGNORECASE), "Grokwise"),
    (re.compile(r"\bnova[ -]three\b|\bnova 3\b", re.IGNORECASE), "nova-3"),
    (re.compile(r"\bquen\b|\bqueue when\b|\bkyuen\b|\bkwan\b", re.IGNORECASE), "Qwen"),
    (re.compile(r"\bsonne\b|\bsonet\b|\bsonnet\b", re.IGNORECASE), "Sonnet"),
    (re.compile(r"\bkimi\s*k\s*2\.?5\b|\bkimi\s*k2\.?5\b", re.IGNORECASE), "Kimi K2.5"),
    (re.compile(r"\bclaude\s+sony\b|\bclaude\s+sonne\b|\bclaude\s+sonet\b", re.IGNORECASE), "Claude Sonnet"),
)

# Filler patterns applied as a pre-LLM cleanup pass.
# Ordered from most to least greedy. Each entry: (compiled regex, replacement).
FILLER_PATTERNS = [
    # Standalone hesitation sounds at the start, middle, or end of a phrase
    (re.compile(r'\b(um+|uh+|hmm+|mhm)\b[,.]?\s*', re.IGNORECASE), ''),
    # "you know" as filler
    (re.compile(r'\byou know[,.]?\s*', re.IGNORECASE), ''),
    # "and all" only at end of phrase (not "and all of ...")
    (re.compile(r'\band all\b(?!\s+of)\b[,.]?\s*$', re.IGNORECASE), ''),
    # trailing rhetorical "right?" or ", right?"
    (re.compile(r',?\s*\bright\??\s*$', re.IGNORECASE), ''),
]

# ── Bolo app ──────────────────────────────────────────────────────────────────


class BoloApp(rumps.App):
    def __init__(self):
        super().__init__("Bolo", icon=ICON_IDLE, title="⌥", template=True, quit_button=None)

        self.recording    = False
        self.audio_frames = []
        self.lock         = threading.Lock()
        self.last_error   = None
        self.last_pipeline = 0.0

        self.menu = [
            rumps.MenuItem("Bolo — Voice Dictation", callback=None),
            None,
            rumps.MenuItem("Hold Right Option to dictate", callback=None),
            None,
            rumps.MenuItem("Last transcript", callback=self._copy_last),
            rumps.MenuItem("History", callback=None),
            None,
            rumps.MenuItem("Auto-stop on silence", callback=self._toggle_auto_silence),
            None,
            rumps.MenuItem("Quit Bolo", callback=self.quit_app),
        ]
        self.menu["Bolo — Voice Dictation"].set_callback(None)
        self.menu["Hold Right Option to dictate"].set_callback(None)
        self.menu["History"].set_callback(None)
        self.last_result     = None
        self.last_raw        = None
        self.last_paste_time = 0.0
        self._rate_limit_backoff_until = 0.0
        self.correction_window_until = 0.0
        self.correction_mode = False
        self.session_history = collections.deque(maxlen=10)
        self.overlay = RecordingOverlay(BASE_DIR)
        self._overlay_hide_timer = None
        self._session_seq = 0
        self._active_session_id = 0
        self._session_phase = "idle"

        # Auto-silence feature (Wispr Flow parity)
        prefs = self._load_prefs()
        self._auto_silence_enabled = prefs.get("auto_silence_enabled", True)
        self._update_auto_silence_menu()

        # Streaming STT state
        self._stt            = None
        self._transcript_state = None
        self._transcript_lock = threading.Lock()
        self._warm_stt = None
        self._warm_stt_lock = threading.Lock()
        self._warm_stt_connecting = False
        self._warm_stt_connected_at: float = 0.0
        self._silence        = SilenceDetector()
        self._silence_event  = threading.Event()
        self._chunk_time     = time.time()
        self._record_started_at = 0.0
        self._stream_connected_at = None
        self._last_overlay_preview_at = 0.0
        self._overlay_stall_notice_shown = False

        self.stream = None  # opened only during recording
        self._current_context = ""
        self._context_aware = True
        self._ropt_held = False
        self._tap_loop  = None
        self._tap_ref   = None
        self._key_event = None  # "press" or "release" set by callback
        self._tap_heartbeat = 0
        self._tap_last_heartbeat = 0
        self._tap_thread_obj = None
        self._last_press_at = 0.0

        # Start global hotkey listener via CGEventTap (no pynput)
        self._start_event_tap()
        # Poll key events and watchdog on main thread
        rumps.Timer(self._process_key_events, 0.02).start()
        rumps.Timer(self._watchdog_tap, 2).start()
        rumps.Timer(self._watchdog_overlay_preview, 0.5).start()
        rumps.Timer(self._watchdog_recording, 5).start()
        self._ensure_warm_stream()

    # ── Prefs ─────────────────────────────────────────────────────────────────

    def _load_prefs(self):
        try:
            with open(BOLO_PREFS_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_prefs(self, prefs):
        try:
            with open(BOLO_PREFS_FILE, "w", encoding="utf-8") as fh:
                json.dump(prefs, fh, indent=2)
        except OSError as e:
            self._log(f"[prefs] failed to save: {e}")

    def _update_auto_silence_menu(self):
        label = "Auto-stop on silence " + ("✓" if self._auto_silence_enabled else "")
        self.menu["Auto-stop on silence"].title = label.strip()

    def _toggle_auto_silence(self, _):
        self._auto_silence_enabled = not self._auto_silence_enabled
        self._update_auto_silence_menu()
        prefs = self._load_prefs()
        prefs["auto_silence_enabled"] = self._auto_silence_enabled
        self._save_prefs(prefs)
        self._log(f"[silence] auto-stop {'enabled' if self._auto_silence_enabled else 'disabled'}")

    def _begin_session(self):
        self._session_seq += 1
        self._active_session_id = self._session_seq
        self._session_phase = "recording"
        return self._active_session_id

    def _is_current_session(self, session_id):
        return session_id == self._active_session_id

    def _set_session_phase(self, phase, session_id=None):
        if session_id is not None and not self._is_current_session(session_id):
            return False
        self._session_phase = phase
        return True

    def _normalize_transcript_text(self, text):
        text = (text or "").strip()
        if not text:
            return ""
        text = re.sub(r"([.!?])([A-Za-z])", r"\1 \2", text)
        text = re.sub(r"([,;:])([A-Za-z])", r"\1 \2", text)
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _canonicalize_known_terms(self, text):
        text = (text or "").strip()
        if not text:
            return ""
        for pattern, replacement in KNOWN_TERM_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def _remove_fillers(self, text):
        """Strip isolated filler words/sounds that add no semantic value."""
        text = (text or "").strip()
        if not text:
            return ""
        for pattern, replacement in FILLER_PATTERNS:
            text = pattern.sub(replacement, text)
        # Clean up whitespace and punctuation artifacts left by removals
        text = re.sub(r'  +', ' ', text)         # collapse double spaces
        text = re.sub(r'^\s*[,;]\s*', '', text)  # leading comma/semicolon
        text = re.sub(r'\s+([.,!?;:])', r'\1', text)  # space before punctuation
        return text.strip()

    # ── Context awareness ─────────────────────────────────────────────────────

    def _get_focused_text_context(self) -> str:
        """Read the last 500 chars of the currently focused text field via accessibility API."""
        try:
            focused_system = HIServices.AXUIElementCreateSystemWide()
            err, focused_el = HIServices.AXUIElementCopyAttributeValue(
                focused_system, "AXFocusedUIElement", None
            )
            if err != 0 or focused_el is None:
                return ""
            err, value = HIServices.AXUIElementCopyAttributeValue(
                focused_el, "AXValue", None
            )
            if err != 0 or not value:
                return ""
            text = str(value)
            return text[-500:].strip() if len(text) > 500 else text.strip()
        except Exception:
            return ""

    def _apply_context_capitalization(self, text: str, context: str) -> str:
        """Lower the first character of text if the context indicates we are mid-sentence."""
        if not text or not context:
            return text
        context_stripped = context.rstrip()
        if not context_stripped:
            return text
        last_char = context_stripped[-1]
        # Mid-sentence indicators: comma, colon, semicolon, or no ending punctuation at all
        sentence_enders = {".", "!", "?"}
        if last_char not in sentence_enders:
            # We are mid-sentence: do not capitalize the first word
            return text[0].lower() + text[1:]
        return text

    # ── Audio ─────────────────────────────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status):
        if not self.recording:
            return
        self.audio_frames.append(indata.copy())
        pcm = indata.tobytes()
        # Stream to WebSocket
        if self._stt:
            try:
                self._stt.send_audio(pcm)
            except Exception:
                pass
        # Silence detection
        now = time.time()
        elapsed = now - self._chunk_time
        self._chunk_time = now
        result = self._silence.process(pcm, elapsed)
        if result == "end_of_utterance":
            self._silence_event.set()

    def _to_wav_bytes(self, audio):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return buf.getvalue()

    # ── Hotkey (CGEventTap) ────────────────────────────────────────────────

    # Right Option = NX_DEVICERALTKEYMASK (bit 6 of device-dep flags)
    _NX_DEVICERALTKEYMASK = 0x00000040

    def _is_right_option_down(self):
        try:
            flags = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)
        except Exception:
            return self._ropt_held
        return bool(flags & self._NX_DEVICERALTKEYMASK)

    def _stream_ends_at_sentence_boundary(self) -> bool:
        """Check if the latest stream transcript ends with sentence-terminal punctuation."""
        state = self._transcript_state
        if not state:
            return True  # no transcript state, allow stop
        text = state.display_text().strip()
        if not text:
            return True  # nothing transcribed, allow stop
        return text[-1] in ".?!"

    def _process_key_events(self, _):
        # Silence auto-stop
        if self._silence_event.is_set():
            self._silence_event.clear()
            if self.recording:
                elapsed_recording = time.time() - self._record_started_at
                if elapsed_recording >= AUTO_SILENCE_MIN_SPEAKING:
                    self._log(
                        f"[silence] end of utterance after {elapsed_recording:.1f}s -- stopping"
                    )
                    self._stop_recording()
                else:
                    # Too early: reset so it can fire again once the user has spoken long enough
                    self._log(
                        f"[silence] ignored early trigger ({elapsed_recording:.1f}s < "
                        f"{AUTO_SILENCE_MIN_SPEAKING}s min)"
                    )
                    self._silence.reset()
                    threshold = AUTO_SILENCE_SECONDS if self._auto_silence_enabled else 9999.0
                    self._silence.set_silence_threshold(threshold)
            return

        # Recover from a missed modifier release. This is the main hotkey wedge.
        if self._ropt_held and not self._is_right_option_down():
            if time.time() - self._last_press_at > 0.08:
                self._ropt_held = False
                if self._key_event != "release":
                    self._log("[key] synthesized Right Option release after missed tap event")
                    self._key_event = "release"

        event = self._key_event
        if event is None:
            return
        self._key_event = None

        if event == "press":
            self.correction_mode = False
            self._last_press_at = time.time()
            self._log("[key] Right Option pressed")
            self._start_recording()
        elif event == "release":
            self._log("[key] Right Option released")
            if self.recording:
                self._stop_recording()

    def _watchdog_tap(self, _):
        if self._tap_thread_obj is not None and not self._tap_thread_obj.is_alive():
            self._log("[tap] thread died — restarting tap thread")
            self._restart_event_tap()
            return

        if self._tap_ref is not None:
            from Quartz import CGEventTapIsEnabled
            if not CGEventTapIsEnabled(self._tap_ref):
                self._log("[tap] was disabled by macOS — re-enabling")
                CGEventTapEnable(self._tap_ref, True)
            return

        if self._tap_last_heartbeat and time.time() - self._tap_last_heartbeat > 5:
            self._log("[tap] no active tap after 5s — restarting")
            self._restart_event_tap()

    def _start_event_tap(self):
        """Create a CGEventTap for flagsChanged events on a background thread."""
        if not HIServices.AXIsProcessTrusted():
            self._log(
                "[accessibility] NOT TRUSTED. Open System Settings > "
                "Privacy & Security > Accessibility and add this app."
            )
            HIServices.AXIsProcessTrustedWithOptions(
                {HIServices.kAXTrustedCheckOptionPrompt: True}
            )

        self._tap_heartbeat = 0
        self._tap_last_heartbeat = time.time()
        t = threading.Thread(target=self._tap_thread, daemon=True)
        t.start()
        self._tap_thread_obj = t

    def _restart_event_tap(self):
        """Kill and restart the event tap thread."""
        if self._tap_loop is not None:
            try:
                CFRunLoopStop(self._tap_loop)
            except Exception:
                pass
        self._tap_ref = None
        self._tap_loop = None
        self._tap_heartbeat = 0
        self._tap_last_heartbeat = time.time()
        self._ropt_held = False
        t = threading.Thread(target=self._tap_thread, daemon=True)
        t.start()
        self._tap_thread_obj = t

    def _tap_thread(self):
        """Background thread running the CGEventTap run loop."""
        mask = 1 << kCGEventFlagsChanged

        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            mask,
            self._flags_callback,
            None,
        )
        if tap is None:
            self._log(
                "[tap] CGEventTapCreate returned None. "
                "Accessibility permission not granted for this process."
            )
            return

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        self._tap_loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self._tap_loop, source, kCFRunLoopDefaultMode)
        CGEventTapEnable(tap, True)
        self._tap_ref = tap
        self._tap_heartbeat = time.time()
        self._log("[tap] CGEventTap active, listening for Right Option")
        CFRunLoopRun()

    def _flags_callback(self, proxy, event_type, event, refcon):
        """Called on every modifier flag change — must return instantly."""
        self._tap_heartbeat = time.time()
        flags = CGEventGetFlags(event)
        ropt_down = bool(flags & self._NX_DEVICERALTKEYMASK)

        if ropt_down and not self._ropt_held:
            if self.recording:
                # Spurious key-down while already recording — ignore to prevent state reset
                self._log("[key] spurious Right Option down while recording — ignored")
                return event
            self._ropt_held = True
            self._last_press_at = time.time()
            self._key_event = "press"
        elif not ropt_down and self._ropt_held:
            self._ropt_held = False
            self._key_event = "release"

        return event

    # ── Record ────────────────────────────────────────────────────────────────

    def _play(self, sound):
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{sound}.aiff"])

    def _ensure_warm_stream(self):
        with self._warm_stt_lock:
            if self.recording or self._stt or self._warm_stt or self._warm_stt_connecting:
                return
            self._warm_stt_connecting = True
        threading.Thread(target=self._warm_stream_worker, daemon=True).start()

    def _warm_stream_worker(self):
        stt = TelnyxStreamingSTT()
        try:
            stt.connect(TELNYX_API_KEY)
        except Exception as e:
            self._log(f"[stream] warm connect failed: {e}")
            with self._warm_stt_lock:
                self._warm_stt_connecting = False
            return

        with self._warm_stt_lock:
            if self.recording or self._stt or self._warm_stt:
                self._warm_stt_connecting = False
                try:
                    stt.close()
                except Exception:
                    pass
                return
            self._warm_stt = stt
            self._warm_stt_connected_at = time.time()
            self._warm_stt_connecting = False
        self._log("[stream] warm connection ready")

    def _claim_warm_stream(self):
        with self._warm_stt_lock:
            stt = self._warm_stt
            age = time.time() - self._warm_stt_connected_at
            if stt is not None and age > 45.0:
                self._log(f"[stream] warm stream stale ({age:.1f}s), discarding")
                try:
                    stt.close()
                except Exception:
                    pass
                stt = None
            self._warm_stt = None
            self._warm_stt_connecting = False
            return stt

    def _start_recording(self):
        if self._overlay_hide_timer is not None:
            self._overlay_hide_timer.cancel()
            self._overlay_hide_timer = None

        # Rate limit check -- give user feedback instead of silently ignoring
        backoff_remaining = self._rate_limit_backoff_until - time.time()
        if backoff_remaining > 0:
            self._play("Basso")
            wait_sec = int(backoff_remaining) + 1
            self.overlay.show()
            self.overlay.update("error", f"Rate limited - wait {wait_sec}s")
            self._hide_overlay_after_delay(1.5)
            return

        with self.lock:
            if self.recording:
                return
            if time.time() - self.last_pipeline < 1.5:
                return
            self.audio_frames = []
            self.recording = True
        session_id = self._begin_session()
        self._record_started_at = time.time()
        self._stream_connected_at = None
        self._last_overlay_preview_at = self._record_started_at
        self._overlay_stall_notice_shown = False
        self.icon = ICON_REC
        self.title = "⌥"
        self._silence.reset()
        if self._context_aware:
            self._current_context = self._get_focused_text_context()
            if self._current_context:
                self._log(f"[context] captured {len(self._current_context)} chars from focused field")
        else:
            self._current_context = ""
        threshold = AUTO_SILENCE_SECONDS if self._auto_silence_enabled else 9999.0
        self._silence.set_silence_threshold(threshold)
        self._silence_event.clear()
        self._chunk_time = time.time()
        self._transcript_state = TranscriptState()

        # IMMEDIATELY give user feedback — don't wait for stream/context
        self._play("Tink")
        self.overlay.show()
        self.overlay.update("listening", "")
        self._set_session_phase("recording", session_id)

        self._stt = self._claim_warm_stream()
        if self._stt is not None:
            self._stream_connected_at = time.time()
            threading.Thread(target=self._drain_stream_transcripts, daemon=True).start()
        else:
            threading.Thread(target=self._connect_stream_async, daemon=True).start()
        for attempt in range(3):
            try:
                self.stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=CHANNELS,
                    dtype="int16", callback=self._audio_callback)
                self.stream.start()
                break
            except Exception as e:
                self._log(f"[mic] error opening stream (attempt {attempt+1}): {e}")
                time.sleep(0.5)
        else:
            self._log("[mic] failed to open after 3 attempts — skipping")
            if self._stt:
                self._stt.close()
                self._stt = None
            with self.lock:
                self.recording = False
            self._ensure_warm_stream()
            return

    def _connect_stream_async(self) -> None:
        """Connect a fresh STT WebSocket in background; catch up with buffered audio."""
        stt = TelnyxStreamingSTT()
        try:
            stt.connect(TELNYX_API_KEY)
        except Exception as e:
            self._log(f"[stream] async connect failed: {e}")
            return
        if not self.recording:
            # Recording ended before we connected — discard
            try:
                stt.close()
            except Exception:
                pass
            return
        # Catch up: send audio frames already captured before stream was ready
        with self.lock:
            buffered = list(self.audio_frames)
        if buffered:
            try:
                pcm = np.concatenate(buffered, axis=0).tobytes()
                stt.send_audio(pcm)  # First call prepends WAV header automatically
            except Exception:
                pass
        self._stt = stt
        self._stream_connected_at = time.time()
        self._log(f"[stream] async connected at +{int((time.time() - self._record_started_at)*1000)}ms")
        threading.Thread(target=self._drain_stream_transcripts, daemon=True).start()

    def _watchdog_overlay_preview(self, _):
        if not self.recording:
            return
        if not self._last_overlay_preview_at:
            return
        since_last_partial = time.time() - self._last_overlay_preview_at
        if since_last_partial < 2.0:
            return
        # No stream partial received in 2s: show a live word-count estimate
        # so the user has feedback that the recording is progressing.
        elapsed = time.time() - self._record_started_at
        estimated_words = max(1, int(elapsed * 2.5))
        self.overlay.update("listening", f"~{estimated_words} words so far...")

    def _watchdog_recording(self, _):
        if not self.recording:
            return
        elapsed = time.time() - self._record_started_at
        if elapsed < MAX_RECORDING_SECONDS:
            return
        # Only force-stop if the key is genuinely not held — avoids cutting off long voice notes
        if not self._is_right_option_down():
            self._log(f"[watchdog] recording stuck for {elapsed:.0f}s, key not held — force-stopping")
            self._stop_recording()

    def _shutdown_stream_async(self, stream):
        if stream is None:
            return

        def _worker():
            try:
                self._log("[audio] stopping input stream")
                stream.stop()
            except Exception as e:
                self._log(f"[audio] stream stop error: {e}")
            try:
                self._log("[audio] closing input stream")
                stream.close()
            except Exception as e:
                self._log(f"[audio] stream close error: {e}")

        threading.Thread(target=_worker, daemon=True).start()

    def _stop_recording(self):
        session_id = self._active_session_id
        with self.lock:
            if not self.recording:
                return
            self.recording = False
            state_ref = self._transcript_state
            if state_ref and state_ref.stop_requested_at is None:
                state_ref.stop_requested_at = time.time()
            frames = list(self.audio_frames)
            stream = self.stream
            self.stream = None

        self._shutdown_stream_async(stream)

        # Send silence padding so Deepgram's VAD can finalize the utterance
        stt_for_padding = self._stt
        if stt_for_padding is not None:
            try:
                stt_for_padding.send_audio(_SILENCE_PADDING)
            except Exception:
                pass

        self.icon = ICON_IDLE
        if self._set_session_phase("processing", session_id):
            self.overlay.update("processing", "")

        if not frames:
            if self._stt:
                self._stt.close()
                self._stt = None
            self.overlay.hide()
            self._ensure_warm_stream()
            return

        audio = np.concatenate(frames, axis=0)
        duration = len(audio) / SAMPLE_RATE
        if duration < 0.5:
            if self._stt:
                self._stt.close()
                self._stt = None
            self.overlay.hide()
            self._ensure_warm_stream()
            return

        self.last_pipeline = time.time()
        state = self._transcript_state
        wav = self._to_wav_bytes(audio)
        threading.Thread(
            target=self._pipeline, args=(wav, state, session_id), daemon=True).start()

    # ── Pipeline ──────────────────────────────────────────────────────────────

    def _log(self, msg):
        print(msg, flush=True)

    def _pipeline(self, wav_bytes, state, session_id):
        def _timeout_watchdog():
            if not self._is_current_session(session_id):
                return
            self._play("Basso")
            self._show_error("Timed out — check network", session_id=session_id)
            self._log("[pipeline] timed out")
        watchdog = threading.Timer(8.0, _timeout_watchdog)
        watchdog.start()
        try:
            self._pipeline_inner(wav_bytes, state, session_id)
        finally:
            watchdog.cancel()

    def _hide_overlay_after_delay(self, delay=0.9, session_id=None):
        if self._overlay_hide_timer is not None:
            self._overlay_hide_timer.cancel()

        def _hide():
            self._overlay_hide_timer = None
            if session_id is not None and not self._is_current_session(session_id):
                return
            self._set_session_phase("idle", session_id)
            self.overlay.hide()

        self._overlay_hide_timer = threading.Timer(delay, _hide)
        self._overlay_hide_timer.start()

    def _pipeline_inner(self, wav_bytes, state, session_id):
        rms = int(np.sqrt(np.mean(np.frombuffer(wav_bytes[44:], dtype=np.int16).astype(np.float32)**2)))
        self._log(f"[pipeline] starting — audio RMS: {rms} ({'SILENT' if rms < 100 else 'OK'})")
        app_context = self._cleanup_context()

        duration_seconds = max(0.0, (len(wav_bytes) - 44) / float(SAMPLE_RATE * CHANNELS * 2))

        # Start batch in background immediately so it runs in parallel with stream drain
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        batch_future = _executor.submit(self._batch_transcribe, wav_bytes, state, duration_seconds)

        # Drain stream (shorter 0.15s window)
        stream_preview = self._finalize_streaming_transcript(state)

        stream_command = parse_command(stream_preview, correction_active=self._correction_active())
        if stream_command and duration_seconds <= 2.0:
            self._log(f"[command] using stream command fast path: {stream_command['kind']}")
            batch_future.cancel()
            _executor.shutdown(wait=False)
            if self._set_session_phase("final", session_id):
                self.overlay.update("final", stream_command["display"])
            self._apply_command(stream_command, state)
            self._hide_overlay_after_delay(session_id=session_id)
            self._log_metrics(state, final_text=stream_command["display"])
            return

        use_stream = self._should_accept_stream_result(state, stream_preview, duration_seconds)

        if use_stream:
            # Stream won: cancel batch (best effort), use stream result
            batch_future.cancel()
            _executor.shutdown(wait=False)
            transcript = stream_preview
            source = "stream"
        else:
            # Batch was already running from t=0, just collect the result
            _executor.shutdown(wait=False)
            try:
                batch_result = batch_future.result(timeout=9.0)
                transcript = batch_result[0] if batch_result else ""
                source = batch_result[1] if batch_result else "batch"
            except Exception as e:
                self._log(f"[pipeline] batch future failed: {e}")
                transcript = stream_preview or ""
                source = "stream_fallback"

        if state:
            state.final_source = source

        self._log(
            f"[pipeline] winner={source} stream_had_final={state.first_final_at is not None if state else False}"
        )

        if not transcript and not use_stream:
            transcript = stream_preview
        if not transcript:
            return

        # Reconcile removed from critical path — batch nova-3 is accurate enough
        # if not use_stream and self._should_reconcile_long_form(stream_preview, transcript, duration_seconds):
        #     reconciled = self._reconcile_transcripts(stream_preview, transcript, app_context, state)
        #     if reconciled:
        #         transcript = reconciled
        #         if state:
        #             state.final_source = "batch+reconcile"

        transcript = self._canonicalize_known_terms(self._normalize_transcript_text(transcript))
        transcript = self._remove_fillers(transcript)
        transcript = CORRECTION_STORE.apply(transcript)
        self.last_raw = transcript
        if stream_preview and stream_preview != transcript:
            self._log(f"[stream-preview] \"{stream_preview}\"")
        self._log(f"[stt] \"{transcript}\"")
        if not transcript:
            return

        command = parse_command(transcript, correction_active=self._correction_active())
        if command:
            if self._set_session_phase("final", session_id):
                self.overlay.update("final", command["display"])
            self._apply_command(command, state)
            self._hide_overlay_after_delay(session_id=session_id)
            self._log_metrics(state, final_text=command["display"])
            return

        result = transcript
        if self._should_run_cleanup(transcript, app_context, duration_seconds):
            cleaned = self._cleanup_transcript(transcript, app_context, state)
            if cleaned:
                result = cleaned
                if state:
                    state.final_source = f"{state.final_source}+cleanup" if state.final_source else "cleanup"

        result = result.strip()
        if not result:
            return
        result = self._canonicalize_known_terms(self._normalize_transcript_text(result))

        # Context-aware capitalization: lower first word if we are mid-sentence
        if self._context_aware and self._current_context and not (state and state.correction_target):
            result = self._apply_context_capitalization(result, self._current_context)
            if self._current_context:
                self._log(f"[context] capitalization applied, context ends with: '{self._current_context[-1]}'")

        self._render_text(result)

        if state and state.correction_target:
            self._log(f"[correction] replacing \"{state.correction_target}\" → \"{result}\"")
            self._learn_correction(state.correction_target, result)
            self.correction_mode = False

        self._remember_result(result)
        if state:
            state.final_text = result

        self._play("Pop")
        self.last_paste_time = time.time()
        self.correction_window_until = self.last_paste_time + CORRECTION_WINDOW_SECONDS
        self.last_pipeline = time.time()
        if self._set_session_phase("final", session_id):
            self.overlay.update("final", result)
        self._hide_overlay_after_delay(session_id=session_id)
        self._log_metrics(state, final_text=result)
        self._log(f"[done] injected and popped")

    def _learn_correction(self, old_text, new_text):
        """Compare old and new transcript, save changed phrases to dictionary."""
        old_words = old_text.lower().split()
        new_words = new_text.split()
        # Find differing spans using simple alignment
        if old_words == [w.lower() for w in new_words]:
            return  # only capitalization changed, skip
        # Save the full old→new pair if they differ meaningfully
        if old_text.lower().strip() != new_text.lower().strip():
            CORRECTION_STORE.save(old_text, new_text)
            self._log(f"[learned] \"{old_text}\" → \"{new_text}\"")

    def _drain_stream_transcripts(self):
        while self.recording and self._stt and self._transcript_state:
            item = self._stt.get_transcript(timeout=0.1)
            if item is None:
                continue
            transcript, is_final = item
            self._handle_stream_transcript(transcript, is_final)
        state = self._transcript_state
        if state and state.stop_requested_at is not None:
            end = state.stop_requested_at + STREAM_DRAIN_SECONDS
            while time.time() < end and self._stt:
                item = self._stt.get_transcript(timeout=0.05)
                if item is None:
                    continue
                transcript, is_final = item
                self._handle_stream_transcript(transcript, is_final)
        if state:
            state.done.set()

    def _handle_stream_transcript(self, transcript, is_final):
        if not transcript:
            return
        state = self._transcript_state
        if not state:
            return
        with self._transcript_lock:
            now = time.time()
            if state.first_partial_at is None:
                state.first_partial_at = now
            if is_final:
                merged = merge_transcript(state.committed_text, transcript)
                state.committed_text = merged
                state.unstable_text = ""
                if state.first_final_at is None:
                    state.first_final_at = now
            else:
                state.unstable_text = transcript.strip()
            state.final_text = state.display_text()
            self._last_overlay_preview_at = time.time()
            self._overlay_stall_notice_shown = False
            self.overlay.update("listening", state.final_text)

    def _finalize_streaming_transcript(self, state):
        if state is not None:
            state.done.wait(timeout=STREAM_DRAIN_SECONDS + 0.5)
        if self._stt is not None:
            try:
                self._stt.close(timeout=0.35)
            except Exception as e:
                self._log(f"[stream] close error: {e}")
            finally:
                self._stt = None
        self._ensure_warm_stream()
        if state is None:
            return ""
        with self._transcript_lock:
            state.stream_finalized_at = time.time()
            final_text = state.display_text().strip()
            if final_text:
                state.final_text = final_text
            state.closed = True
            return state.final_text.strip()

    def _should_accept_stream_result(self, state, transcript, duration_seconds):
        transcript = (transcript or "").strip()
        if not transcript:
            return False
        word_count = len(transcript.split())
        if word_count == 0:
            return False

        if duration_seconds > 25.0:
            self._log(
                f"[stream] rejected for long utterance duration_s={duration_seconds:.2f}"
            )
            return False

        # If Deepgram sent a final, trust it — don't gate on word count
        if state and state.first_final_at is not None:
            self._log(
                f"[stream] final candidate words={word_count} duration_s={duration_seconds:.2f} accepted=True"
            )
            return True

        accepted = duration_seconds <= 0.9 and word_count >= 1
        self._log(
            f"[stream] partial candidate words={word_count} duration_s={duration_seconds:.2f} accepted={accepted}"
        )
        return accepted

    def _batch_transcribe_request(self, wav_bytes):
        self._log("[stt] using batch fallback")
        try:
            vocab_terms = VOCAB_STORE.terms()
            model_config = {"smart_format": True, "punctuate": True}
            if vocab_terms:
                model_config["keyterms"] = vocab_terms[:50]
            resp = requests.post(
                STT_ENDPOINT,
                headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={
                    "model": "deepgram/nova-3",
                    "language": "en",
                    "model_config": json.dumps(model_config),
                },
                timeout=8,
            )
            rate_limited = resp.status_code == 429
            if resp.status_code == 429:
                resp = requests.post(
                    STT_ENDPOINT,
                    headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                    data={"model": "distil-whisper/distil-large-v2"},
                    timeout=8,
                )
                if resp.status_code == 429:
                    return "", True
            if resp.status_code == 200:
                return resp.json().get("text", "").strip(), rate_limited
            self._log(f"[stt error] {resp.status_code}: {resp.text[:200]}")
            return "", rate_limited
        except Exception as e:
            self._log(f"[stt exception] {e}")
            # Retry once on transient network/SSL errors
            try:
                import time as _time
                _time.sleep(0.3)
                resp = requests.post(
                    STT_ENDPOINT,
                    headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                    data={
                        "model": "deepgram/nova-3",
                        "language": "en",
                        "model_config": json.dumps({"smart_format": True, "punctuate": True}),
                    },
                    timeout=8,
                )
                if resp.status_code == 200:
                    self._log("[stt] retry succeeded")
                    return resp.json().get("text", "").strip(), False
            except Exception as e2:
                self._log(f"[stt retry failed] {e2}")
            raise

    def _batch_transcribe(self, wav_bytes, state=None, duration_seconds=0.0):
        if state:
            state.batch_started_at = time.time()
        try:
            transcript, rate_limited = self._batch_transcribe_request(wav_bytes)
        except Exception as e:
            if state:
                state.batch_finished_at = time.time()
            self._show_error(f"STT failed: {e}")
            return "", "batch"
        if state:
            state.batch_finished_at = time.time()
        if not transcript:
            if rate_limited:
                if state:
                    state.rate_limited = True
                self._rate_limit_backoff_until = time.time() + RATE_LIMIT_BACKOFF_SECONDS
                self._show_error("Rate limited, try again shortly")
            else:
                self._show_error("STT error")
            return "", "batch"
        if rate_limited:
            self._log("[stt] primary model rate limited")
            if state:
                state.rate_limited = True
            self._rate_limit_backoff_until = time.time() + RATE_LIMIT_BACKOFF_SECONDS
            return transcript, "batch"
        if self._should_retry_chunked_batch(transcript, duration_seconds):
            chunked = self._batch_transcribe_chunked(wav_bytes, state)
            if self._prefer_chunked_transcript(transcript, chunked):
                self._log("[stt] accepted chunked batch retry")
                return chunked, "batch+chunked"
        return transcript, "batch"

    def _should_retry_chunked_batch(self, transcript, duration_seconds):
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

    def _prefer_chunked_transcript(self, original, chunked):
        original = (original or "").strip()
        chunked = (chunked or "").strip()
        if not chunked:
            return False
        if len(chunked) <= len(original):
            return False
        if original.endswith((".", "!", "?")) and len(original.split()) >= len(chunked.split()) - 2:
            return False
        return True

    def _batch_transcribe_chunked(self, wav_bytes, state=None):
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
        if state:
            state.chunked_started_at = time.time()
            state.chunked_segments = len(segments)
        transcripts = []
        for segment in segments:
            chunk_wav = self._to_wav_bytes(segment)
            try:
                text, rate_limited = self._batch_transcribe_request(chunk_wav)
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
        if state:
            state.chunked_finished_at = time.time()
        if not transcripts:
            return ""
        merged = ""
        for text in transcripts:
            merged = merge_transcript(merged, text)
        return merged.strip()

    def _frontmost_app_name(self):
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is not None:
                name = app.localizedName()
                if name:
                    return str(name)
        except Exception:
            pass
        return ""

    def _cleanup_context(self):
        app_name = self._frontmost_app_name()
        vocabulary = VOCAB_STORE.terms()
        return {
            "app_name": app_name,
            "is_code_app": app_name in CODE_APPS,
            "vocabulary": vocabulary,
        }

    def _should_reconcile_long_form(self, stream_preview, batch_transcript, duration_seconds):
        if duration_seconds < 8.0:
            return False
        left = self._normalize_transcript_text(stream_preview or "")
        right = self._normalize_transcript_text(batch_transcript or "")
        if len(left.split()) < 12 or len(right.split()) < 12:
            return False
        if left.casefold() == right.casefold():
            return False
        shorter = min(len(left), len(right))
        longer = max(len(left), len(right))
        return shorter / max(1, longer) >= 0.65

    def _looks_codeish(self, transcript):
        lowered = transcript.lower()
        if re.search(r"[`{}()[\]<>_=\\/]", transcript):
            return True
        code_tokens = (
            "function ",
            "const ",
            "let ",
            "var ",
            "class ",
            "import ",
            "export ",
            "def ",
            "return ",
            "camel case",
            "snake case",
            "open bracket",
            "close bracket",
        )
        return any(token in lowered for token in code_tokens)

    def _should_run_cleanup(self, transcript, context, duration_seconds):
        if time.time() < self._rate_limit_backoff_until:
            return False
        if LLM_CLEANUP_MODE == "off":
            return False
        if LLM_CLEANUP_MODE == "on":
            return True
        if len(transcript.split()) < 18:
            return False
        if duration_seconds < 6.0:
            return False
        if context.get("is_code_app"):
            return False
        if self._looks_codeish(transcript):
            return False
        app_name = context.get("app_name") or ""
        prose_apps = {"Slack", "Messages", "Mail", "Notes", "Notion", "Google Chrome", "Arc", "Safari"}
        if app_name not in prose_apps:
            return False
        if any(transcript.lower().startswith(prefix) for prefix in ("actually ", "bullet ")):
            return False
        if any(token in transcript.lower() for token in ("release note", "remotion", "telnyx", "bolo", "wispr")):
            return False
        if transcript.endswith((".", "!", "?")) and re.search(r"[A-Z]", transcript):
            return False
        return True

    def _cleanup_transcript(self, transcript, context, state=None):
        app_name = context.get("app_name") or "unknown"
        vocabulary = context.get("vocabulary") or []
        vocab_line = ", ".join(vocabulary[:40]) if vocabulary else "none"
        existing_text = self._current_context
        if state:
            state.cleanup_started_at = time.time()
        context_instruction = ""
        if existing_text:
            snippet = existing_text[-300:]
            context_instruction = (
                f"The user is dictating into a text field that already contains this text "
                f"(last 300 chars shown): '{snippet}'. "
                "Continue naturally from that context. Do not repeat information already present. "
                "If the dictation starts mid-sentence (no capital), preserve that casing.\n"
            )
        try:
            llm_resp = requests.post(
                LLM_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {TELNYX_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "Qwen/Qwen3-235B-A22B",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"Frontmost app: {app_name}\n"
                                f"Preferred vocabulary: {vocab_line}\n"
                                f"{context_instruction}"
                                "Apply only minimal punctuation/capitalization cleanup.\n"
                                f"Transcript: {transcript}"
                            ),
                        },
                    ],
                    "max_tokens": 300,
                    "temperature": 0,
                    "enable_thinking": False,
                    "stream": True,
                },
                timeout=8,
                stream=True,
            )
            if llm_resp.status_code == 429:
                self._log("[llm] cleanup rate limited")
                self._rate_limit_backoff_until = time.time() + RATE_LIMIT_BACKOFF_SECONDS
                if state:
                    state.rate_limited = True
                    state.cleanup_finished_at = time.time()
                return ""
            if llm_resp.status_code != 200:
                self._log(f"[llm error] {llm_resp.status_code}: {llm_resp.text[:200]}")
                if state:
                    state.cleanup_finished_at = time.time()
                return ""
        except Exception as e:
            if state:
                state.cleanup_finished_at = time.time()
            self._log(f"[llm exception] {e}")
            return ""

        result = ""
        for line in llm_resp.iter_lines():
            if line and line.startswith(b"data: "):
                chunk = line[6:]
                if chunk == b"[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    self._log("[llm] malformed stream chunk")
                    continue
                delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                result += delta or ""

        if state:
            state.cleanup_finished_at = time.time()
        result = result.strip()
        if result:
            self._log(f"[llm] \"{result}\"")
        return result

    def _reconcile_transcripts(self, stream_preview, batch_transcript, context, state=None):
        if time.time() < self._rate_limit_backoff_until:
            return ""
        app_name = context.get("app_name") or "unknown"
        vocabulary = context.get("vocabulary") or []
        vocab_line = ", ".join(vocabulary[:40]) if vocabulary else "none"
        if state:
            state.reconcile_started_at = time.time()
        try:
            llm_resp = requests.post(
                LLM_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {TELNYX_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "Qwen/Qwen3-235B-A22B",
                    "messages": [
                        {"role": "system", "content": RECONCILE_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"Frontmost app: {app_name}\n"
                                f"Preferred vocabulary: {vocab_line}\n"
                                f"Streaming transcript: {stream_preview}\n"
                                f"Batch transcript: {batch_transcript}"
                            ),
                        },
                    ],
                    "max_tokens": 400,
                    "temperature": 0,
                    "enable_thinking": False,
                    "stream": True,
                },
                timeout=8,
                stream=True,
            )
            if llm_resp.status_code == 429:
                self._log("[reconcile] rate limited")
                self._rate_limit_backoff_until = time.time() + RATE_LIMIT_BACKOFF_SECONDS
                if state:
                    state.rate_limited = True
                    state.reconcile_finished_at = time.time()
                return ""
            if llm_resp.status_code != 200:
                self._log(f"[reconcile error] {llm_resp.status_code}: {llm_resp.text[:200]}")
                if state:
                    state.reconcile_finished_at = time.time()
                return ""
        except Exception as e:
            if state:
                state.reconcile_finished_at = time.time()
            self._log(f"[reconcile exception] {e}")
            return ""

        result = ""
        for line in llm_resp.iter_lines():
            if line and line.startswith(b"data: "):
                chunk = line[6:]
                if chunk == b"[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    self._log("[reconcile] malformed stream chunk")
                    continue
                delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                result += delta or ""

        if state:
            state.reconcile_finished_at = time.time()
        result = result.strip()
        if result:
            self._log(f"[reconcile] \"{result}\"")
        return result

    def _correction_active(self):
        return self.correction_mode or time.time() < self.correction_window_until

    def _apply_command(self, command, state):
        kind = command["kind"]
        visible_len = len(state.visible_text) if state else 0
        if visible_len:
            self._delete_text(visible_len)
            if state:
                state.visible_text = ""
        if kind == "scratch":
            target = "" if (state and state.correction_target) else (self.last_result or "")
            if target:
                self._delete_text(len(target))
            self.last_result = None
            self.correction_mode = False
            self.correction_window_until = 0.0
        elif kind == "replace":
            target = state.correction_target if state and state.correction_target else self.last_result
            if target:
                if not (state and state.correction_target):
                    self._delete_text(len(target))
                self._type_text(command["text"])
                self._remember_result(command["text"])
                self.last_paste_time = time.time()
                self.correction_window_until = self.last_paste_time + CORRECTION_WINDOW_SECONDS
                self._learn_correction(target, command["text"])
                if state:
                    state.final_text = command["text"]
            self.correction_mode = False
            self._play("Pop")
        elif kind == "insert":
            self._type_text(command["text"])
            self._remember_result(command["text"])
            self.last_paste_time = time.time()
            self.correction_window_until = self.last_paste_time + CORRECTION_WINDOW_SECONDS
            if state:
                state.final_text = command["text"]
            self._play("Pop")
        if kind == "scratch":
            self.menu["Last transcript"].title = "↳ [scratch that]"

    def _render_text(self, text):
        state = self._transcript_state
        if state is None:
            return
        previous = state.visible_text
        target = text or ""
        prefix = longest_common_prefix(previous, target)
        to_delete = len(previous) - prefix
        if to_delete > 0:
            self._delete_text(to_delete)
        suffix = target[prefix:]
        if suffix:
            self._type_text(suffix)
            if state.first_visible_at is None:
                state.first_visible_at = time.time()
        state.visible_text = target

    def _type_text(self, text):
        if not text:
            return
        down = CGEventCreateKeyboardEvent(None, 0, True)
        up = CGEventCreateKeyboardEvent(None, 0, False)
        CGEventKeyboardSetUnicodeString(down, len(text), text)
        CGEventKeyboardSetUnicodeString(up, len(text), text)
        CGEventSetFlags(down, 0)
        CGEventSetFlags(up, 0)
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)

    def _delete_text(self, count):
        for _ in range(max(0, count)):
            down = CGEventCreateKeyboardEvent(None, DELETE_KEYCODE, True)
            up = CGEventCreateKeyboardEvent(None, DELETE_KEYCODE, False)
            CGEventSetFlags(down, 0)
            CGEventSetFlags(up, 0)
            CGEventPost(kCGHIDEventTap, down)
            CGEventPost(kCGHIDEventTap, up)

    def _log_metrics(self, state, final_text):
        if state is None:
            return
        now = time.time()
        record_ms = int((now - self._record_started_at) * 1000) if self._record_started_at else None
        final_words = len((final_text or "").split())
        metrics = {
            "record_ms": record_ms,
            "audio_duration_s": round(record_ms / 1000.0, 2) if record_ms is not None else None,
            "release_to_final_ms": int((now - state.stop_requested_at) * 1000)
            if state.stop_requested_at else None,
            "stream_connect_ms": int((self._stream_connected_at - self._record_started_at) * 1000)
            if self._stream_connected_at and self._record_started_at else None,
            "first_partial_ms": int((state.first_partial_at - self._record_started_at) * 1000)
            if state.first_partial_at and self._record_started_at else None,
            "first_final_ms": int((state.first_final_at - self._record_started_at) * 1000)
            if state.first_final_at and self._record_started_at else None,
            "first_visible_ms": int((state.first_visible_at - self._record_started_at) * 1000)
            if state.first_visible_at and self._record_started_at else None,
            "stream_finalize_ms": int((state.stream_finalized_at - self._record_started_at) * 1000)
            if state.stream_finalized_at and self._record_started_at else None,
            "batch_ms": int((state.batch_finished_at - state.batch_started_at) * 1000)
            if state.batch_started_at and state.batch_finished_at else None,
            "chunked_ms": int((state.chunked_finished_at - state.chunked_started_at) * 1000)
            if state.chunked_started_at and state.chunked_finished_at else None,
            "chunked_segments": state.chunked_segments or None,
            "reconcile_ms": int((state.reconcile_finished_at - state.reconcile_started_at) * 1000)
            if state.reconcile_started_at and state.reconcile_finished_at else None,
            "cleanup_ms": int((state.cleanup_finished_at - state.cleanup_started_at) * 1000)
            if state.cleanup_started_at and state.cleanup_finished_at else None,
            "final_chars": len(final_text or ""),
            "final_words": final_words,
            "chars_per_second": round(len(final_text or "") / max(0.1, record_ms / 1000.0), 2)
            if record_ms is not None else None,
            "final_source": state.final_source or None,
            "rate_limited": state.rate_limited,
            "stream_failed": state.stream_failed,
        }
        self._log(f"[metrics] {json.dumps(metrics, sort_keys=True)}")

    def _remember_result(self, text):
        self.last_result = text
        label = text if len(text) <= 50 else text[:47] + "..."
        self.menu["Last transcript"].title = f"↳ {label}"
        self.session_history.appendleft(text)
        self.menu["History"].title = f"History ({len(self.session_history)})"

    def _copy_last(self, _):
        if self.last_result:
            pasteboard = NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            pasteboard.setString_forType_(self.last_result, NSPasteboardTypeString)
            prev = self.menu["Last transcript"].title
            self.menu["Last transcript"].title = "✓ Copied!"
            threading.Timer(1.5, lambda: setattr(
                self.menu["Last transcript"], "title", prev)).start()

    def _show_error(self, msg, session_id=None):
        self.menu["Last transcript"].title = f"✗ {msg}"
        if self._set_session_phase("error", session_id):
            self.overlay.update("error", msg)
        self._hide_overlay_after_delay(1.0, session_id=session_id)

    # ── Quit ──────────────────────────────────────────────────────────────────

    def quit_app(self, _):
        self._active_session_id += 1
        self._session_phase = "idle"
        if self.stream:
            self.stream.stop()
            self.stream.close()
        if self._stt:
            self._stt.close()
            self._stt = None
        with self._warm_stt_lock:
            warm_stt = self._warm_stt
            self._warm_stt = None
            self._warm_stt_connecting = False
        if warm_stt:
            warm_stt.close()
        if self._tap_loop is not None:
            CFRunLoopStop(self._tap_loop)
        self.overlay.hide()
        import pathlib
        pathlib.Path("/tmp/bolo.lock").unlink(missing_ok=True)
        rumps.quit_application()


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if not TELNYX_API_KEY:
        print("ERROR: TELNYX_API_KEY not set.")
        print("Get a free key at https://telnyx.com and add to your shell:")
        print('  export TELNYX_API_KEY="your_key_here"')
        sys.exit(1)
    def _crash_handler(signum, frame):
        print(f"[crash] received signal {signum}", flush=True)
        traceback.print_stack(frame)
    for _sig in (signal.SIGABRT, signal.SIGBUS, signal.SIGSEGV):
        try:
            signal.signal(_sig, _crash_handler)
        except (OSError, ValueError):
            pass

    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory)
    BoloApp().run()
