"""Unit tests for keyboard-layout resolution in the insertion helper.

Run with: python3 -m pytest tests/
No mic, pasteboard, accessibility, or API key needed.

Most of these drive Apple's stock keyboard layouts directly rather than only
whichever layout this Mac happens to be set to, so a QWERTY machine still
proves the Dvorak behaviour. Layouts ship with macOS, so they are present
whether or not the user has enabled them; any that are missing are skipped.
"""

import ctypes
import ctypes.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from insert_text import (
    COMMAND_MODIFIER_STATE,
    QWERTY_V_KEYCODE,
    key_code_for_character,
    paste_key_code,
    scan_layout_for_character,
)

# Deliberately not a real keycode. Any test that gets this back proves the
# layout scan bailed out instead of finding an answer.
SENTINEL = 199


def installed_layouts():
    """Maps input source ID to that layout's key table pointer."""
    carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))
    core_foundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    carbon.TISCreateInputSourceList.restype = ctypes.c_void_p
    carbon.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core_foundation.CFArrayGetCount.restype = ctypes.c_long
    core_foundation.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    core_foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    core_foundation.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    core_foundation.CFDataGetBytePtr.restype = ctypes.c_void_p
    core_foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    core_foundation.CFStringGetCString.restype = ctypes.c_bool
    core_foundation.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]

    source_id = ctypes.c_void_p.in_dll(carbon, "kTISPropertyInputSourceID")
    layout_property = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
    sources = carbon.TISCreateInputSourceList(None, True)

    layouts = {}
    for index in range(core_foundation.CFArrayGetCount(sources)):
        source = core_foundation.CFArrayGetValueAtIndex(sources, index)
        name_ref = carbon.TISGetInputSourceProperty(source, source_id)
        buffer = ctypes.create_string_buffer(512)
        if not name_ref or not core_foundation.CFStringGetCString(
            name_ref, buffer, 512, 0x08000100
        ):
            continue
        data = carbon.TISGetInputSourceProperty(source, layout_property)
        if not data:
            continue
        pointer = core_foundation.CFDataGetBytePtr(data)
        if pointer:
            layouts[buffer.value.decode()] = pointer
    return layouts


# Where "v" physically sits on each stock layout, measured with Command held,
# which is how a paste shortcut is actually pressed.
EXPECTED_PASTE_KEYCODE = {
    "com.apple.keylayout.US": 9,
    "com.apple.keylayout.Colemak": 9,
    "com.apple.keylayout.French": 9,
    "com.apple.keylayout.German": 9,
    "com.apple.keylayout.Dvorak": 47,
    "com.apple.keylayout.Dvorak-Left": 9,
    "com.apple.keylayout.Dvorak-Right": 43,
    "com.apple.keylayout.DVORAK-QWERTYCMD": 9,
}


def test_paste_keycode_is_read_from_the_layout_not_assumed():
    """Passing a sentinel fallback means a silently broken scan cannot pass by
    coincidentally returning the right QWERTY number."""
    assert key_code_for_character("v", SENTINEL) != SENTINEL


def test_unmappable_character_falls_back():
    """No layout produces this glyph on a plain keypress, so the scan must
    exhaust and hand back the caller's fallback."""
    assert key_code_for_character("ӿ", SENTINEL) == SENTINEL


def test_letters_resolve_to_distinct_valid_keycodes():
    """Layout-independent sanity: three different letters cannot share one
    physical key, and every virtual keycode is below 128."""
    codes = [key_code_for_character(letter, SENTINEL) for letter in ("a", "s", "v")]
    assert all(code != SENTINEL for code in codes)
    assert all(0 <= code < 128 for code in codes)
    assert len(set(codes)) == 3


def test_paste_key_code_resolves_v_with_command_held():
    assert paste_key_code() == key_code_for_character(
        "v", QWERTY_V_KEYCODE, modifier_state=COMMAND_MODIFIER_STATE
    )


def test_stock_layouts_resolve_to_their_real_paste_keys():
    """The actual point of the change: Dvorak's "v" is not where QWERTY's is.

    Runs against the real system layouts, so this proves the Dvorak behaviour
    from a QWERTY machine.
    """
    layouts = installed_layouts()
    checked = 0
    for name, expected in EXPECTED_PASTE_KEYCODE.items():
        layout = layouts.get(name)
        if layout is None:
            continue
        actual = scan_layout_for_character(
            layout, "v", COMMAND_MODIFIER_STATE, SENTINEL
        )
        assert actual == expected, f"{name}: expected {expected}, got {actual}"
        checked += 1
    assert checked >= 4, f"only {checked} stock layouts available to check"


def test_dvorak_qwerty_command_needs_the_command_modifier():
    """The regression guard.

    "Dvorak - QWERTY Command" snaps back to QWERTY positions while Command is
    held, which is the whole reason people choose it. Resolving without the
    modifier returns 47 and would paste with the wrong physical key, breaking
    exactly the users this change is meant to fix.
    """
    layout = installed_layouts().get("com.apple.keylayout.DVORAK-QWERTYCMD")
    if layout is None:
        return
    assert scan_layout_for_character(layout, "v", 0, SENTINEL) == 47
    assert scan_layout_for_character(layout, "v", COMMAND_MODIFIER_STATE, SENTINEL) == 9
