"""Regression tests for the onboarding/status AppKit window script."""

import json
import os
import re
import stat

import app_window


def test_marker_payload_shape():
    payload = app_window.marker_payload(now_ms=1234)

    assert payload == {"version": 1, "completed_at_ms": 1234}

    fresh = app_window.marker_payload()
    assert isinstance(fresh["completed_at_ms"], int)
    assert fresh["completed_at_ms"] > 0


def test_write_marker_is_private_and_valid_json(tmp_path):
    marker = tmp_path / ".bolo" / "onboarding.json"
    app_window.write_marker(str(marker), app_window.marker_payload(now_ms=7))

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    with open(marker) as handle:
        assert json.load(handle) == {"version": 1, "completed_at_ms": 7}


def test_parse_update_accepts_only_objects():
    assert app_window.parse_update('{"try_it_complete": true}') == {
        "try_it_complete": True
    }
    assert app_window.parse_update("not json") is None
    assert app_window.parse_update("[1, 2]") is None
    assert app_window.parse_update('"text"') is None


def test_wrap_lines_keeps_short_text_on_one_line():
    assert app_window.wrap_lines("Hold Right Option and say a sentence.") == [
        "Hold Right Option and say a sentence."
    ]
    assert app_window.wrap_lines("") == [""]


def test_wrap_lines_breaks_long_details():
    long_detail = "Add this Python interpreter in System Settings, then run restart: " + "x" * 80
    lines = app_window.wrap_lines(long_detail)

    assert len(lines) > 1
    assert all(len(line) <= app_window.DETAIL_WRAP_AT for line in lines)
    assert "".join(lines).replace(" ", "") == long_detail.replace(" ", "")


def test_plan_layout_stacks_rows_and_button_in_order():
    payload = {
        "title": "Set up Bolo",
        "welcome": "Bolo listens while you hold a key.",
        "rows": [
            {"label": "Accessibility", "detail": "Granted for the paste helper.", "state": "ok"},
            {"label": "Try it", "detail": "Hold Right Option and say a sentence.", "state": "pending"},
        ],
        "button": "Done",
    }
    plan = app_window.plan_layout(payload)

    assert plan["welcome_y"] == app_window.TOP_PAD
    assert plan["rows"][0]["label_y"] < plan["rows"][0]["detail_y"]
    assert plan["rows"][0]["detail_y"] < plan["rows"][1]["label_y"]
    assert plan["button_y"] > plan["rows"][-1]["detail_y"]
    assert plan["height"] == plan["button_y"] + app_window.BUTTON_AREA_H


def test_plan_layout_without_welcome_or_rows_still_has_button():
    plan = app_window.plan_layout({"rows": []})

    assert plan["welcome_y"] is None
    assert plan["rows"] == []
    assert plan["height"] > plan["button_y"]


def test_key_entry_index_requires_in_range_row():
    assert app_window.key_entry_index({"rows": [{"label": "x"}], "key_entry": {"index": 0}}) == 0
    assert app_window.key_entry_index({"rows": []}) is None
    assert app_window.key_entry_index({"rows": [{"label": "x"}]}) is None
    assert (
        app_window.key_entry_index({"rows": [{"label": "x"}], "key_entry": {"index": 3}})
        is None
    )
    assert app_window.key_entry_index({"rows": [{"label": "x"}], "key_entry": {"index": "a"}}) is None


def test_classify_key_response_maps_http_statuses():
    assert app_window.classify_key_response(200) == "valid"
    assert app_window.classify_key_response(401) == "invalid"
    # Anything else is transient, not a rejection.
    assert app_window.classify_key_response(500) == "error"
    assert app_window.classify_key_response(403) == "error"


def _stub_fetch(status):
    def fetch(key, url=app_window.ASSEMBLYAI_LIST_URL, timeout=None):
        return status

    def failing(key, url=app_window.ASSEMBLYAI_LIST_URL, timeout=None):
        raise OSError("no route to host")

    return fetch, failing


def test_validate_and_save_key_accepts_200_and_writes_env(tmp_path):
    env = tmp_path / "env"
    fetch, _ = _stub_fetch(200)

    verdict, detail = app_window.validate_and_save_key("abc123", env_path=str(env), fetch=fetch)

    assert verdict == "valid"
    assert "saved" in detail
    assert env.read_text() == 'ASSEMBLYAI_API_KEY="abc123"\n'
    import stat
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_validate_and_save_key_rejects_401_without_writing(tmp_path):
    env = tmp_path / "env"
    fetch, _ = _stub_fetch(401)

    verdict, detail = app_window.validate_and_save_key("bad", env_path=str(env), fetch=fetch)

    assert verdict == "invalid"
    assert not env.exists()


def test_validate_and_save_key_handles_unexpected_status(tmp_path):
    fetch, _ = _stub_fetch(503)

    verdict, detail = app_window.validate_and_save_key("k", env_path=str(tmp_path / "env"), fetch=fetch)

    assert verdict == "error"
    assert not (tmp_path / "env").exists()


def test_validate_and_save_key_handles_network_failure(tmp_path):
    _, failing = _stub_fetch(401)

    verdict, detail = app_window.validate_and_save_key(
        "k", env_path=str(tmp_path / "env"), fetch=failing
    )

    assert verdict == "unreachable"
    assert "api.assemblyai.com" in detail
    assert not (tmp_path / "env").exists()


def test_validate_and_save_key_requires_nonempty_key(tmp_path):
    verdict, detail = app_window.validate_and_save_key(
        "   ", env_path=str(tmp_path / "env"), fetch=lambda *a, **k: 200
    )

    assert verdict == "empty"
    assert not (tmp_path / "env").exists()


def test_validate_and_save_key_replaces_existing_value(tmp_path):
    env = tmp_path / "env"
    env.write_text('ASSEMBLYAI_API_KEY="old"\nBOLO_HOTKEY="right_option"\n')
    fetch, _ = _stub_fetch(200)

    app_window.validate_and_save_key("new", env_path=str(env), fetch=fetch)

    assert env.read_text() == (
        'ASSEMBLYAI_API_KEY="new"\nBOLO_HOTKEY="right_option"\n'
    )


def test_plan_layout_reserves_space_for_key_entry_field():
    payload = {
        "title": "Set up Bolo",
        "welcome": "hello",
        "rows": [
            {"label": "Accessibility", "detail": "Granted.", "state": "ok"},
            {"label": "Speech to text", "detail": "Missing key.", "state": "warn"},
            {"label": "Try it", "detail": "Hold Right Option.", "state": "pending"},
        ],
        "button": "Done",
        "key_entry": {"index": 1, "placeholder": "Paste your key"},
    }
    plan = app_window.plan_layout(payload)

    assert plan["key_index"] == 1
    key_row = plan["rows"][1]
    assert key_row["field_y"] is not None
    assert key_row["field_y"] > key_row["label_y"]
    assert key_row["detail_y"] == key_row["field_y"] + app_window.KEY_FIELD_H + 4
    assert plan["rows"][0]["field_y"] is None
    assert plan["rows"][2]["field_y"] is None
    # Rows after the key row still stack below its detail line.
    assert plan["rows"][2]["label_y"] >= key_row["detail_y"]


def test_plan_layout_without_key_entry_matches_legacy_geometry():
    payload = {
        "rows": [
            {"label": "a", "detail": "one", "state": "ok"},
            {"label": "b", "detail": "two", "state": "ok"},
        ]
    }
    plan = app_window.plan_layout(payload)

    assert plan["key_index"] is None
    assert all(row["field_y"] is None for row in plan["rows"])
    # Same stacking as before the key-entry feature.
    assert plan["rows"][1]["label_y"] == (
        plan["rows"][0]["detail_y"] + app_window.DETAIL_LINE_H + app_window.ROW_GAP
    )


def test_appkit_action_selectors_have_single_colons():
    """Guard against the pyobjc underscore-to-colon trap.

    A method named `validate_key_` would be parsed by pyobjc as the
    two-part selector `validate:key:`, which raises BadPrototypeError when
    the class is defined and crashes every onboarding/status window. AppKit
    action names must carry exactly one trailing underscore.
    """
    source = open(os.path.join(os.path.dirname(app_window.__file__), "app_window.py")).read()
    bad = re.findall(r"def (\w+_\w+_)\(", source)
    assert bad == [], "pyobjc would read these as multi-colon selectors: {0}".format(bad)
    assert "validateKey_" in source
    assert '"validateKey:"' in source
