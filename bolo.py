#!/usr/bin/env python3
"""
Bolo — Telnyx voice dictation menubar app.
Hold Right Option anywhere to dictate. Release to transcribe and inject.
"""

import concurrent.futures
import datetime
import io
import json
import logging
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
    NSEvent,
    NSEventMaskFlagsChanged,
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
    CGEventTapIsEnabled,
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CGEventGetFlags,
    CGEventMaskBit,
    kCGEventTapOptionListenOnly,
    kCGHeadInsertEventTap,
    kCGSessionEventTap,
    kCGHIDEventTap,
    kCGEventSourceStateCombinedSessionState,
    kCGEventFlagsChanged,
)
from CoreFoundation import kCFRunLoopDefaultMode
import HIServices

# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_FILE = "/tmp/bolo.log"
_log_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(message)s"))
_logger = logging.getLogger("bolo")
_logger.setLevel(logging.DEBUG)
_logger.addHandler(_log_handler)
_logger.addHandler(_console_handler)
_logger.propagate = False

# ── Config ────────────────────────────────────────────────────────────────────

def _load_env_value(name: str) -> str:
    """Load a config value.

    Priority (highest first):
      1. ~/.codex/.env — authoritative on-disk source; always wins so that
         stale shell env vars (from a previous session) cannot shadow the key.
      2. os.environ — useful for CI/test overrides when .codex/.env is absent.
      3. ~/.zshrc — last-resort fallback.
    """
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
                        val = raw.strip().strip("\"'")
                        if val:
                            return val
        except OSError:
            pass

    value = os.environ.get(name, "").strip()
    if value:
        return value

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


TELNYX_API_KEY  = _load_env_value("TELNYX_API_KEY")
_LITELLM_BASE   = _load_env_value("LITELLM_BASE") or ""
_LITELLM_KEY    = _load_env_value("LITELLM_KEY") or ""

STT_ENDPOINT    = "https://api.telnyx.com/v2/ai/audio/transcriptions"
_TELNYX_LLM_ENDPOINT = "https://api.telnyx.com/v2/ai/chat/completions"


def _llm_endpoint() -> str:
    """Return the LLM chat-completions URL. Prefer LiteLLM proxy when available."""
    if _LITELLM_BASE:
        base = _LITELLM_BASE.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        return f"{base}/chat/completions"
    return _TELNYX_LLM_ENDPOINT


def _llm_headers() -> dict:
    """Return Authorization headers for the active LLM backend."""
    if _LITELLM_BASE and _LITELLM_KEY:
        return {"Authorization": f"Bearer {_LITELLM_KEY}", "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {TELNYX_API_KEY}", "Content-Type": "application/json"}


def _llm_model() -> str:
    """Return model ID appropriate for the active backend."""
    if _LITELLM_BASE:
        return "MiniMax-M2.5-drop"
    return "Qwen/Qwen3-235B-A22B"


SAMPLE_RATE    = 16000
CHANNELS       = 1
CORRECTION_WINDOW_SECONDS = 3.0
STREAM_DRAIN_SECONDS = 0.35
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
BOLO_METRICS_FILE = os.path.expanduser("~/.bolo/metrics.jsonl")
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

# STT prompt limit: Whisper uses a 224-token window; 4 chars/token is a safe approximation.
_STT_PROMPT_MAX_CHARS = 224 * 4


def build_stt_prompt(vocab_terms: list, context_text: str = "") -> str:
    """
    Build a short STT hint string for the Telnyx/Whisper `prompt` parameter.

    Proper nouns and domain terms are listed first (highest leverage), followed
    by a tail of the active text-field context so the model continues in the
    right register.  The result is capped at _STT_PROMPT_MAX_CHARS to stay
    within Whisper's 224-token prompt window.

    Returns an empty string when there is nothing useful to inject.
    """
    parts = []

    if vocab_terms:
        # Comma-separated list of recognised terms
        parts.append(", ".join(vocab_terms))

    if context_text:
        # Append the last 120 chars of the active field — enough for register
        # continuity without blowing the token budget.
        tail = context_text.strip()[-120:]
        if tail:
            parts.append(tail)

    prompt = ". ".join(parts) if parts else ""
    return prompt[:_STT_PROMPT_MAX_CHARS]


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

_CLEANUP_PROMPT_SLACK = (
    "You are a transcription formatter for a casual chat message. "
    "Fix obvious errors, remove filler words (um, uh, you know, like), and apply light punctuation. "
    "Keep contractions (don't, I'm, it's). Do NOT add formal punctuation or restructure sentences. "
    "Keep the tone casual and conversational. Output only the cleaned text."
)

_CLEANUP_PROMPT_MAIL = (
    "You are a transcription formatter for an email message. "
    "Fix grammar, apply proper sentence punctuation, remove filler words (um, uh, you know, kind of, sort of). "
    "Use full sentences with correct capitalization. Maintain a professional but natural tone. "
    "Do not add or remove content. Output only the cleaned text."
)

_CLEANUP_PROMPT_NOTES = (
    "You are a transcription formatter for a notes or document app. "
    "Fix grammar and punctuation. Remove filler words. "
    "If the text appears to be a list or contains bullet-point structure, preserve that structure. "
    "Apply light formatting improvements without changing meaning. Output only the cleaned text."
)


def _build_cleanup_prompt(app_name: str) -> str:
    """Return the system prompt variant appropriate for the given app."""
    name = (app_name or "").lower()
    if any(k in name for k in ("slack", "messages", "discord", "whatsapp", "telegram")):
        return _CLEANUP_PROMPT_SLACK
    if any(k in name for k in ("mail", "gmail", "outlook", "spark")):
        return _CLEANUP_PROMPT_MAIL
    if any(k in name for k in ("notes", "notion", "obsidian", "bear", "craft", "docs", "word")):
        return _CLEANUP_PROMPT_NOTES
    return SYSTEM_PROMPT

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
            rumps.MenuItem("Clipboard paste mode", callback=self._toggle_clipboard_mode),
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
        self._clipboard_mode_enabled = prefs.get("clipboard_mode_enabled", False)
        self._update_auto_silence_menu()
        self._update_clipboard_mode_menu()

        # Streaming STT state
        self._stt            = None
        self._transcript_state = None
        self._transcript_lock = threading.Lock()
        self._warm_stt = None
        self._warm_stt_lock = threading.Lock()
        self._warm_stt_connecting = False
        self._warm_stt_connected_at: float = 0.0
        self._stream_auth_failed = False  # set on first 401; blocks all warm retries
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
        self._ns_monitor = None  # deprecated: using CGEventTap now
        self._key_event = None  # "press" or "release" set by handler
        self._last_press_at = 0.0
        self._last_key_recovery_check = 0.0

        # CGEventTap state
        self._cg_tap = None
        self._cg_tap_thread = None
        self._cg_tap_enabled = False

        # Panic reset: triple-tap Right Option to force reset
        self._panic_presses = []
        self._PANIC_WINDOW = 1.0  # 1 second to triple-tap

        # Start CGEventTap hotkey listener (more reliable than NSEvent)
        self._start_cgevent_tap()
        # Poll key events and watchdogs on main thread
        rumps.Timer(self._process_key_events, 0.02).start()
        rumps.Timer(self._watchdog_cg_tap, 2).start()  # Restart tap if disabled
        rumps.Timer(self._watchdog_overlay_health, 1.0).start()  # Monitor overlay
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

    def _update_clipboard_mode_menu(self):
        label = "Clipboard paste mode " + ("✓" if self._clipboard_mode_enabled else "")
        self.menu["Clipboard paste mode"].title = label.strip()

    def _toggle_clipboard_mode(self, _):
        self._clipboard_mode_enabled = not self._clipboard_mode_enabled
        self._update_clipboard_mode_menu()
        prefs = self._load_prefs()
        prefs["clipboard_mode_enabled"] = self._clipboard_mode_enabled
        self._save_prefs(prefs)
        self._log(f"[clipboard] paste mode {'enabled' if self._clipboard_mode_enabled else 'disabled'}")

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

    # ── Hotkey (NSEvent global monitor) ───────────────────────────────────

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

        # AGGRESSIVE RECOVERY: Check actual key state every tick
        is_down = self._is_right_option_down()

        # Case 1: We think key is held, but it's actually up -> force release
        if self._ropt_held and not is_down:
            self._ropt_held = False
            if self._key_event != "release":
                self._log("[key] RECOVERY: synthesized release (missed event)")
                self._key_event = "release"

        # Case 2: We think key is up, but it's actually held -> might be stuck from previous session
        # Only recover if not currently recording (avoid double-trigger while active)
        elif not self._ropt_held and is_down and not self.recording:
            # Check if we've been stuck for a while (key held but we missed the press)
            if time.time() - self._last_key_recovery_check > 2.0:
                self._ropt_held = True
                self._last_press_at = time.time()
                self._log("[key] RECOVERY: synthesized press (missed event)")
                self._key_event = "press"
        self._last_key_recovery_check = time.time()

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
        # NSEvent monitors are never disabled by macOS — just log if monitor was lost.
        if self._ns_monitor is None:
            self._log("[tap] NSEvent monitor is None — re-registering")
            self._start_nsevent_monitor()

    def _start_nsevent_monitor(self):
        """Register an NSEvent global monitor for flagsChanged events.

        NSEvent monitors are never disabled by macOS (unlike CGEventTap),
        so no watchdog re-enable loop is needed.
        """
        if not HIServices.AXIsProcessTrusted():
            self._log(
                "[accessibility] NOT TRUSTED. Open System Settings > "
                "Privacy & Security > Accessibility and add this app."
            )
            HIServices.AXIsProcessTrustedWithOptions(
                {HIServices.kAXTrustedCheckOptionPrompt: True}
            )

        self._ns_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged,
            self._nsevent_flags_handler,
        )
        if self._ns_monitor is None:
            self._log("[tap] NSEvent monitor creation failed — Accessibility not granted?")
        else:
            self._log("[tap] NSEvent global monitor active, listening for Right Option")

    def _nsevent_flags_handler(self, event):
        """Called on every modifier flag change via NSEvent global monitor."""
        flags = event.modifierFlags()
        ropt_down = bool(flags & self._NX_DEVICERALTKEYMASK)

        if ropt_down and not self._ropt_held:
            if self.recording:
                # Spurious key-down while already recording — ignore to prevent state reset
                self._log("[key] spurious Right Option down while recording — ignored")
                self._ropt_held = True  # still track so release is recognized
                return
            self._ropt_held = True
            self._last_press_at = time.time()
            self._key_event = "press"
        elif not ropt_down and self._ropt_held:
            self._ropt_held = False
            self._key_event = "release"

    # ── CGEventTap Hotkey Listener (Primary) ───────────────────────────────

    def _cgevent_callback(self, proxy, event_type, event, refcon):
        """Callback for CGEventTap - called on every event in the tap.

        More reliable than NSEvent global monitor because it runs at lower level
        and can be re-enabled if macOS disables it.

        Also implements panic reset: triple-tap Right Option to force reset.
        """
        if event_type == kCGEventFlagsChanged:
            flags = CGEventGetFlags(event)
            ropt_down = bool(flags & self._NX_DEVICERALTKEYMASK)

            # Panic reset detection: track press releases in a 1-second window
            if ropt_down:
                now = time.time()
                self._panic_presses.append(now)
                # Keep only presses in the last second
                self._panic_presses = [t for t in self._panic_presses if now - t < self._PANIC_WINDOW]
                # Triple-tap detected
                if len(self._panic_presses) >= 3:
                    self._log("[panic] triple-tap Right Option detected — forcing reset")
                    self._panic_reset()
                    self._panic_presses = []

            if ropt_down and not self._ropt_held:
                if self.recording:
                    # Spurious key-down while already recording — ignore
                    self._log("[tap] spurious Right Option down while recording — ignored")
                    self._ropt_held = True
                else:
                    self._ropt_held = True
                    self._last_press_at = time.time()
                    self._key_event = "press"
                    self._log("[tap] Right Option pressed (CGEventTap)")
            elif not ropt_down and self._ropt_held:
                self._ropt_held = False
                self._key_event = "release"
                self._log("[tap] Right Option released (CGEventTap)")

        return event

    def _panic_reset(self):
        """Force reset all state — emergency recovery for wedged hotkey."""
        self._log("[panic] executing force reset")

        # Force stop recording if active
        if self.recording:
            self._log("[panic] forcing recording stop")
            try:
                self._stop_recording()
            except Exception as e:
                self._log(f"[panic] error stopping recording: {e}")

        # Reset key state
        self._ropt_held = False
        self._key_event = None

        # Force kill overlay
        try:
            self.overlay.force_kill()
        except Exception as e:
            self._log(f"[panic] error killing overlay: {e}")

        # Play error sound so user knows reset happened
        try:
            self._play("Basso")
        except Exception:
            pass

        self._log("[panic] force reset complete")

    def _start_cgevent_tap(self):
        """Create and enable a CGEventTap for modifier key events.

        CGEventTap is lower-level than NSEvent and more reliable, but can be
        disabled by macOS if the process stalls. We watchdog-re-enable it.
        """
        def tap_thread():
            # Create the event tap for flags changed events
            self._cg_tap = CGEventTapCreate(
                kCGSessionEventTap,  # Session scope (not HID - works without root)
                kCGHeadInsertEventTap,  # Insert at head of event stream
                kCGEventTapOptionListenOnly,  # Don't intercept, just observe
                CGEventMaskBit(kCGEventFlagsChanged),  # Only modifier key events
                self._cgevent_callback,
                None
            )

            if self._cg_tap is None:
                self._log("[tap] CGEventTap creation failed — need Accessibility permission")
                # Fall back to NSEvent
                self._start_nsevent_monitor()
                return

            # Get the runloop source
            run_loop_source = CFMachPortCreateRunLoopSource(None, self._cg_tap, 0)

            # Add to current runloop (default mode)
            from CoreFoundation import kCFRunLoopDefaultMode
            CFRunLoopAddSource(CFRunLoopGetCurrent(), run_loop_source, kCFRunLoopDefaultMode)

            # Enable the tap
            CGEventTapEnable(self._cg_tap, True)
            self._cg_tap_enabled = True
            self._log("[tap] CGEventTap active (with watchdog)")

            # Run the loop
            CFRunLoopRun()

        # Start in a daemon thread
        self._cg_tap_thread = threading.Thread(target=tap_thread, daemon=True)
        self._cg_tap_thread.start()

    def _watchdog_cg_tap(self, _):
        """Watchdog: re-enable CGEventTap if macOS disabled it."""
        if self._cg_tap is None:
            return  # Fall back mode

        if not CGEventTapIsEnabled(self._cg_tap):
            self._log("[tap] CGEventTap was disabled by macOS — re-enabling")
            CGEventTapEnable(self._cg_tap, True)

    def _watchdog_overlay_health(self, _):
        """Watchdog: monitor overlay process and restart if dead while showing."""
        # If overlay died while supposed to be showing, force hide to clean state
        if self.overlay._is_showing and not self.overlay.is_alive():
            self._log("[overlay] died while showing — forcing hide to reset")
            self.overlay._proc = None
            self.overlay._is_showing = False
            # If we're recording, stop to avoid stuck state
            if self.recording:
                self._log("[overlay] overlay died during recording — stopping")
                self._stop_recording()

    # ── Record ────────────────────────────────────────────────────────────────

    def _play(self, sound):
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{sound}.aiff"])

    def _ensure_warm_stream(self):
        if self._stream_auth_failed:
            return  # API key is bad — don't hammer the endpoint
        with self._warm_stt_lock:
            if self.recording or self._stt or self._warm_stt or self._warm_stt_connecting:
                return
            self._warm_stt_connecting = True
        threading.Thread(target=self._warm_stream_worker, daemon=True).start()

    def _warm_stream_worker(self):
        stt = TelnyxStreamingSTT()
        try:
            stt.connect(TELNYX_API_KEY, keywords=VOCAB_STORE.terms())
        except Exception as e:
            err_msg = str(e)
            self._log(f"[stream] warm connect failed: {e}")
            if "401" in err_msg or "Unauthorized" in err_msg or "auth" in err_msg.lower():
                self._stream_auth_failed = True
                self._rate_limit_backoff_until = time.time() + 86400.0
                self._log("[stream] 401 auth failure — disabling stream. Fix API key and restart.")
                self._show_error("Invalid API key — check ~/.codex/.env")
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

        # Backoff / auth-failure check — give user feedback instead of silently ignoring
        backoff_remaining = self._rate_limit_backoff_until - time.time()
        if backoff_remaining > 0:
            self._play("Basso")
            self.overlay.show()
            if self._stream_auth_failed:
                self.overlay.update("error", "Invalid API key — restart required")
            else:
                wait_sec = int(backoff_remaining) + 1
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
            stt.connect(TELNYX_API_KEY, keywords=VOCAB_STORE.terms())
        except Exception as e:
            err_msg = str(e)
            self._log(f"[stream] async connect failed: {e}")
            if "401" in err_msg or "Unauthorized" in err_msg or "auth" in err_msg.lower():
                self._stream_auth_failed = True
                self._rate_limit_backoff_until = time.time() + 86400.0
                self._log("[stream] 401 auth failure — disabling stream. Fix API key and restart.")
                self._show_error("Invalid API key — check ~/.codex/.env")
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
        with self.lock:
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
        if self._set_session_phase("transcribing", session_id):
            self.overlay.update("transcribing", "")

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
        _logger.info(msg)

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
            if self._set_session_phase("inserting", session_id):
                self.overlay.update("inserting", stream_command["display"])
            self._apply_command(stream_command, state)
            if self._set_session_phase("success", session_id):
                self.overlay.update("success", stream_command["display"])
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
            if self._set_session_phase("inserting", session_id):
                self.overlay.update("inserting", command["display"])
            self._apply_command(command, state)
            if self._set_session_phase("success", session_id):
                self.overlay.update("success", command["display"])
            self._hide_overlay_after_delay(session_id=session_id)
            self._log_metrics(state, final_text=command["display"])
            return

        result = transcript
        run_async_cleanup = (
            not (state and state.correction_target)
            and self._should_run_cleanup(transcript, app_context, duration_seconds)
        )

        result = result.strip()
        if not result:
            return
        result = self._canonicalize_known_terms(self._normalize_transcript_text(result))

        # Context-aware capitalization: lower first word if we are mid-sentence
        if self._context_aware and self._current_context and not (state and state.correction_target):
            result = self._apply_context_capitalization(result, self._current_context)
            if self._current_context:
                self._log(f"[context] capitalization applied, context ends with: '{self._current_context[-1]}'")

        if self._set_session_phase("inserting", session_id):
            self.overlay.update("inserting", result)

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
        if self._set_session_phase("success", session_id):
            self.overlay.update("success", result)
        self._hide_overlay_after_delay(session_id=session_id)
        self._log_metrics(state, final_text=result)
        self._log(f"[done] injected and popped")

        # Async LLM cleanup: log only, no in-place replacement (disabled — fires into wrong window)
        if run_async_cleanup:
            raw_for_cleanup = result
            threading.Thread(
                target=lambda: self._cleanup_transcript(raw_for_cleanup, app_context, state),
                daemon=True,
            ).start()

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
            stt_prompt = build_stt_prompt(vocab_terms, self._current_context)
            primary_data = {
                "model": "deepgram/nova-3",
                "language": "en",
                "model_config": json.dumps(model_config),
            }
            if stt_prompt:
                primary_data["prompt"] = stt_prompt
                self._log(f"[stt] prompt injected ({len(stt_prompt)} chars)")
            resp = requests.post(
                STT_ENDPOINT,
                headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data=primary_data,
                timeout=8,
            )
            if resp.status_code == 401:
                self._log("[stt] 401 Unauthorized — check TELNYX_API_KEY in ~/.codex/.env")
                raise RuntimeError("STT auth failed (401): invalid or missing API key")
            rate_limited = resp.status_code == 429
            if resp.status_code == 429:
                # Rate-limited fallback: lighter model, still include prompt
                fallback_data = {"model": "distil-whisper/distil-large-v2"}
                if stt_prompt:
                    fallback_data["prompt"] = stt_prompt
                resp = requests.post(
                    STT_ENDPOINT,
                    headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
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
        except Exception as e:
            self._log(f"[stt exception] {e}")
            # Retry once on transient network/SSL errors
            try:
                import time as _time
                _time.sleep(0.3)
                vocab_terms = VOCAB_STORE.terms()
                retry_data = {
                    "model": "deepgram/nova-3",
                    "language": "en",
                    "model_config": json.dumps({"smart_format": True, "punctuate": True}),
                }
                retry_prompt = build_stt_prompt(vocab_terms, self._current_context)
                if retry_prompt:
                    retry_data["prompt"] = retry_prompt
                resp = requests.post(
                    STT_ENDPOINT,
                    headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
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

    def _batch_transcribe(self, wav_bytes, state=None, duration_seconds=0.0):
        if state:
            state.batch_started_at = time.time()
        try:
            transcript, rate_limited = self._batch_transcribe_request(wav_bytes)
        except RuntimeError as e:
            if state:
                state.batch_finished_at = time.time()
            msg = str(e)
            if "401" in msg or "auth failed" in msg.lower():
                # Auth failure: freeze backoff so no further requests are made.
                # User must fix the key and restart.
                self._rate_limit_backoff_until = time.time() + 86400.0  # 24h
                self._show_error("Invalid API key — check ~/.codex/.env")
            else:
                self._show_error(f"STT failed: {e}")
            return "", "batch"
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
            self._log("[llm] skipped (rate limit backoff)")
            return False
        if LLM_CLEANUP_MODE == "off":
            self._log("[llm] skipped (mode=off)")
            return False
        if LLM_CLEANUP_MODE == "on":
            self._log("[llm] running (mode=on)")
            return True
        # auto mode — relaxed thresholds
        word_count = len(transcript.split())
        if word_count < 6:
            self._log(f"[llm] skipped (too short: {word_count} words)")
            return False
        if duration_seconds < 2.0:
            self._log(f"[llm] skipped (duration too short: {duration_seconds:.1f}s)")
            return False
        if context.get("is_code_app"):
            self._log("[llm] skipped (code app)")
            return False
        if self._looks_codeish(transcript):
            self._log("[llm] skipped (looks like code)")
            return False
        self._log("[llm] running (auto)")
        return True

    def _call_llm(self, system_prompt: str, user_content: str, state=None) -> str:
        """
        POST to the active LLM backend (LiteLLM proxy or Telnyx direct).
        Falls back to Telnyx API if LiteLLM returns a non-200 on first attempt.
        Returns the streamed completion text, or "" on failure.
        """
        endpoint = _llm_endpoint()
        headers = _llm_headers()
        model = _llm_model()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 1500,
            "temperature": 0,
            "stream": True,
        }
        # Telnyx-specific param — harmless to omit on LiteLLM
        if not _LITELLM_BASE:
            payload["enable_thinking"] = False

        def _do_request(ep, hdrs):
            return requests.post(ep, headers=hdrs, json=payload, timeout=8, stream=True)

        try:
            resp = _do_request(endpoint, headers)
        except Exception as e:
            # If LiteLLM is unreachable, fall back to Telnyx silently
            if _LITELLM_BASE:
                self._log(f"[llm] LiteLLM unreachable ({e}), falling back to Telnyx API")
                fallback_headers = {
                    "Authorization": f"Bearer {TELNYX_API_KEY}",
                    "Content-Type": "application/json",
                }
                payload["model"] = "Qwen/Qwen3-235B-A22B"
                payload["enable_thinking"] = False
                try:
                    resp = _do_request(_TELNYX_LLM_ENDPOINT, fallback_headers)
                except Exception as e2:
                    if state:
                        state.cleanup_finished_at = time.time()
                    self._log(f"[llm exception] fallback also failed: {e2}")
                    return ""
            else:
                if state:
                    state.cleanup_finished_at = time.time()
                self._log(f"[llm exception] {e}")
                return ""

        if resp.status_code == 429:
            self._log("[llm] rate limited")
            self._rate_limit_backoff_until = time.time() + RATE_LIMIT_BACKOFF_SECONDS
            if state:
                state.rate_limited = True
                state.cleanup_finished_at = time.time()
            return ""

        if resp.status_code != 200:
            # If LiteLLM returned an error, retry on Telnyx
            if _LITELLM_BASE:
                self._log(f"[llm] LiteLLM error {resp.status_code}, falling back to Telnyx API")
                fallback_headers = {
                    "Authorization": f"Bearer {TELNYX_API_KEY}",
                    "Content-Type": "application/json",
                }
                payload["model"] = "Qwen/Qwen3-235B-A22B"
                payload["enable_thinking"] = False
                try:
                    resp = _do_request(_TELNYX_LLM_ENDPOINT, fallback_headers)
                    if resp.status_code != 200:
                        self._log(f"[llm error] fallback {resp.status_code}: {resp.text[:200]}")
                        if state:
                            state.cleanup_finished_at = time.time()
                        return ""
                except Exception as e:
                    if state:
                        state.cleanup_finished_at = time.time()
                    self._log(f"[llm exception] fallback: {e}")
                    return ""
            else:
                self._log(f"[llm error] {resp.status_code}: {resp.text[:200]}")
                if state:
                    state.cleanup_finished_at = time.time()
                return ""

        result = ""
        for line in resp.iter_lines():
            if line and line.startswith(b"data: "):
                chunk = line[6:]
                if chunk == b"[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    self._log("[llm] malformed stream chunk")
                    continue
                choice = data.get("choices", [{}])[0]
                delta = choice.get("delta", {}).get("content", "") or ""
                # Some models (GLM-5, Kimi) stream reasoning in a separate field — ignore it
                if not delta:
                    delta = ""
                result += delta or ""

        # Strip Kimi/Qwen chain-of-thought reasoning tags
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.DOTALL)
        return result.strip()

    def _cleanup_transcript(self, transcript, context, state=None):
        app_name = context.get("app_name") or "unknown"
        vocabulary = context.get("vocabulary") or []
        vocab_line = ", ".join(vocabulary[:40]) if vocabulary else "none"
        existing_text = self._current_context
        if state:
            state.cleanup_started_at = time.time()

        system_prompt = _build_cleanup_prompt(app_name)

        context_instruction = ""
        if existing_text:
            snippet = existing_text[-300:]
            context_instruction = (
                f"The user is dictating into a text field that already contains this text "
                f"(last 300 chars shown): '{snippet}'. "
                "Continue naturally from that context. Do not repeat information already present. "
                "If the dictation starts mid-sentence (no capital), preserve that casing.\n"
            )

        user_content = (
            f"Frontmost app: {app_name}\n"
            f"Preferred vocabulary: {vocab_line}\n"
            f"{context_instruction}"
            "Apply only minimal punctuation/capitalization cleanup.\n"
            f"Transcript: {transcript}"
        )

        result = self._call_llm(system_prompt, user_content, state=state)

        if state:
            state.cleanup_finished_at = time.time()
        if result:
            self._log(f"[llm] \"{result}\"")
        return result

    def _cleanup_transcript_async(self, raw_text: str, context: dict, state, injected_app: str):
        """
        Background worker: run LLM cleanup on raw_text that was already injected,
        then replace it in-place if the result differs meaningfully AND the focused
        app is still the same as when the text was injected.
        """
        cleaned = self._cleanup_transcript(raw_text, context, state)
        if not cleaned:
            return
        cleaned = cleaned.strip()
        # Meaningful difference check: skip if only whitespace differs
        if cleaned.lower() == raw_text.lower():
            self._log("[llm] in-place skipped (no meaningful change)")
            return
        # App-change guard: don't stomp a different window
        current_app = self._frontmost_app_name()
        if current_app and injected_app and current_app != injected_app:
            self._log(f"[llm] in-place skipped (app changed: '{injected_app}' -> '{current_app}')")
            return
        # Delete the raw text and type the cleaned version
        self._log(f"[llm] replaced in-place: \"{raw_text}\" -> \"{cleaned}\"")
        # Small guard: only replace if lengths are plausible (not a runaway LLM response)
        if len(cleaned) > len(raw_text) * 3:
            self._log("[llm] in-place skipped (cleaned text suspiciously long)")
            return
        chars_to_delete = len(raw_text)
        self._delete_text(chars_to_delete)
        self._inject_text(cleaned)
        # Update last_result so corrections reference the cleaned version
        self.last_result = cleaned
        if state:
            state.final_text = cleaned
            if state.final_source:
                state.final_source += "+async_cleanup"

    def _reconcile_transcripts(self, stream_preview, batch_transcript, context, state=None):
        if time.time() < self._rate_limit_backoff_until:
            return ""
        app_name = context.get("app_name") or "unknown"
        vocabulary = context.get("vocabulary") or []
        vocab_line = ", ".join(vocabulary[:40]) if vocabulary else "none"
        if state:
            state.reconcile_started_at = time.time()

        user_content = (
            f"Frontmost app: {app_name}\n"
            f"Preferred vocabulary: {vocab_line}\n"
            f"Streaming transcript: {stream_preview}\n"
            f"Batch transcript: {batch_transcript}"
        )
        result = self._call_llm(RECONCILE_PROMPT, user_content, state=state)

        if state:
            state.reconcile_finished_at = time.time()
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
                self._inject_text(command["text"])
                self._remember_result(command["text"])
                self.last_paste_time = time.time()
                self.correction_window_until = self.last_paste_time + CORRECTION_WINDOW_SECONDS
                self._learn_correction(target, command["text"])
                if state:
                    state.final_text = command["text"]
            self.correction_mode = False
            self._play("Pop")
        elif kind == "insert":
            self._inject_text(command["text"])
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
            if self._should_use_clipboard():
                self._delete_text(to_delete)
            else:
                self._delete_text(to_delete)
        suffix = target[prefix:]
        if suffix:
            self._inject_text(suffix)
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

    # ── Clipboard paste fallback ────────────────────────────────────────────

    # Apps known to reject CGEvent keyboard simulation (sandboxed, Electron, etc.)
    KNOWN_CLIPBOARD_APPS = {
        "1Password",
        "Bitwarden",
        "Discord",
        "Figma",
        "Notion",
        "Signal",
        "Slack",
        "Spotify",
        "Teams",
        "WhatsApp",
    }

    def _should_use_clipboard(self) -> bool:
        """Check if clipboard paste mode should be used."""
        # User preference overrides everything
        if self._clipboard_mode_enabled:
            return True
        # Auto-detect: frontmost app is known to reject CGEvent
        app_name = self._frontmost_app_name()
        if app_name in self.KNOWN_CLIPBOARD_APPS:
            return True
        return False

    def _type_text_via_clipboard(self, text: str):
        """Insert text by saving it to clipboard and simulating Cmd+V.

        This is a fallback for apps that reject CGEvent keyboard simulation.
        The user's original clipboard contents are restored after paste.
        """
        if not text:
            return
        pb = NSPasteboard.generalPasteboard()

        # Save current clipboard contents
        saved_clipboard = None
        try:
            saved_clipboard = pb.stringForType_(NSPasteboardTypeString)
        except Exception:
            pass

        # Set clipboard to our text
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

        # Small delay to ensure clipboard is set
        time.sleep(0.02)

        # Simulate Cmd+V
        cmd_key = 55  # kVK_Command
        v_keycode = 9  # kVK_ANSI_V

        cmd_down = CGEventCreateKeyboardEvent(None, cmd_key, True)
        v_down = CGEventCreateKeyboardEvent(None, v_keycode, True)
        v_up = CGEventCreateKeyboardEvent(None, v_keycode, False)
        cmd_up = CGEventCreateKeyboardEvent(None, cmd_key, False)

        # Set Cmd flag on all events
        cmd_flag = 1 << 20  # kCGEventFlagMaskCommand
        CGEventSetFlags(cmd_down, cmd_flag)
        CGEventSetFlags(v_down, cmd_flag)
        CGEventSetFlags(v_up, cmd_flag)
        CGEventSetFlags(cmd_up, cmd_flag)

        CGEventPost(kCGHIDEventTap, cmd_down)
        CGEventPost(kCGHIDEventTap, v_down)
        CGEventPost(kCGHIDEventTap, v_up)
        CGEventPost(kCGHIDEventTap, cmd_up)

        # Wait for paste to complete before restoring clipboard
        time.sleep(0.05)

        # Restore original clipboard contents
        try:
            pb.clearContents()
            if saved_clipboard is not None:
                pb.setString_forType_(saved_clipboard, NSPasteboardTypeString)
        except Exception:
            pass

        self._log(f"[clipboard] pasted {len(text)} chars via clipboard fallback")

    def _delete_text_via_clipboard(self, count: int):
        """Delete text by selecting it and replacing via clipboard.

        Used as fallback when CGEvent backspace simulation fails.
        Selects characters via Shift+Left, then deletes via Backspace.
        """
        if count <= 0:
            return

        # Try regular CGEvent delete first
        self._delete_text(count)
        return

    def _inject_text(self, text: str):
        """Insert text using the best available method.

        Tries CGEvent first, falls back to clipboard paste if the app
        is known to reject simulated keystrokes or clipboard mode is enabled.
        """
        if not text:
            return

        if self._should_use_clipboard():
            self._log("[inject] using clipboard paste method")
            self._type_text_via_clipboard(text)
        else:
            self._log("[inject] using CGEvent keyboard simulation")
            self._type_text(text)

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

        # Derive the fields required for persistent metrics and write off the hot path.
        source = state.final_source or ""
        if "stream" in source:
            stt_provider = "deepgram_stream"
        elif "batch+chunked" in source:
            stt_provider = "deepgram_batch_chunked"
        elif "batch" in source:
            stt_provider = "deepgram_batch"
        else:
            stt_provider = source or "unknown"

        # End-to-end latency: key press to text injected.
        # record_ms covers press->release; release_to_final_ms covers release->inject.
        e2e_ms = None
        if record_ms is not None and metrics.get("release_to_final_ms") is not None:
            e2e_ms = record_ms + metrics["release_to_final_ms"]
        elif record_ms is not None:
            e2e_ms = record_ms

        audio_duration_ms = int(record_ms) if record_ms is not None else None

        persistent = {
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "latency_ms": e2e_ms,
            "audio_duration_ms": audio_duration_ms,
            "stt_provider": stt_provider,
            "stt_success": bool(final_text),
            "inject_success": bool(final_text),
            "word_count": final_words,
            "error": "rate_limited" if state.rate_limited else (
                "stream_failed" if state.stream_failed else None
            ),
        }
        threading.Thread(
            target=self._persist_metrics, args=(persistent,), daemon=True
        ).start()

    def _persist_metrics(self, record: dict):
        """Append one JSONL record to ~/.bolo/metrics.jsonl (called from background thread)."""
        try:
            metrics_dir = os.path.dirname(BOLO_METRICS_FILE)
            os.makedirs(metrics_dir, exist_ok=True)
            line = json.dumps(record, separators=(",", ":")) + "\n"
            with open(BOLO_METRICS_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as e:
            self._log(f"[metrics] failed to persist record: {e}")

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
        # Cancel any pending overlay hide timer and force hide overlay before
        # invalidating the session, so background threads don't get stuck
        if self._overlay_hide_timer is not None:
            self._overlay_hide_timer.cancel()
            self._overlay_hide_timer = None
        self.overlay.hide()
        self.recording = False
        self.icon = ICON_IDLE
        self.title = "⌥"
        self._active_session_id += 1
        self._session_phase = "idle"
        # Synchronously close audio stream to dismiss macOS recording indicator
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self._stt:
            self._stt.close()
            self._stt = None
        with self._warm_stt_lock:
            warm_stt = self._warm_stt
            self._warm_stt = None
            self._warm_stt_connecting = False
        if warm_stt:
            warm_stt.close()
        if self._ns_monitor is not None:
            NSEvent.removeMonitor_(self._ns_monitor)
            self._ns_monitor = None
        self.overlay.hide()
        # Instance lock is released by the atexit handler registered at startup.
        rumps.quit_application()


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pathlib
    import sys

    # ── Single-instance guard ─────────────────────────────────────────────────
    # Use a lock directory (atomic on all POSIX systems) so two launches of
    # bolo.py (e.g. Bolo.app + start-bolo.command) cannot coexist.
    _LOCK_DIR = pathlib.Path("/tmp/bolo-instance.lock")
    try:
        _LOCK_DIR.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        _logger.info("[startup] another instance already running (lock exists). Exiting.")
        sys.exit(0)

    def _release_instance_lock():
        try:
            _LOCK_DIR.rmdir()
        except Exception:
            pass

    import atexit
    
    def _cleanup_and_release_lock():
        # Reset icon on normal exit
        if _app_instance:
            _app_instance.recording = False
            _app_instance.icon = ICON_IDLE
            _app_instance.title = "⌥"
            _app_instance.overlay.hide()
        _release_instance_lock()
    
    atexit.register(_cleanup_and_release_lock)

    # Also release on SIGTERM/SIGINT so the supervisor can clean up cleanly.
    _app_instance = None  # Will hold the BoloApp instance for signal handlers
    
    def _signal_handler(signum, frame):
        # Clean shutdown: close audio stream first to dismiss system recording indicator
        if _app_instance:
            _app_instance.recording = False
            # Force close the audio stream to dismiss macOS recording indicator
            if _app_instance.stream:
                try:
                    _app_instance.stream.stop()
                    _app_instance.stream.close()
                except Exception:
                    pass
            _app_instance.icon = ICON_IDLE
            _app_instance.title = "⌥"
            _app_instance.overlay.hide()
        _release_instance_lock()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # ── API key check ─────────────────────────────────────────────────────────
    if not TELNYX_API_KEY:
        _logger.error("ERROR: TELNYX_API_KEY not set. Add to ~/.codex/.env and restart.")
        sys.exit(1)

    def _crash_handler(signum, frame):
        _logger.error(f"[crash] received signal {signum}")
        traceback.print_stack(frame)
    for _sig in (signal.SIGABRT, signal.SIGBUS, signal.SIGSEGV):
        try:
            signal.signal(_sig, _crash_handler)
        except (OSError, ValueError):
            pass

    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory)
    _app_instance = BoloApp()
    _app_instance.run()
