"""Unit tests for keyboard-layout resolution and the paste split in the
insertion helper.

Run with: python3 -m pytest tests/
No mic, pasteboard, accessibility, or API key needed.

Most of these drive Apple's stock keyboard layouts directly rather than only
whichever layout this Mac happens to be set to, so a QWERTY machine still
proves the Dvorak behaviour. Layouts ship with macOS, so they are present
whether or not the user has enabled them; any that are missing are skipped.

The mocked-pasteboard section at the bottom drives the real
start_paste/finalize_paste code over fakes, no AX permission or real
keystroke required. tests/test_accessibility_daemon.py imports those fakes
for its daemon-side paste tests, so both files share one pasteboard model.
"""

import ctypes
import ctypes.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AppKit import NSStringPboardType

import insert_text
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


# ---------------------------------------------------------------------------
# Mocked-pasteboard tests for the start_paste / finalize_paste split.
#
# perform_paste's CLI contract (install text, one Cmd+V, restore the snapshot
# when nothing else took the paste) is exercised without AX permission, real
# keystrokes, or the real pasteboard.


class FakePasteboardItem:
    """NSPasteboardItem stand-in: just the type/data pairs the helper uses."""

    def __init__(self, data_by_type=None):
        self._data_by_type = dict(data_by_type or {})

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def types(self):
        return list(self._data_by_type)

    def dataForType_(self, item_type):
        return self._data_by_type.get(item_type)

    def setData_forType_(self, data, item_type):
        self._data_by_type[item_type] = data


class FakePasteboard:
    """String-level pasteboard whose changeCount bumps on every write."""

    def __init__(self, initial_text=""):
        self._items = (
            [FakePasteboardItem({NSStringPboardType: initial_text.encode("utf-8")})]
            if initial_text
            else []
        )
        self._change_count = 0
        self.refuse_writes = False

    def current_text(self):
        return self.stringForType_(NSStringPboardType)

    # -- the NSPasteboard surface insert_text.py touches --

    def pasteboardItems(self):
        return list(self._items)

    def changeCount(self):
        return self._change_count

    def clearContents(self):
        self._items = []
        self._change_count += 1

    def setString_forType_(self, text, item_type):
        if self.refuse_writes:
            return False
        self._items = [FakePasteboardItem({item_type: text.encode("utf-8")})]
        self._change_count += 1
        return True

    def stringForType_(self, item_type):
        for item in self._items:
            data = item.dataForType_(item_type)
            if data is not None:
                return data.decode("utf-8")
        return None

    def writeObjects_(self, items):
        self._items = list(items)
        self._change_count += 1
        return True

    # -- test-side controls --

    def set_string_externally(self, text):
        """Simulate another process writing the pasteboard mid-restore-window."""
        self.clearContents()
        self.setString_forType_(text, NSStringPboardType)


class FakeNSPasteboardModule:
    """Stands in for the NSPasteboard class: one shared fake per test."""

    def __init__(self, instance):
        self._instance = instance

    def generalPasteboard(self):
        return self._instance


def install_mock_paste(
    monkeypatch, initial_text="original clipboard", restore_timeout=1.0
):
    """Point insert_text at a fake pasteboard and a recording Cmd+V.

    Returns ``(fake, posted)``; ``posted`` grows by one per keystroke so a
    test can prove whether the key was pressed at all. Shared with
    tests/test_accessibility_daemon.py for its daemon-side paste tests.
    """
    fake = FakePasteboard(initial_text)
    monkeypatch.setattr(insert_text, "NSPasteboard", FakeNSPasteboardModule(fake))
    monkeypatch.setattr(insert_text, "NSPasteboardItem", FakePasteboardItem)
    posted = []
    monkeypatch.setattr(insert_text, "post_cmd_v", lambda: posted.append(True))
    monkeypatch.setattr(insert_text, "RESTORE_TIMEOUT", restore_timeout)
    return fake, posted


def test_perform_paste_installs_text_presses_once_and_restores_the_snapshot(
    monkeypatch,
):
    """The CLI contract, end to end over the mock: text installed, one
    Cmd+V, and the pre-paste clipboard put back when nothing changed it."""
    fake, posted = install_mock_paste(
        monkeypatch, initial_text="user data", restore_timeout=0.05
    )
    assert insert_text.perform_paste("dictation") == 0
    assert len(posted) == 1
    assert fake.current_text() == "user data"


def test_start_paste_reports_failure_and_perform_paste_keeps_it(monkeypatch):
    """A refused pasteboard write: no keystroke, status 2, no restore."""
    fake, posted = install_mock_paste(monkeypatch, initial_text="user data")
    fake.refuse_writes = True
    assert insert_text.start_paste("dictation") is None
    assert posted == []
    assert insert_text.perform_paste("dictation") == 2


def test_finalize_paste_reports_external_change_and_leaves_the_board_alone(
    monkeypatch,
):
    fake, posted = install_mock_paste(
        monkeypatch, initial_text="user data", restore_timeout=0.05
    )
    state = insert_text.start_paste("dictation")
    assert fake.current_text() == "dictation"
    fake.set_string_externally("the app rewrote it")
    assert insert_text.finalize_paste(state) == "external_change"
    assert fake.current_text() == "the app rewrote it"
    assert len(posted) == 1


def test_pasteboard_matches_state_flips_false_once_a_newer_paste_writes(monkeypatch):
    fake, _posted = install_mock_paste(monkeypatch, initial_text="user data")
    first = insert_text.start_paste("first")
    assert insert_text.pasteboard_matches_state(first) is True
    insert_text.start_paste("second")
    assert fake.current_text() == "second"
    assert insert_text.pasteboard_matches_state(first) is False
