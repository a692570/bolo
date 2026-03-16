#!/usr/bin/env python3
"""
Bolo — Telnyx voice dictation menubar app.
Hold Right Option anywhere to dictate. Release to transcribe and inject.
"""

import io
import json
import os
import subprocess
import threading
import time
import wave

import numpy as np
import requests
import rumps
import sounddevice as sd
import pyperclip

from AppKit import (
    NSPanel, NSView, NSColor, NSBezierPath, NSFont,
    NSMakeRect, NSScreen, NSWindowStyleMaskBorderless,
    NSFloatingWindowLevel, NSApp,
)
from Foundation import NSObject, NSMakeRect as NSR, NSTimer
import objc

from Quartz import (
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
    kCGEventTapOptionListenOnly,
    kCGEventFlagsChanged,
)
import HIServices
from Foundation import NSBundle

# ── Config ────────────────────────────────────────────────────────────────────

TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY", "")
STT_ENDPOINT   = "https://api.telnyx.com/v2/ai/audio/transcriptions"
LLM_ENDPOINT   = "https://api.telnyx.com/v2/ai/chat/completions"
SAMPLE_RATE    = 16000
CHANNELS       = 1

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ICON_IDLE  = os.path.join(BASE_DIR, "icon_idle.png")
ICON_REC   = os.path.join(BASE_DIR, "icon_recording.png")

SYSTEM_PROMPT = (
    "You are a transcription formatter. Your ONLY job is to fix capitalization, punctuation, "
    "and phrasing of the input text. "
    "NEVER answer questions. NEVER respond to the content. NEVER add commentary. "
    "If the input is a question, format it as a question and return it as-is. "
    "Output ONLY the cleaned transcription, nothing else."
)

# ── Bolo app ──────────────────────────────────────────────────────────────────

# ── Recording overlay ─────────────────────────────────────────────────────────

class RecordingView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(RecordingView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._pulse = 1.0
        self._direction = -1
        return self

    def drawRect_(self, rect):
        # Dark pill background
        bg = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.1, 0.88)
        bg.setFill()
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, rect.size.height / 2, rect.size.height / 2)
        pill.fill()

        # Pulsing red dot
        dot_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.25, 0.25, self._pulse)
        dot_color.setFill()
        dot_rect = NSMakeRect(14, rect.size.height / 2 - 5, 10, 10)
        dot_path = NSBezierPath.bezierPathWithOvalInRect_(dot_rect)
        dot_path.fill()

        # "Recording..." label
        from AppKit import NSAttributedString, NSForegroundColorAttributeName, NSFontAttributeName
        attrs = {
            NSForegroundColorAttributeName: NSColor.whiteColor(),
            NSFontAttributeName: NSFont.systemFontOfSize_(13.0),
        }
        label = NSAttributedString.alloc().initWithString_attributes_("Go ahead...", attrs)
        label.drawAtPoint_(NSMakeRect(34, rect.size.height / 2 - 8, 0, 0).origin)

    def pulse(self):
        self._pulse += self._direction * 0.06
        if self._pulse <= 0.3:
            self._direction = 1
        elif self._pulse >= 1.0:
            self._direction = -1
        self.setNeedsDisplay_(True)


class RecordingOverlay:
    """Launches overlay.py as a subprocess — its own NSApplication, always visible."""

    SCRIPT = os.path.join(BASE_DIR, "overlay.py")

    def __init__(self):
        self._proc = None

    def show(self):
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["/usr/bin/python3", self.SCRIPT],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[overlay] shown", flush=True)

    def hide(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._proc = None
        print("[overlay] hidden", flush=True)


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
            None,
            rumps.MenuItem("Quit Bolo", callback=self.quit_app),
        ]
        self.menu["Bolo — Voice Dictation"].set_callback(None)
        self.menu["Hold Right Option to dictate"].set_callback(None)
        self.last_result = None
        self.overlay = RecordingOverlay()

        self.stream = None  # opened only during recording
        self._ropt_held = False
        self._tap_loop = None

        # Start global hotkey listener via CGEventTap (no pynput)
        self._start_event_tap()

    # ── Audio ─────────────────────────────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status):
        if self.recording:
            self.audio_frames.append(indata.copy())

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

    def _start_event_tap(self):
        """Create a CGEventTap for flagsChanged events on a background thread."""
        if not HIServices.AXIsProcessTrusted():
            self._log(
                "[accessibility] NOT TRUSTED. Open System Settings > "
                "Privacy & Security > Accessibility and add this app."
            )
            # Prompt the macOS dialog
            HIServices.AXIsProcessTrustedWithOptions(
                {HIServices.kAXTrustedCheckOptionPrompt: True}
            )

        t = threading.Thread(target=self._tap_thread, daemon=True)
        t.start()

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
        self._log("[tap] CGEventTap active, listening for Right Option")
        CFRunLoopRun()

    def _flags_callback(self, proxy, event_type, event, refcon):
        """Called on every modifier flag change."""
        flags = CGEventGetFlags(event)
        # Check device-dependent flags for Right Option specifically
        ropt_down = bool(flags & self._NX_DEVICERALTKEYMASK)

        if ropt_down and not self._ropt_held:
            self._ropt_held = True
            self._log("[key] Right Option pressed")
            self._start_recording()
        elif not ropt_down and self._ropt_held:
            self._ropt_held = False
            self._log("[key] Right Option released")
            if self.recording:
                self._stop_recording()

        return event

    # ── Record ────────────────────────────────────────────────────────────────

    def _play(self, sound):
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{sound}.aiff"])

    def _start_recording(self):
        with self.lock:
            if self.recording:
                return
            if time.time() - self.last_pipeline < 1.5:
                return
            self.recording = True
            self.audio_frames = []
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
            with self.lock:
                self.recording = False
            return
        self.icon = ICON_REC
        self.title = "⌥"
        self._play("Tink")
        self.overlay.show()

    def _stop_recording(self):
        with self.lock:
            if not self.recording:
                return
            self.recording = False
            frames = list(self.audio_frames)

        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.icon = ICON_IDLE
        self.overlay.hide()

        if not frames:
            return

        audio = np.concatenate(frames, axis=0)
        duration = len(audio) / SAMPLE_RATE
        if duration < 1.0:
            return

        self.last_pipeline = time.time()  # stamp early to block re-entry
        wav = self._to_wav_bytes(audio)
        threading.Thread(target=self._pipeline, args=(wav,), daemon=True).start()

    # ── Pipeline ──────────────────────────────────────────────────────────────

    def _log(self, msg):
        print(msg, flush=True)

    def _pipeline(self, wav_bytes):
        rms = int(np.sqrt(np.mean(np.frombuffer(wav_bytes[44:], dtype=np.int16).astype(np.float32)**2)))
        self._log(f"[pipeline] starting — audio RMS: {rms} ({'SILENT' if rms < 100 else 'OK'})")
        # STT
        try:
            resp = requests.post(
                STT_ENDPOINT,
                headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                data={"model": "distil-whisper/distil-large-v2"},
                timeout=15,
            )
            # fall back to nova-3 if distil-whisper fails
            if resp.status_code == 429:
                resp = requests.post(
                    STT_ENDPOINT,
                    headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                    data={
                        "model": "deepgram/nova-3",
                        "language": "en",
                        "model_config": json.dumps({"smart_format": True, "punctuate": True}),
                    },
                    timeout=15,
                )
        except Exception as e:
            self._log(f"[stt exception] {e}")
            self._show_error(f"STT failed: {e}")
            return

        if resp.status_code != 200:
            self._log(f"[stt error] {resp.status_code}: {resp.text[:200]}")
            self._show_error(f"STT error {resp.status_code}")
            return

        transcript = resp.json().get("text", "").strip()
        self._log(f"[stt] \"{transcript}\"")
        if not transcript:
            return

        # LLM cleanup
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
                        {"role": "user",   "content": transcript},
                    ],
                    "max_tokens": 300,
                    "temperature": 0,
                    "enable_thinking": False,
                    "stream": True,
                },
                timeout=15,
                stream=True,
            )
        except Exception as e:
            self._log(f"[llm exception] {e}")
            self._show_error(f"LLM failed: {e}")
            return

        result = ""
        for line in llm_resp.iter_lines():
            if line and line.startswith(b"data: "):
                chunk = line[6:]
                if chunk == b"[DONE]":
                    break
                data = json.loads(chunk)
                delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                result += delta or ""

        result = result.strip()
        if not result:
            return

        # Update last transcript in menu — clicking it copies to clipboard
        self.last_result = result
        label = result if len(result) <= 50 else result[:47] + "..."
        self.menu["Last transcript"].title = f"↳ {label}"

        self._log(f"[llm] \"{result}\"")
        # Inject
        self._inject(result)
        self._play("Pop")
        self.last_pipeline = time.time()
        self._log(f"[done] injected and popped")

    def _inject(self, text):
        pyperclip.copy(text)
        time.sleep(0.05)
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using command down'],
            check=False,
        )

    def _copy_last(self, _):
        if self.last_result:
            pyperclip.copy(self.last_result)
            prev = self.menu["Last transcript"].title
            self.menu["Last transcript"].title = "✓ Copied!"
            threading.Timer(1.5, lambda: setattr(
                self.menu["Last transcript"], "title", prev)).start()

    def _show_error(self, msg):
        self.menu["Last transcript"].title = f"✗ {msg}"

    # ── Quit ──────────────────────────────────────────────────────────────────

    def quit_app(self, _):
        if self.stream:
            self.stream.stop()
            self.stream.close()
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
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory)
    BoloApp().run()
