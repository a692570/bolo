"""Pure tests for safe Accessibility target matching and password-field refusal."""

import io
import json
import sys

import pytest

import accessibility_context
from accessibility_context import target_range_before_caret


def test_matches_exact_text_immediately_before_caret():
    assert target_range_before_caret("hello dictated text", 19, 0, "dictated text") == (6, 13)


def test_allows_one_trailing_space_after_dictation():
    assert target_range_before_caret("hello dictated text ", 20, 0, "dictated text") == (6, 14)


def test_refuses_moved_caret_or_changed_text():
    assert target_range_before_caret("dictated text then moved", 24, 0, "dictated text") is None
    assert target_range_before_caret("hello edited text", 17, 0, "dictated text") is None


def test_refuses_existing_selection():
    assert target_range_before_caret("dictated text", 0, 14, "dictated text") is None


class FakeElement:
    def __init__(self, attributes):
        self.attributes = attributes


@pytest.fixture
def refusal_log(monkeypatch):
    """Keep tests off /tmp/bolo.log and record each refusal."""
    calls = []
    monkeypatch.setattr(accessibility_context, "log_secure_refusal", lambda: calls.append(True))
    return calls


def use_fake_element(monkeypatch, attributes):
    """Serve element attributes from a dict via the module's copy_attribute seam."""
    element = FakeElement(attributes)

    def fake_copy_attribute(fake, attribute):
        return fake.attributes.get(str(attribute))

    monkeypatch.setattr(accessibility_context, "copy_attribute", fake_copy_attribute)
    return element


def run_context_main(monkeypatch, element, app=("Notes", "com.apple.Notes")):
    """Run the context path; main() returns None here and exits 0 via SystemExit."""
    monkeypatch.setattr(accessibility_context, "focused_element", lambda: element)
    monkeypatch.setattr(accessibility_context, "frontmost_app", lambda: app)
    monkeypatch.setattr(sys, "argv", ["accessibility_context.py"])
    return accessibility_context.main()


def run_selection_main(monkeypatch, element, target="dictated text", select_recorder=None):
    monkeypatch.setattr(accessibility_context, "focused_element", lambda: element)
    monkeypatch.setattr(sys, "argv", ["accessibility_context.py", "--select-before-caret"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(target))
    if select_recorder is not None:
        monkeypatch.setattr(
            accessibility_context,
            "select_text_immediately_before_caret",
            select_recorder,
        )
    return accessibility_context.main()


def test_secure_subrole_withholds_context(monkeypatch, capsys, refusal_log):
    element = use_fake_element(
        monkeypatch,
        {"AXSubrole": "AXSecureTextField", "AXValue": "hunter2", "AXSelectedText": "hunter2"},
    )
    run_context_main(monkeypatch, element)
    output = json.loads(capsys.readouterr().out)
    assert output["text_before_cursor"] == ""
    assert output["selected_text"] == ""
    assert "hunter2" not in json.dumps(output)
    assert output["app_name"] == "Notes"
    assert refusal_log == [True]


def test_secure_role_description_withholds_context(monkeypatch, capsys, refusal_log):
    element = use_fake_element(
        monkeypatch,
        {"AXRoleDescription": "secure text field", "AXValue": "secret contents"},
    )
    run_context_main(monkeypatch, element)
    output = json.loads(capsys.readouterr().out)
    assert output["text_before_cursor"] == ""
    assert output["selected_text"] == ""
    assert "secret contents" not in json.dumps(output)
    assert refusal_log == [True]


def test_password_placeholder_withholds_context(monkeypatch, capsys, refusal_log):
    element = use_fake_element(
        monkeypatch,
        {"AXPlaceholderValue": "Enter your password", "AXValue": "typed123"},
    )
    run_context_main(monkeypatch, element)
    output = json.loads(capsys.readouterr().out)
    assert output["text_before_cursor"] == ""
    assert output["selected_text"] == ""
    assert "typed123" not in json.dumps(output)
    assert refusal_log == [True]


def test_select_before_caret_refuses_secure_field(monkeypatch, capsys, refusal_log):
    element = use_fake_element(
        monkeypatch,
        {"AXSubrole": "AXSecureTextField", "AXValue": "hunter2"},
    )
    calls = []

    def recorder(fake_element, target):
        calls.append(target)
        return True

    code = run_selection_main(monkeypatch, element, target="hunter2", select_recorder=recorder)
    assert code == 3
    assert json.loads(capsys.readouterr().out) == {"selected": False}
    assert calls == []
    assert refusal_log == [True]


def test_select_before_caret_still_selects_ordinary_field(monkeypatch, capsys, refusal_log):
    element = use_fake_element(
        monkeypatch,
        {"AXValue": "hello dictated text "},
    )
    calls = []

    def recorder(fake_element, target):
        calls.append(target)
        return True

    code = run_selection_main(monkeypatch, element, select_recorder=recorder)
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"selected": True}
    assert calls == ["dictated text"]
    assert refusal_log == []


def test_ordinary_field_with_password_in_content_returns_context(monkeypatch, capsys, refusal_log):
    body = "notes about password managers and how to rotate them"
    element = use_fake_element(monkeypatch, {"AXValue": body})
    run_context_main(monkeypatch, element)
    output = json.loads(capsys.readouterr().out)
    assert "password managers" in output["text_before_cursor"]
    assert output["selected_text"] == ""
    assert refusal_log == []


def test_refusal_log_writes_one_sanitized_line(monkeypatch, tmp_path):
    log_path = tmp_path / "bolo.log"
    monkeypatch.setattr(accessibility_context, "LOG_FILE", str(log_path))
    accessibility_context.log_secure_refusal()
    contents = log_path.read_text(encoding="utf-8")
    assert contents.count("\n") == 1
    assert "[accessibility] secure field focused; context withheld" in contents
