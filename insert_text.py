#!/usr/bin/env python3
"""Paste text while preserving the full macOS pasteboard payload."""

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
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
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


def main():
    text = sys.stdin.read()
    if not text:
        return 0

    pasteboard = NSPasteboard.generalPasteboard()
    snapshot = snapshot_pasteboard(pasteboard)
    pasteboard.clearContents()
    if not pasteboard.setString_forType_(text, NSStringPboardType):
        return 2

    temporary_change_count = pasteboard.changeCount()
    post_cmd_v()

    externally_changed = wait_for_pasteboard_to_change(
        pasteboard,
        temporary_change_count,
        text,
    )
    if not externally_changed:
        restore_pasteboard(pasteboard, snapshot)

    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        sys.stdin.read()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
