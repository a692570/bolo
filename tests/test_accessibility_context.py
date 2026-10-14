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


def stub_value_writes(monkeypatch, accepted):
    """Replace the AXValue-setability seam used by the writable check.
    Records how many elements were probed so tests can assert the probe
    ran exactly once (and only after the role check missed)."""
    calls = []

    def fake(element):
        calls.append(element)
        return accepted

    monkeypatch.setattr(
        accessibility_context, "element_accepts_value_writes", fake
    )
    return calls


def test_no_focused_element_is_not_writable():
    assert accessibility_context.is_writable_element(None) is False


@pytest.mark.parametrize(
    "role",
    [
        "AXTextArea",
        "AXTextField",
        "AXComboBox",
        "AXTextGroup",
        "AXSearchField",
    ],
)
def test_editable_roles_are_writable_targets(monkeypatch, role):
    element = use_fake_element(monkeypatch, {"AXRole": role})
    assert accessibility_context.is_writable_element(element) is True


def test_value_settable_element_is_writable_even_off_the_role_list(monkeypatch):
    # The WebArea role only becomes a paste target when AXValue accepts set
    # operations: the setability probe is what proves it, and it runs
    # exactly once after the role check missed.
    element = use_fake_element(monkeypatch, {"AXRole": "AXWebArea"})
    calls = stub_value_writes(monkeypatch, True)
    assert accessibility_context.is_writable_element(element) is True
    assert calls == [element]


def test_plain_control_is_not_a_writable_target(monkeypatch):
    element = use_fake_element(monkeypatch, {"AXRole": "AXButton", "AXValue": "OK"})
    calls = stub_value_writes(monkeypatch, False)
    assert accessibility_context.is_writable_element(element) is False
    assert calls == [element]


def test_secure_field_is_not_writable_and_its_value_is_never_read(
    monkeypatch, refusal_log
):
    requested = []

    def recording_copy(element, attribute):
        requested.append(str(attribute))
        return element.attributes.get(str(attribute))

    monkeypatch.setattr(accessibility_context, "copy_attribute", recording_copy)
    element = FakeElement({"AXSubrole": "AXSecureTextField", "AXValue": "hunter2"})
    assert accessibility_context.is_writable_element(element) is False
    assert "AXValue" not in requested


def test_secure_field_reports_writable_false_without_leaking_context(
    monkeypatch, capsys, refusal_log
):
    # main() emits writable alongside the context; a secure field reports
    # non-writable while its value stays withheld from every output key.
    element = use_fake_element(
        monkeypatch,
        {"AXSubrole": "AXSecureTextField", "AXValue": "hunter2"},
    )
    run_context_main(monkeypatch, element)
    output = json.loads(capsys.readouterr().out)
    assert output["writable"] is False
    assert "hunter2" not in json.dumps(output)
    assert refusal_log == [True]


def test_ordinary_field_reports_writable_true(monkeypatch, capsys, refusal_log):
    element = use_fake_element(monkeypatch, {"AXRole": "AXTextArea", "AXValue": "hi"})
    run_context_main(monkeypatch, element)
    output = json.loads(capsys.readouterr().out)
    assert output["writable"] is True
    assert refusal_log == []
