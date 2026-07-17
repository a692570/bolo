"""Regression tests for first-run hotkey onboarding."""

import builtins
import os
import stat

import pytest

import onboarding


def test_save_hotkey_creates_private_config(monkeypatch, tmp_path):
    bolo_dir = tmp_path / ".bolo"
    env_file = bolo_dir / "env"
    monkeypatch.setattr(onboarding, "ENV_FILE", str(env_file))

    previous_umask = os.umask(0o022)
    try:
        onboarding.save_hotkey("right_option")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(bolo_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_save_hotkey_rejects_unsupported_value(monkeypatch, tmp_path):
    monkeypatch.setattr(onboarding, "ENV_FILE", str(tmp_path / ".bolo" / "env"))

    with pytest.raises(ValueError, match="unsupported hotkey"):
        onboarding.save_hotkey("banana")


def test_fallback_prompt_reprompts_after_invalid_value(monkeypatch):
    answers = iter(["banana", "right_shift"])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))

    assert onboarding._fallback_prompt() == "right_shift"


def test_empty_saved_hotkey_does_not_suppress_onboarding(monkeypatch, tmp_path):
    env_file = tmp_path / ".bolo" / "env"
    env_file.parent.mkdir()
    env_file.write_text("BOLO_HOTKEY=\n")
    monkeypatch.setattr(onboarding, "ENV_FILE", str(env_file))
    monkeypatch.delenv("BOLO_HOTKEY", raising=False)
    monkeypatch.setattr(onboarding, "show_dialog", lambda: "right_control")

    onboarding.main()

    assert env_file.read_text() == "BOLO_HOTKEY=right_control\n"
