"""Unit tests for parse_command: exact matches, punctuation tolerance,
prefix commands, and non-command fallthrough."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands import parse_command


def test_scratch_that_exact():
    assert parse_command("scratch that")["kind"] == "scratch"


def test_scratch_that_with_stt_punctuation_and_case():
    assert parse_command("Scratch that.")["kind"] == "scratch"
    assert parse_command("Scratch that!")["kind"] == "scratch"


def test_scratch_that_inside_longer_utterance_is_not_a_command():
    assert parse_command("Okay testing this. Scratch that.") is None


def test_new_paragraph():
    assert parse_command("New paragraph.")["text"] == "\n\n"


def test_bullet_prefix_keeps_payload():
    command = parse_command("bullet buy milk")
    assert command["kind"] == "insert"
    assert command["text"] == "\n- buy milk"


def test_actually_requires_correction_window():
    assert parse_command("actually send it tomorrow", correction_active=False) is None
    command = parse_command("actually send it tomorrow", correction_active=True)
    assert command["kind"] == "replace"
    assert command["text"] == "send it tomorrow"


def test_punctuation_words():
    assert parse_command("Comma.")["text"] == ", "
    assert parse_command("question mark")["text"] == "? "


def test_normal_dictation_is_not_a_command():
    assert parse_command("The correction part didn't work.") is None
    assert parse_command("") is None
    assert parse_command(None) is None
