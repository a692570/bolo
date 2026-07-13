"""Text insertion module: puts transcript text into the focused app.

Interface: render(previous, target) / inject(text) / delete(count).
Picks CGEvent typing or clipboard paste internally; callers never choose.
Clipboard paste preserves the full pasteboard payload (all types, not just
plain text) and skips the restore if something else wrote to the pasteboard
in the meantime, via the helpers shared with insert_text.py.
"""

from AppKit import NSPasteboard, NSStringPboardType
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSetFlags,
    kCGHIDEventTap,
)

from insert_text import (
    post_cmd_v,
    restore_pasteboard,
    snapshot_pasteboard,
    wait_for_pasteboard_to_change,
)
from transcript_state import longest_common_prefix

DELETE_KEYCODE = 51

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


class Inserter:
    def __init__(self, frontmost_app, clipboard_mode_enabled, log=None):
        """frontmost_app and clipboard_mode_enabled are zero-arg callables."""
        self._frontmost_app = frontmost_app
        self._clipboard_mode_enabled = clipboard_mode_enabled
        self._log = log or (lambda message: None)

    def render(self, previous, target):
        """Morph on-screen text from previous to target. Returns the injected
        suffix ("" when the change was deletion-only or a no-op)."""
        previous = previous or ""
        target = target or ""
        prefix = longest_common_prefix(previous, target)
        to_delete = len(previous) - prefix
        if to_delete > 0:
            self.delete(to_delete)
        suffix = target[prefix:]
        if suffix:
            self.inject(suffix)
        return suffix

    def inject(self, text):
        if not text:
            return
        if self._use_clipboard():
            self._log("[inject] using clipboard paste method")
            self._paste_via_clipboard(text)
        else:
            self._log("[inject] using CGEvent keyboard simulation")
            self._type_keystrokes(text)

    def delete(self, count):
        for _ in range(max(0, count)):
            down = CGEventCreateKeyboardEvent(None, DELETE_KEYCODE, True)
            up = CGEventCreateKeyboardEvent(None, DELETE_KEYCODE, False)
            CGEventSetFlags(down, 0)
            CGEventSetFlags(up, 0)
            CGEventPost(kCGHIDEventTap, down)
            CGEventPost(kCGHIDEventTap, up)

    def _use_clipboard(self):
        if self._clipboard_mode_enabled():
            return True
        return self._frontmost_app() in KNOWN_CLIPBOARD_APPS

    def _type_keystrokes(self, text):
        down = CGEventCreateKeyboardEvent(None, 0, True)
        up = CGEventCreateKeyboardEvent(None, 0, False)
        CGEventKeyboardSetUnicodeString(down, len(text), text)
        CGEventKeyboardSetUnicodeString(up, len(text), text)
        CGEventSetFlags(down, 0)
        CGEventSetFlags(up, 0)
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)

    def _paste_via_clipboard(self, text):
        pasteboard = NSPasteboard.generalPasteboard()
        snapshot = snapshot_pasteboard(pasteboard)
        pasteboard.clearContents()
        if not pasteboard.setString_forType_(text, NSStringPboardType):
            self._log("[clipboard] failed to set pasteboard, falling back to keystrokes")
            self._type_keystrokes(text)
            return
        temporary_change_count = pasteboard.changeCount()
        post_cmd_v()
        externally_changed = wait_for_pasteboard_to_change(
            pasteboard,
            temporary_change_count,
            text,
        )
        if not externally_changed:
            restore_pasteboard(pasteboard, snapshot)
        self._log(f"[clipboard] pasted {len(text)} chars via clipboard fallback")
