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


def test_plan_layout_reserves_the_brand_row():
    payload = {
        "title": "Set up Bolo",
        "welcome": "hello",
        "rows": [
            {"label": "Accessibility", "detail": "Granted.", "state": "ok"},
            {"label": "Try it", "detail": "Hold Right Option.", "state": "pending"},
        ],
        "button": "Done",
    }
    without_brand = app_window.plan_layout(dict(payload))
    with_brand = app_window.plan_layout({**payload, "brand": "BOLO"})

    assert without_brand["brand_y"] is None
    assert with_brand["brand_y"] == app_window.TOP_PAD
    shift = app_window.BRAND_ROW_H + app_window.BRAND_GAP
    assert with_brand["welcome_y"] == without_brand["welcome_y"] + shift
    assert (
        with_brand["rows"][0]["label_y"]
        == without_brand["rows"][0]["label_y"] + shift
    )
    assert with_brand["height"] == without_brand["height"] + shift


def test_plan_layout_brand_without_welcome_still_stacks_rows():
    plan = app_window.plan_layout(
        {
            "brand": "BOLO",
            "rows": [{"label": "Version", "detail": "1.6.0", "state": "ok"}],
            "button": "Close",
        }
    )

    assert plan["welcome_y"] is None
    assert plan["brand_y"] == app_window.TOP_PAD
    assert plan["rows"][0]["label_y"] == (
        app_window.TOP_PAD + app_window.BRAND_ROW_H + app_window.BRAND_GAP
    )


def test_plan_layout_skips_blank_detail_lines():
    # Learning-window rows carry no detail text; they reserve zero detail
    # lines so each tappable row stays a single tight line.
    plan = app_window.plan_layout(
        {
            "rows": [
                {"label": "tim -> tom", "detail": "", "state": "ok"},
                {"label": "meting -> meeting", "detail": "", "state": "ok"},
            ]
        }
    )

    assert plan["rows"][0]["detail_lines"] == []
    assert plan["rows"][0]["detail_y"] == plan["rows"][0]["label_y"] + app_window.LABEL_LINE_H + 2
    assert plan["rows"][1]["label_y"] == (
        plan["rows"][0]["detail_y"] + app_window.ROW_GAP
    )


def test_learning_display_payload_pairs_error_and_copy():
    spec = {
        "hint_welcome": "Tap a correction to remove it.",
        "empty_welcome": "Nothing learned yet.",
    }

    display = app_window.learning_display_payload(
        [{"misheard": "tim", "corrected": "tom"}, {"misheard": "meting", "corrected": "meeting"}],
        None,
        spec,
    )

    assert display["welcome"] == "Tap a correction to remove it."
    assert display["rows"] == [
        {"label": "tim -> tom", "detail": "", "state": "ok"},
        {"label": "meting -> meeting", "detail": "", "state": "ok"},
    ]

    # No pairs left: the empty-state copy takes over and an error line, when
    # present, renders as a warn row after the (empty) pair rows.
    empty = app_window.learning_display_payload([], None, spec)
    assert empty["welcome"] == "Nothing learned yet."
    assert empty["rows"] == []

    unreadable = app_window.learning_display_payload([], "Could not read the learned-words file.", spec)
    assert unreadable["welcome"] == "Nothing learned yet."
    assert unreadable["rows"] == [
        {"label": "Could not read the learned-words file.", "detail": "", "state": "warn"}
    ]


def test_delete_learned_pair_removes_only_that_pair(tmp_path):
    path = tmp_path / "learned_vocabulary.json"
    path.write_text(
        json.dumps(
            {
                "corrections": {
                    "tim": {"corrected": "tom", "count": 2, "last_used": 10},
                    "meting": {"corrected": "meeting", "count": 1, "last_used": 20},
                }
            }
        )
    )
    path.chmod(0o600)

    removed, error = app_window.delete_learned_pair(str(path), "tim")

    assert removed is True
    assert error is None
    with open(path) as handle:
        data = json.load(handle)
    assert data["corrections"] == {
        "meting": {"corrected": "meeting", "count": 1, "last_used": 20}
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "learned_vocabulary.json.tmp").exists()


def test_delete_learned_pair_reports_missing_key_and_file(tmp_path):
    path = tmp_path / "learned_vocabulary.json"
    path.write_text(
        json.dumps({"corrections": {"tim": {"corrected": "tom", "count": 1, "last_used": 1}}})
    )

    removed, error = app_window.delete_learned_pair(str(path), "zzabsent")
    assert removed is False
    assert error

    removed, error = app_window.delete_learned_pair(str(tmp_path / "nope.json"), "tim")
    assert removed is False
    assert error

    # An unparsable file fails plainly rather than wiping it.
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("this is not json")
    removed, error = app_window.delete_learned_pair(str(corrupt), "tim")
    assert removed is False
    assert "Could not read" in error
    assert corrupt.read_text() == "this is not json"


def test_write_learned_file_matches_runtime_shape(tmp_path):
    path = str(tmp_path / "nested" / "learned_vocabulary.json")
    payload = {"corrections": {"tim": {"corrected": "tom", "count": 1, "last_used": 5}}}

    app_window.write_learned_file(path, payload)

    with open(path) as handle:
        assert handle.read() == (
            '{\n  "corrections": {\n    "tim": {\n      '
            '"corrected": "tom",\n      "count": 1,\n      "last_used": 5\n    }\n  }\n}\n'
        )
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_try_it_hero_is_styling_only_not_geometry():
    # The hero line changes fonts and colors in build_ui, never the plan:
    # payloads with and without it must produce identical geometry.
    base = {
        "rows": [
            {"label": "Try it", "detail": "Hold Right Option.", "state": "pending"}
        ],
        "try_it_index": 0,
        "button": "Done",
    }
    plan_plain = app_window.plan_layout(dict(base))
    plan_hero = app_window.plan_layout(
        {
            **base,
            "try_it_hero": "Hold Right Option. This window will stay open, and the dot turns green when you're done.",
        }
    )

    assert plan_plain["rows"] == plan_hero["rows"]
    assert plan_plain["height"] == plan_hero["height"]


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
    assert "openAccessibility_" in source
    assert '"openAccessibility:"' in source
    assert "restartBolo_" in source
    assert '"restartBolo:"' in source


def test_accessibility_settings_url_targets_privacy_accessibility():
    assert app_window.ACCESSIBILITY_SETTINGS_URL == (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    )
    assert app_window.open_settings_command() == ["open", app_window.ACCESSIBILITY_SETTINGS_URL]


def test_restart_command_only_exists_in_bundle_mode():
    assert app_window.restart_command(True) == ["pkill", "-USR1", "-f", "Bolo.app/Contents/MacOS"]
    # Source mode has no supervised relaunch: the window keeps the
    # ./restart.sh instruction instead of a restart button.
    assert app_window.restart_command(False) is None


def test_row_action_kind_accepts_only_known_kinds():
    warn = {
        "label": "Accessibility",
        "detail": "Enable Bolo in the list.",
        "state": "warn",
        "action": {"kind": "open_settings", "title": "Open Accessibility Settings"},
    }
    assert app_window.row_action_kind(warn) == "open_settings"
    assert app_window.row_action_kind({"action": {"kind": "restart"}}) == "restart"
    assert app_window.row_action_kind({}) is None
    assert app_window.row_action_kind({"action": None}) is None
    assert app_window.row_action_kind({"action": {"kind": "self_destruct"}}) is None
    assert app_window.row_action_kind({"action": "open_settings"}) is None
    assert app_window.row_action_kind("not a row") is None


def test_action_button_title_prefers_payload_then_kind_default():
    titled = {"action": {"kind": "restart", "title": "Relaunch now"}}
    assert app_window.action_button_title(titled) == "Relaunch now"
    assert (
        app_window.action_button_title({"action": {"kind": "open_settings"}})
        == "Open Accessibility Settings"
    )
    assert app_window.action_button_title({"action": {"kind": "restart"}}) == "Restart Bolo"
    assert app_window.action_button_title({"action": {"title": ""}}) == "Open Accessibility Settings"


def test_plan_layout_reserves_space_for_the_row_action_button():
    payload = {
        "brand": "BOLO",
        "welcome": "hello",
        "rows": [
            {
                "label": "Accessibility",
                "detail": "Click the button, then enable Bolo.",
                "state": "warn",
                "action": {"kind": "open_settings"},
            },
            {"label": "Try it", "detail": "Hold Right Option.", "state": "pending"},
        ],
    }
    plan = app_window.plan_layout(payload)

    row, following = plan["rows"]
    assert row["action_y"] is not None
    assert row["action_y"] > row["detail_y"]
    assert following["action_y"] is None
    assert following["label_y"] >= row["action_y"] + app_window.ACTION_BUTTON_H

    plain = app_window.plan_layout(
        {**payload, "rows": [dict(payload["rows"][0], action=None), payload["rows"][1]]}
    )
    assert plan["height"] == (
        plain["height"] + app_window.ACTION_BUTTON_H + app_window.ACTION_BUTTON_GAP
    )


def test_accessibility_flip_swaps_button_in_bundle_mode():
    warn = {
        "label": "Accessibility",
        "detail": "Bolo needs Accessibility to type for you.",
        "state": "warn",
        "action": {"kind": "open_settings", "title": "Open Accessibility Settings"},
    }
    # Still untrusted (or the check failed): no change at all.
    assert app_window.accessibility_flip(warn, False, True) is None
    assert app_window.accessibility_flip(warn, None, True) is None

    flipped = app_window.accessibility_flip(warn, True, True)
    assert flipped == {
        "label": "Accessibility",
        "detail": "Granted.",
        "state": "ok",
        "action": {"kind": "restart", "title": "Restart Bolo"},
    }
    # An already-green row never flips again.
    assert app_window.accessibility_flip(flipped, True, True) is None


def test_accessibility_flip_source_mode_keeps_restart_instruction_and_drops_button():
    warn = {
        "label": "Accessibility",
        "detail": "Bolo needs Accessibility to type for you.",
        "state": "warn",
        "action": {"kind": "open_settings", "title": "Open Accessibility Settings"},
    }
    flipped = app_window.accessibility_flip(warn, True, False)

    assert flipped["state"] == "ok"
    assert "restart.sh" in flipped["detail"]
    assert app_window.row_action_kind(flipped) is None
    # Only the Accessibility row rides this path.
    assert (
        app_window.accessibility_flip(
            {"label": "Microphone", "detail": "None found.", "state": "warn"}, True, True
        )
        is None
    )
