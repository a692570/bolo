#!/usr/bin/env python3
"""Paste text while preserving the full macOS pasteboard payload.

Also holds the direct-typing insert (``type_text``): synthetic Unicode
keystrokes for targets that ignore Cmd+V, driven by the accessibility daemon.
"""

import ctypes
import ctypes.util
import os
import select
import sys
import time
import warnings

from objc import ObjCPointerWarning
from AppKit import NSPasteboard, NSPasteboardItem, NSStringPboardType
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventKeyboardGetUnicodeString,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskShift,
    kCGHIDEventTap,
)


warnings.filterwarnings("ignore", category=ObjCPointerWarning)


RESTORE_TIMEOUT = float(os.environ.get("BOLO_INSERT_RESTORE_TIMEOUT", "0.35"))


def snapshot_pasteboard(pasteboard):
    items = []
    for item in pasteboard.pasteboardItems() or []:
        data_by_type = {}
        for item_type in item.types() or []:
            data = item.dataForType_(item_type)
            if data is not None:
                data_by_type[item_type] = bytes(data)
        if data_by_type:
            items.append(data_by_type)
    return items


def restore_pasteboard(pasteboard, snapshot):
    pasteboard.clearContents()
    restored = []
    for data_by_type in snapshot:
        item = NSPasteboardItem.alloc().init()
        for item_type, data in data_by_type.items():
            item.setData_forType_(data, item_type)
        restored.append(item)
    if restored:
        pasteboard.writeObjects_(restored)


QWERTY_V_KEYCODE = 9
_VIRTUAL_KEYCODE_COUNT = 128
_UC_KEY_ACTION_DISPLAY = 3
_UC_KEY_TRANSLATE_NO_DEAD_KEYS_MASK = 1
# UCKeyTranslate takes the modifier bits shifted right by 8, so cmdKey (0x0100)
# is 1. Resolving with Command held is what makes "Dvorak - QWERTY Command"
# work: that layout deliberately snaps back to QWERTY positions for shortcuts,
# so its "v" is keycode 47 unmodified but 9 under Command, and a paste resolved
# without the modifier would press the wrong physical key.
COMMAND_MODIFIER_STATE = 1


def _load_keyboard_layout_api():
    """Carbon + CoreFoundation entry points needed to read the active layout.

    Returns None when either framework is unavailable, so callers fall back to
    the QWERTY keycode rather than failing to paste.
    """
    carbon_path = ctypes.util.find_library("Carbon")
    core_foundation_path = ctypes.util.find_library("CoreFoundation")
    if not carbon_path or not core_foundation_path:
        return None
    try:
        carbon = ctypes.CDLL(carbon_path)
        core_foundation = ctypes.CDLL(core_foundation_path)
    except OSError:
        return None

    # Every restype below matters: ctypes defaults to c_int, which truncates
    # 64-bit pointers and would hand UCKeyTranslate a garbage layout address.
    carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
    carbon.TISCopyCurrentKeyboardLayoutInputSource.argtypes = []
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    carbon.LMGetKbdType.restype = ctypes.c_uint8
    carbon.LMGetKbdType.argtypes = []
    carbon.UCKeyTranslate.restype = ctypes.c_int32
    carbon.UCKeyTranslate.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint16,
        ctypes.c_uint16,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint16),
    ]
    core_foundation.CFDataGetBytePtr.restype = ctypes.c_void_p
    core_foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    return carbon, core_foundation


def scan_layout_for_character(layout, character, modifier_state, fallback):
    """Search one layout's key table for the keycode producing `character`.

    Split out from the active-layout lookup so tests can drive any installed
    layout, not just whichever one this machine happens to be using.
    """
    api = _load_keyboard_layout_api()
    if api is None or not layout:
        return fallback
    carbon, _ = api
    keyboard_type = carbon.LMGetKbdType()
    target = ord(character)
    buffer = (ctypes.c_uint16 * 4)()
    length = ctypes.c_uint32()
    for key_code in range(_VIRTUAL_KEYCODE_COUNT):
        dead_key_state = ctypes.c_uint32(0)
        length.value = 0
        status = carbon.UCKeyTranslate(
            layout,
            key_code,
            _UC_KEY_ACTION_DISPLAY,
            modifier_state,
            keyboard_type,
            _UC_KEY_TRANSLATE_NO_DEAD_KEYS_MASK,
            ctypes.byref(dead_key_state),
            len(buffer),
            ctypes.byref(length),
            buffer,
        )
        if status != 0 or length.value == 0:
            continue
        if buffer[0] == target:
            return key_code
    return fallback


def key_code_for_character(character, fallback, modifier_state=0):
    """Virtual keycode that produces `character` under the active layout.

    Synthesized shortcuts carry a keycode, not a character, so a hardcoded
    keycode silently targets the wrong physical key on layouts that move it:
    "v" is keycode 9 on QWERTY, AZERTY, QWERTZ and Colemak, but 47 on Dvorak
    and 43 on Dvorak-Right. Returns `fallback` when the layout cannot be read.
    """
    api = _load_keyboard_layout_api()
    if api is None:
        return fallback
    carbon, core_foundation = api

    source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
    if not source:
        return fallback
    try:
        try:
            layout_property = ctypes.c_void_p.in_dll(
                carbon, "kTISPropertyUnicodeKeyLayoutData"
            )
        except ValueError:
            return fallback
        layout_data = carbon.TISGetInputSourceProperty(source, layout_property)
        if not layout_data:
            return fallback
        layout = core_foundation.CFDataGetBytePtr(layout_data)
        if not layout:
            return fallback
        return scan_layout_for_character(layout, character, modifier_state, fallback)
    finally:
        core_foundation.CFRelease(source)


def paste_key_code():
    """Keycode for Cmd+V under the active layout.

    Resolved with Command held, because that is how the key will actually be
    pressed. Resolved per call rather than cached, so switching layout
    mid-session is picked up on the next paste.
    """
    return key_code_for_character(
        "v", QWERTY_V_KEYCODE, modifier_state=COMMAND_MODIFIER_STATE
    )


def post_cmd_v():
    key_code_v = paste_key_code()
    down = CGEventCreateKeyboardEvent(None, key_code_v, True)
    up = CGEventCreateKeyboardEvent(None, key_code_v, False)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def wait_for_pasteboard_to_change(pasteboard, temporary_change_count, text):
    deadline = time.monotonic() + RESTORE_TIMEOUT
    while time.monotonic() < deadline:
        if pasteboard.changeCount() != temporary_change_count:
            current = pasteboard.stringForType_(NSStringPboardType)
            if current != text:
                return True
        time.sleep(0.025)
    return False


def start_paste(text: str):
    """Snapshot the pasteboard, install ``text``, and post Cmd+V.

    The synchronous half of a paste, split out of ``perform_paste`` so the
    accessibility daemon can answer the moment the keystroke is posted
    instead of waiting out the restore window. Returns the state that
    ``finalize_paste`` consumes, or None when the pasteboard write failed.
    """
    pasteboard = NSPasteboard.generalPasteboard()
    snapshot = snapshot_pasteboard(pasteboard)
    pasteboard.clearContents()
    if not pasteboard.setString_forType_(text, NSStringPboardType):
        return None

    temporary_change_count = pasteboard.changeCount()
    post_cmd_v()
    return {
        "pasteboard": pasteboard,
        "snapshot": snapshot,
        "change_count": temporary_change_count,
        "text": text,
    }


def pasteboard_matches_state(state) -> bool:
    """Whether the pasteboard still holds exactly what ``start_paste`` set.

    The ownership check a deferred restore must pass: any newer paste's
    ``start_paste`` bumps the change count, so a finalize that lost the race
    to a newer paste sees False here and must leave the pasteboard alone.
    """
    pasteboard = state["pasteboard"]
    return (
        pasteboard.changeCount() == state["change_count"]
        and pasteboard.stringForType_(NSStringPboardType) == state["text"]
    )


def finalize_paste(state, restore_decider=None) -> str:
    """Wait out the restore window, then put the snapshot back if nothing did.

    Returns "restored" when the pre-paste contents were written back and
    "external_change" when the pasteboard changed on its own (the target app
    consumed the paste), meaning no restore is needed. Callers whose finalize
    can be superseded by a newer paste pass ``restore_decider``: invoked
    instead of the unconditional restore, it receives the state, performs
    any restore itself under whatever lock makes its ownership check
    atomic, and returns the outcome to report ("restored", "skipped", ...).
    """
    externally_changed = wait_for_pasteboard_to_change(
        state["pasteboard"],
        state["change_count"],
        state["text"],
    )
    if externally_changed:
        return "external_change"
    if restore_decider is None:
        restore_pasteboard(state["pasteboard"], state["snapshot"])
        return "restored"
    return restore_decider(state)


def perform_paste(text: str) -> int:
    """Paste ``text`` via the clipboard and Cmd+V, preserving its payload.

    Runs ``start_paste`` then ``finalize_paste`` back to back; the
    accessibility daemon drives the two halves separately so its reply is
    not delayed by the restore wait. Returns 0 on success and 2 when the
    pasteboard write failed.
    """
    state = start_paste(text)
    if state is None:
        return 2
    finalize_paste(state)
    return 0


# CGEventKeyboardSetUnicodeString stores longer strings, but receiving apps
# read at most 20 UTF-16 units from one keyboard event, so longer runs are
# split across events.
TYPE_CHUNK_UNITS = 20
# Pause after each typed event pair: back-to-back posts can outrun targets
# that drain their key queue slowly (remote desktop clients, Electron apps).
TYPE_EVENT_DELAY = 0.004
# Carrier keycode for Unicode-string events. Apps that honor the attached
# string ignore it; there is no keycode that is neutral everywhere.
UNICODE_EVENT_KEYCODE = 0
RETURN_KEYCODE = 36
NEWLINE = "\n"


def utf16_units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units, the unit the CGEvent API
    counts in. A character outside the BMP (most emoji) is two units."""
    return len(text.encode("utf-16-le")) // 2


def typing_segments(text: str, max_units: int = TYPE_CHUNK_UNITS):
    """Split ``text`` into the strings one keyboard event each will carry.

    Every line break (CRLF, CR or LF) becomes its own ``NEWLINE`` segment,
    typed as a Return key; every other segment fits in ``max_units`` UTF-16
    units. Splits fall on code point boundaries, so a surrogate pair is never
    cut in half (half a pair would reach the target as an invalid lone
    surrogate). The segments concatenate back to the newline-normalized text.
    """
    normalized = text.replace("\r\n", NEWLINE).replace("\r", NEWLINE)
    segments = []
    chunk = []
    chunk_units = 0
    for character in normalized:
        if character == NEWLINE:
            if chunk:
                segments.append("".join(chunk))
                chunk, chunk_units = [], 0
            segments.append(NEWLINE)
            continue
        units = utf16_units(character)
        if chunk and chunk_units + units > max_units:
            segments.append("".join(chunk))
            chunk, chunk_units = [], 0
        chunk.append(character)
        chunk_units += units
    if chunk:
        segments.append("".join(chunk))
    return segments


def _unicode_key_event(segment: str, key_down: bool):
    """One keyboard event carrying ``segment``, or None when Quartz refused
    it or the string did not read back intact."""
    event = CGEventCreateKeyboardEvent(None, UNICODE_EVENT_KEYCODE, key_down)
    if event is None:
        return None
    # Clear the modifiers: a key still physically held (the dictation key
    # itself, a Command a user is resting on) must not turn text into
    # shortcuts.
    CGEventSetFlags(event, 0)
    units = utf16_units(segment)
    CGEventKeyboardSetUnicodeString(event, units, segment)
    read_back = CGEventKeyboardGetUnicodeString(event, units, None, None)
    if tuple(read_back) != (units, segment):
        return None
    return event


def _return_key_event(key_down: bool):
    """Shift+Return: a line break in chat apps (Slack, Teams, browser chat
    boxes) where a bare Return would send the message mid-dictation, and a
    plain newline in editors."""
    event = CGEventCreateKeyboardEvent(None, RETURN_KEYCODE, key_down)
    if event is None:
        return None
    CGEventSetFlags(event, kCGEventFlagMaskShift)
    return event


def build_typing_events(segments):
    """Build every (key down, key up) pair before any is posted.

    Returns None if any event fails to build or verify, so a failure lands
    before the first keystroke and never leaves half the text typed.
    """
    pairs = []
    for segment in segments:
        if segment == NEWLINE:
            down, up = _return_key_event(True), _return_key_event(False)
        else:
            down = _unicode_key_event(segment, True)
            up = _unicode_key_event(segment, False)
        if down is None or up is None:
            return None
        pairs.append((down, up))
    return pairs


def type_text(text: str) -> bool:
    """Type ``text`` into the focused app as synthetic keystrokes.

    The insert for targets that ignore Cmd+V (remote desktops, VMs, some
    terminals); the clipboard is never touched. Returns False, with nothing
    typed, when the events could not be built.
    """
    if not text:
        return True
    pairs = build_typing_events(typing_segments(text))
    if pairs is None:
        return False
    for down, up in pairs:
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)
        time.sleep(TYPE_EVENT_DELAY)
    return True


def main():
    text = sys.stdin.read()
    if not text:
        return 0

    status = perform_paste(text)

    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        sys.stdin.read()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
