"""Regression tests for the onboarding/status AppKit window script."""

import json
import os
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
