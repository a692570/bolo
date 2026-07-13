"""Unit tests for the Inserter module: render diffing and strategy pick.

Run with: python3 -m pytest tests/
No mic, pasteboard, accessibility, or API key needed. RecordingInserter
overrides inject/delete so render's diff logic is exercised through the
module's own interface without posting real CGEvents.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inserter import Inserter, KNOWN_CLIPBOARD_APPS


class RecordingInserter(Inserter):
    def __init__(self):
        super().__init__(
            frontmost_app=lambda: "TestApp",
            clipboard_mode_enabled=lambda: False,
        )
        self.ops = []

    def inject(self, text):
        self.ops.append(("inject", text))

    def delete(self, count):
        self.ops.append(("delete", count))


def test_render_appends_suffix():
    ins = RecordingInserter()
    injected = ins.render("hello", "hello world")
    assert injected == " world"
    assert ins.ops == [("inject", " world")]


def test_render_corrects_mid_word():
    ins = RecordingInserter()
    injected = ins.render("hello wrld", "hello world")
    assert injected == "orld"
    assert ins.ops == [("delete", 3), ("inject", "orld")]


def test_render_from_empty():
    ins = RecordingInserter()
    injected = ins.render("", "hello")
    assert injected == "hello"
    assert ins.ops == [("inject", "hello")]


def test_render_deletion_only():
    ins = RecordingInserter()
    injected = ins.render("hello world", "hello")
    assert injected == ""
    assert ins.ops == [("delete", 6)]


def test_render_noop():
    ins = RecordingInserter()
    injected = ins.render("same", "same")
    assert injected == ""
    assert ins.ops == []


def test_render_none_target_clears():
    ins = RecordingInserter()
    injected = ins.render("abc", None)
    assert injected == ""
    assert ins.ops == [("delete", 3)]


def test_use_clipboard_when_pref_enabled():
    ins = Inserter(
        frontmost_app=lambda: "TextEdit",
        clipboard_mode_enabled=lambda: True,
    )
    assert ins._use_clipboard() is True


def test_use_clipboard_for_known_app():
    ins = Inserter(
        frontmost_app=lambda: "Slack",
        clipboard_mode_enabled=lambda: False,
    )
    assert "Slack" in KNOWN_CLIPBOARD_APPS
    assert ins._use_clipboard() is True


def test_keystrokes_for_normal_app():
    ins = Inserter(
        frontmost_app=lambda: "TextEdit",
        clipboard_mode_enabled=lambda: False,
    )
    assert ins._use_clipboard() is False
