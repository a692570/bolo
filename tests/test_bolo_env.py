"""Tests for the shared ~/.bolo/env writer used by installer and onboarding."""

import os
import stat

import bolo_env


def test_shell_double_quote_escapes_backslash_then_quote():
    backslash = chr(92)
    assert bolo_env.shell_double_quote("plain") == "plain"
    assert bolo_env.shell_double_quote('a"b') == "a" + backslash + '"b'
    assert bolo_env.shell_double_quote("a" + backslash + "b") == "a" + backslash * 2 + "b"
    # Backslash escaping happens before quote escaping, so a literal
    # backslash-quote pair becomes escaped-backslash + escaped-quote.
    expected = "a" + backslash * 3 + '"b'
    assert bolo_env.shell_double_quote('a' + backslash + '"b') == expected


def test_write_env_value_appends_to_new_file_with_private_perms(tmp_path):
    env = tmp_path / "env"

    bolo_env.write_env_value(str(env), "ASSEMBLYAI_API_KEY", "abc123")

    assert env.read_text() == 'ASSEMBLYAI_API_KEY="abc123"\n'
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    # No leftover temp files from the atomic swap.
    assert list(tmp_path.iterdir()) == [env]


def test_write_env_value_replaces_existing_line_only(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        'BOLO_HOTKEY="right_option"\n'
        "ASSEMBLYAI_API_KEY=\"old\"\n"
        "TELNYX_API_KEY=\"keep\"\n"
    )

    bolo_env.write_env_value(str(env), "ASSEMBLYAI_API_KEY", "new")

    assert env.read_text() == (
        'BOLO_HOTKEY="right_option"\n'
        'ASSEMBLYAI_API_KEY="new"\n'
        'TELNYX_API_KEY="keep"\n'
    )


def test_write_env_value_escapes_special_values(tmp_path):
    env = tmp_path / "env"
    backslash = chr(92)
    value = 'a"b' + backslash + "c"

    bolo_env.write_env_value(str(env), "ASSEMBLYAI_API_KEY", value)

    quoted = env.read_text()
    expected = 'ASSEMBLYAI_API_KEY="a' + backslash + '"b' + backslash * 2 + 'c"\n'
    assert quoted == expected
    # Parity with the Rust reader (`read_key_value_file`): surrounding
    # quotes are stripped, embedded escapes are left as written. Real API
    # keys are alphanumeric, so this only matters for adversarial values.
    assert bolo_env.read_env_value(str(env), "ASSEMBLYAI_API_KEY") == (
        'a' + backslash + '"b' + backslash * 2 + "c"
    )


def test_write_env_value_does_not_clobber_prefix_siblings(tmp_path):
    env = tmp_path / "env"
    env.write_text('ASSEMBLYAI_API_KEY_URL="keep"\n')

    bolo_env.write_env_value(str(env), "ASSEMBLYAI_API_KEY", "key")

    # Only an exact `NAME=` prefix matches; longer names are untouched and
    # the new key is appended below them.
    assert env.read_text() == (
        'ASSEMBLYAI_API_KEY_URL="keep"\nASSEMBLYAI_API_KEY="key"\n'
    )


def test_write_env_value_creates_parent_directory(tmp_path):
    env = tmp_path / "nested" / "dir" / "env"

    bolo_env.write_env_value(str(env), "BOLO_HOTKEY", "right_option")

    assert stat.S_IMODE((tmp_path / "nested" / "dir").stat().st_mode) == 0o700
    assert bolo_env.read_env_value(str(env), "BOLO_HOTKEY") == "right_option"


def test_read_env_value_handles_bare_and_quoted_formats(tmp_path):
    env = tmp_path / "env"
    env.write_text("BOLO_HOTKEY=right_option\nASSEMBLYAI_API_KEY=\"quoted\"\n")

    assert bolo_env.read_env_value(str(env), "BOLO_HOTKEY") == "right_option"
    assert bolo_env.read_env_value(str(env), "ASSEMBLYAI_API_KEY") == "quoted"
    assert bolo_env.read_env_value(str(env), "MISSING") is None
    assert bolo_env.read_env_value(str(tmp_path / "nope"), "X") is None


def test_write_env_value_round_trips_through_rust_reader_format(tmp_path):
    """The line format must match what the Rust runtime parses: NAME="escaped"."""
    env = tmp_path / "env"
    backslash = chr(92)
    value = 'tricky"' + backslash + "value"

    bolo_env.write_env_value(str(env), "ASSEMBLYAI_API_KEY", value)

    expected_line = (
        'ASSEMBLYAI_API_KEY="tricky' + backslash + '"' + backslash * 2 + 'value"'
    )
    assert env.read_text().splitlines()[0] == expected_line
    # Rust-side parse result: quotes stripped, escapes preserved.
    assert bolo_env.read_env_value(str(env), "ASSEMBLYAI_API_KEY") == (
        'tricky' + backslash + '"' + backslash * 2 + "value"
    )
