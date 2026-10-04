"""Regression tests for the onboarding/status AppKit window script."""

import json
import os
import re
import stat
import sys

import pytest

import app_window


def test_marker_payload_shape():
    payload = app_window.marker_payload(now_ms=1234)

    # Version 2: the polished wizard's verified setup. A version-1 marker
    # from the old checklist no longer skips onboarding.
    assert payload == {"version": 2, "completed_at_ms": 1234}

    fresh = app_window.marker_payload()
    assert isinstance(fresh["completed_at_ms"], int)
    assert fresh["completed_at_ms"] > 0
    assert app_window.MARKER_VERSION == 2


def test_write_marker_is_private_and_valid_json(tmp_path):
    marker = tmp_path / ".bolo" / "onboarding.json"
    app_window.write_marker(str(marker), app_window.marker_payload(now_ms=7))

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    with open(marker) as handle:
        assert json.load(handle) == {"version": 2, "completed_at_ms": 7}


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
    assert env.read_text() == (
        'ASSEMBLYAI_API_KEY="abc123"\n'
        'BOLO_STT_MODEL="assemblyai/universal-3-5-pro"\n'
    )
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
        'BOLO_STT_MODEL="assemblyai/universal-3-5-pro"\n'
    )


def test_validate_and_save_key_telnyx_provider_writes_telnyx_env(tmp_path):
    env = tmp_path / "env"
    fetch, _ = _stub_fetch(200)

    verdict, detail = app_window.validate_and_save_key(
        "KEY24601", env_path=str(env), fetch=fetch, provider="telnyx"
    )

    assert verdict == "valid"
    assert "saved" in detail
    assert env.read_text() == (
        'TELNYX_API_KEY="KEY24601"\n'
        'BOLO_STT_MODEL="deepgram/nova-3"\n'
    )


def test_validate_and_save_key_telnyx_rejection_writes_nothing(tmp_path):
    env = tmp_path / "env"
    fetch, _ = _stub_fetch(401)

    verdict, detail = app_window.validate_and_save_key(
        "bad", env_path=str(env), fetch=fetch, provider="telnyx"
    )

    assert verdict == "invalid"
    assert "Telnyx" in detail
    assert not env.exists()


def test_validate_and_save_key_rejects_unknown_provider(tmp_path):
    env = tmp_path / "env"

    verdict, _ = app_window.validate_and_save_key(
        "k", env_path=str(env), fetch=lambda *a, **k: 200, provider="other"
    )

    # An unknown provider id falls back to AssemblyAI semantics rather
    # than writing a variable the runtime would never read.
    assert verdict == "valid"
    assert env.read_text() == (
        'ASSEMBLYAI_API_KEY="k"\n'
        'BOLO_STT_MODEL="assemblyai/universal-3-5-pro"\n'
    )


def test_provider_fetchers_map_to_each_provider():
    assert (
        app_window.PROVIDER_KEY_FETCHERS["assemblyai"]
        is app_window.fetch_assemblyai_status
    )
    assert (
        app_window.PROVIDER_KEY_FETCHERS["telnyx"]
        is app_window.fetch_telnyx_status
    )


def test_fetch_telnyx_status_sends_bearer_header(monkeypatch):
    """The Telnyx probe authenticates with Bearer, unlike AssemblyAI's
    raw-key header; a captured request proves the header shape."""
    import urllib.request

    captured = {}

    class FakeResponse:
        def __init__(self, code):
            self._code = code

        def getcode(self):
            return self._code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.header_items())
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status = app_window.fetch_telnyx_status("KEY1")
    assert status == 200
    assert captured["url"] == app_window.TELNYX_KEY_LIST_URL
    assert captured["headers"].get("Authorization") == "Bearer KEY1"


def test_wizard_provider_choice_covers_both_providers_with_defaults():
    assert app_window.WIZARD_PROVIDERS == ("assemblyai", "telnyx")
    assert app_window.WIZARD_PROVIDER_TITLES["assemblyai"] == (
        "AssemblyAI (recommended)"
    )
    assert app_window.WIZARD_PROVIDER_TITLES["telnyx"] == "Telnyx (legacy)"
    assert app_window.WIZARD_PROVIDER_ENV_NAMES["assemblyai"] == "ASSEMBLYAI_API_KEY"
    assert app_window.WIZARD_PROVIDER_ENV_NAMES["telnyx"] == "TELNYX_API_KEY"
    assert app_window.WIZARD_PROVIDER_STT_MODELS == {
        "assemblyai": "assemblyai/universal-3-5-pro",
        "telnyx": "deepgram/nova-3",
    }


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


def test_restart_command_is_not_a_name_scan():
    """The pkill helper is gone entirely: the restart path signals one
    alive PID (see test_targeted_restart_signals_only_the_alive_parent),
    never a pattern match over process command lines."""
    import inspect

    assert not hasattr(app_window, "restart_command")
    source = inspect.getsource(app_window)
    assert "restart_runtime_request" not in source
    # No pattern-matching process scan survives anywhere in the module.
    for marker in ("pkill", "killall"):
        assert marker not in source, marker


def test_targeted_restart_signals_only_the_alive_parent():
    """Behavioral regression for the restart target.

    The window must never signal by name scan: the broad pkill matched
    the native launcher too. The targeted path signals exactly the
    parent PID, and only after a kill -0 probe confirms it is alive
    and signalable. Source mode never signals at all.
    """
    # A dead or nonsense PID is refused: nothing is signalled.
    assert app_window.targeted_restart_command(True, parent_pid=999999) is None
    assert app_window.targeted_restart_command(True, parent_pid=0) is None
    assert app_window.targeted_restart_command(True, parent_pid=-5) is None
    # Source mode has no supervised relaunch, so no signal at all.
    assert app_window.targeted_restart_command(False, parent_pid=1) is None
    # A live, signalable PID yields a kill aimed at exactly that PID,
    # with no name matching anywhere in the command.
    import os

    live = os.getpid()
    command = app_window.targeted_restart_command(True, parent_pid=live)
    assert command == ["kill", "-USR1", str(live)]
    assert not any("pkill" in part or "-f" in part for part in command)
    # The probe is honest about its limits: it proves the parent is
    # alive and signalable, not which binary owns the PID.
    assert app_window.is_parent_alive(live) is True
    assert app_window.is_parent_alive(999999) is False


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
        == "Open Privacy & Security Settings"
    )
    assert app_window.action_button_title({"action": {"kind": "restart"}}) == "Restart Bolo"
    assert (
        app_window.action_button_title({"action": {"title": ""}})
        == "Open Privacy & Security Settings"
    )


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


def test_wizard_screen_queue_skips_key_screen_when_key_present():
    payload = {"key_missing": False}
    assert (
        list(app_window.onboarding_screen_queue(payload)) == [
            app_window.SCREEN_WELCOME,
            app_window.SCREEN_MICROPHONE,
            app_window.SCREEN_ACCESSIBILITY,
            app_window.SCREEN_PRACTICE,
            app_window.SCREEN_READY,
        ]
    )


def test_wizard_screen_queue_includes_key_when_missing():
    payload = {"key_missing": True}
    queue = app_window.onboarding_screen_queue(payload)
    assert queue[0] == app_window.SCREEN_WELCOME
    assert queue[1] == app_window.SCREEN_CONNECT_SPEECH
    assert app_window.SCREEN_PRACTICE in queue
    assert app_window.SCREEN_READY == queue[-1]


def test_microphone_devices_count_is_not_permission_grant():
    zero = app_window.microphone_devices_row(0)
    one = app_window.microphone_devices_row(1)
    many = app_window.microphone_devices_row(3)
    assert zero["state"] == "warn"
    assert "Allow microphone access" in zero["detail"]
    assert one["state"] == "ok"
    assert "permission" not in one["detail"].lower()
    assert many["state"] == "ok"
    assert "microphones found" in many["detail"]


def test_practice_complete_requires_insert_trusted_field():
    assert app_window.practice_complete_from_update({"try_it_complete": True}) is False
    assert (
        app_window.practice_complete_from_update(
            {"try_it_complete": True, "insert_trusted": False}
        )
        is False
    )
    assert (
        app_window.practice_complete_from_update(
            {"try_it_complete": True, "insert_trusted": True}
        )
        is True
    )
    assert app_window.practice_complete_from_update({"try_it_complete": False}) is False
    assert app_window.practice_complete_from_update(None) is False
    assert app_window.practice_complete_from_update("text") is False


def test_blocked_continuation_until_practice_done():
    waiting = app_window.screen_primary_button(
        app_window.SCREEN_PRACTICE, practice_done=False
    )
    ready = app_window.screen_primary_button(
        app_window.SCREEN_PRACTICE, practice_done=True
    )
    assert waiting != ready
    assert "waiting" in waiting.lower() or "Hold" in waiting
    assert "Continue" in ready


def test_wizard_practice_primary_keeps_continue_label_while_disabled():
    # The wizard primary keeps the future action as its label while
    # gated; the status line carries the pending state, not the button.
    assert (
        app_window.wizard_primary_title(
            app_window.SCREEN_PRACTICE, {}, practice_done=False
        )
        == "Continue"
    )
    assert (
        app_window.wizard_primary_title(
            app_window.SCREEN_PRACTICE, {}, practice_done=True
        )
        == "Continue"
    )
    # Gating is expressed through enabled state, never the title.
    assert (
        app_window.wizard_primary_enabled(
            app_window.SCREEN_PRACTICE, {}, practice_done=False
        )
        is False
    )
    assert (
        app_window.wizard_primary_enabled(
            app_window.SCREEN_PRACTICE, {}, practice_done=True
        )
        is True
    )


def test_wizard_microphone_body_explains_allow_in_practice():
    paragraphs = app_window.wizard_body_paragraphs(
        app_window.SCREEN_MICROPHONE, {"hotkey": "left_option"}
    )
    text = " ".join(paragraphs)
    # Honest handoff, not a faked permission check: macOS prompts during
    # the Practice dictation (which follows the Accessibility step, and
    # may be step 4 or 5 depending on the key screen), and the copy
    # names Practice without a hard step number.
    assert "allow microphone access" in text.lower()
    assert "Click Allow" in text
    assert "in Practice" in text
    assert "next step" not in text


def test_wizard_status_lines_carry_no_developer_jargon():
    assert app_window.WIZARD_ACCESSIBILITY_PENDING_STATUS == (
        "Permission not confirmed yet."
    )
    assert app_window.WIZARD_KEY_STATUS_INITIAL == "Key not validated yet."
    body = " ".join(
        app_window.wizard_body_paragraphs(
            app_window.SCREEN_ACCESSIBILITY, {"trust": "warn"}
        )
    )
    assert "runtime" not in body
    assert "recheck" not in body


def test_runtime_trust_reply_is_the_only_source_of_truth():
    request = '{"type":"trust_check"}'
    parsed = json.loads(request)
    assert parsed["type"] == "trust_check"
    assert (
        app_window.parse_trust_reply('{"try_it_complete": true, "trusted": true}')
        is None
    )
    assert app_window.parse_trust_reply("not json") is None
    assert app_window.parse_trust_reply(
        '{"type":"trust_reply","trusted":true,"restart_needed":true}'
    ) == (True, True)
    assert app_window.parse_trust_reply(
        '{"type":"trust_reply","trusted":false,"restart_needed":false}'
    ) == (False, False)


def test_marker_writes_only_after_practice_and_user_close():
    assert (
        app_window.should_write_marker(
            payload={"write_marker": True},
            screen=app_window.SCREEN_READY,
            practice_done=True,
            user_closed=True,
        )
        is True
    )
    assert (
        app_window.should_write_marker(
            payload={"write_marker": True},
            screen=app_window.SCREEN_READY,
            practice_done=True,
            user_closed=False,
        )
        is False
    )
    assert (
        app_window.should_write_marker(
            payload={"write_marker": True},
            screen=app_window.SCREEN_READY,
            practice_done=False,
            user_closed=True,
        )
        is False
    )
    for earlier in (
        app_window.SCREEN_WELCOME,
        app_window.SCREEN_MICROPHONE,
        app_window.SCREEN_ACCESSIBILITY,
        app_window.SCREEN_PRACTICE,
    ):
        assert (
            app_window.should_write_marker(
                payload={},
                screen=earlier,
                practice_done=True,
                user_closed=True,
            )
            is False
        )
    assert (
        app_window.should_write_marker(
            payload={"write_marker": False},
            screen=app_window.SCREEN_READY,
            practice_done=True,
            user_closed=True,
        )
        is False
    )


def test_marker_v1_still_required_for_new_setup():
    marker = app_window.marker_payload(now_ms=1)
    assert marker["version"] == app_window.MARKER_VERSION == 2
    payload = {"write_marker": False}
    assert (
        app_window.should_write_marker(
            payload=payload,
            screen=app_window.SCREEN_READY,
            practice_done=True,
            user_closed=True,
        )
        is False
    )


def test_wizard_trust_primary_avoids_restart_loop_and_keeps_settings_link():
    # Untrusted: the primary keeps the short settings deep link, not a
    # second settings button and not a restart.
    assert app_window.wizard_trust_primary(False, False) == (
        "Open Settings",
        "openAccessibility:",
    )
    assert app_window.wizard_trust_primary(False, True) == (
        "Open Settings",
        "openAccessibility:",
    )
    # Trusted with no restart needed: straight to Continue, so a live
    # grant never traps the user in a restart loop.
    assert app_window.wizard_trust_primary(True, False) == (
        "Continue",
        "advance:",
    )
    # Trusted but the runtime only picks the grant up after a relaunch:
    # the targeted Restart Bolo action, never a broad pkill.
    assert app_window.wizard_trust_primary(True, True) == (
        "Restart Bolo",
        "restartBolo:",
    )


def test_wizard_primary_gates_never_fake_readiness():
    # The key screen stays disabled until a real validation succeeds.
    assert (
        app_window.wizard_primary_enabled(
            app_window.SCREEN_CONNECT_SPEECH, {}, practice_done=False
        )
        is False
    )
    assert (
        app_window.wizard_primary_enabled(
            app_window.SCREEN_CONNECT_SPEECH, {}, practice_done=True
        )
        is False
    )
    # Practice unlocks only on a genuine inserted dictation.
    assert (
        app_window.wizard_primary_enabled(
            app_window.SCREEN_PRACTICE, {}, practice_done=False
        )
        is False
    )
    assert (
        app_window.wizard_primary_enabled(
            app_window.SCREEN_PRACTICE, {}, practice_done=True
        )
        is True
    )
    # Accessibility while untrusted opens settings (enabled); granted or
    # other screens advance.
    assert (
        app_window.wizard_primary_enabled(
            app_window.SCREEN_ACCESSIBILITY, {"trust": "warn"}
        )
        is True
    )
    assert (
        app_window.wizard_primary_enabled(
            app_window.SCREEN_WELCOME, {}
        )
        is True
    )
    assert (
        app_window.wizard_primary_title(
            app_window.SCREEN_ACCESSIBILITY, {"trust": "ok"}
        )
        == "Continue"
    )
    assert (
        app_window.wizard_primary_title(
            app_window.SCREEN_ACCESSIBILITY, {"trust": "warn"}
        )
        == "Open Settings"
    )


def test_wizard_body_names_both_pane_titles_and_stale_grant_path():
    warn_facts = {"trust": "warn", "hotkey": "left_option"}
    paragraphs = app_window.wizard_body_paragraphs(
        app_window.SCREEN_ACCESSIBILITY, warn_facts
    )
    text = " ".join(paragraphs)
    assert "Accessibility" in text
    assert "Device Control and Data Access" in text
    assert "remove bolo" in text.lower()
    assert "add it again" in text
    assert "checks permission automatically" in text
    assert "real runtime" not in text
    # Granted: short, no instructions left.
    granted = app_window.wizard_body_paragraphs(
        app_window.SCREEN_ACCESSIBILITY, {"trust": "ok"}
    )
    assert len(granted) == 1
    assert "trusted" in granted[0].lower()

    # Practice copy names the precise configured hotkey, and so does Ready.
    practice = " ".join(
        app_window.wizard_body_paragraphs(
            app_window.SCREEN_PRACTICE, {"hotkey": "left_option"}
        )
    )
    assert "Left Option" in practice
    ready = " ".join(
        app_window.wizard_body_paragraphs(
            app_window.SCREEN_READY, {"hotkey": "right_shift"}
        )
    )
    assert "Right Shift" in ready


def test_hotkey_display_name_covers_supported_keys():
    assert app_window.hotkey_display_name("left_option") == "Left Option"
    assert app_window.hotkey_display_name("right_shift") == "Right Shift"
    assert app_window.hotkey_display_name("fn") == "Fn"
    assert app_window.hotkey_display_name("caps_lock") == "Caps Lock"
    assert app_window.hotkey_display_name("") == "your dictation key"
    assert app_window.hotkey_display_name(None) == "your dictation key"


def test_wizard_step_totals_frozen_across_mid_flow_updates():
    # The queue is snapshotted once per window build, so a validated key
    # or a trust reply cannot renumber later steps.
    import copy

    facts = {
        "key_missing": True,
        "trust": "warn",
        "microphones": 2,
        "hotkey": "left_option",
    }
    queue_before = app_window.onboarding_screen_queue({"wizard": dict(facts)})
    facts_after = dict(facts)
    facts_after["key_missing"] = False
    facts_after["trust"] = "ok"
    # An already-built window keeps its queue: the pure helper confirms
    # the order, and the window's refs["queue"] never recomputes.
    queue_after = app_window.onboarding_screen_queue({"wizard": dict(facts_after)})
    assert list(queue_before) == [
        app_window.SCREEN_WELCOME,
        app_window.SCREEN_CONNECT_SPEECH,
        app_window.SCREEN_MICROPHONE,
        app_window.SCREEN_ACCESSIBILITY,
        app_window.SCREEN_PRACTICE,
        app_window.SCREEN_READY,
    ]
    # A flow that starts with the key screen present keeps that total;
    # a fresh flow without the key screen is a different window, not a
    # renumbering of the same one.
    assert app_window.SCREEN_CONNECT_SPEECH not in queue_after
    assert len(queue_after) == len(queue_before) - 1


def test_wizard_trust_poll_lifecycle_across_flow():
    """Native callback regression: the trust poll must follow the flow.

    Welcome and microphone leave the poll inactive; entering the
    Accessibility step while untrusted activates it so run_event_loop
    requests checks and Open Settings can become Continue; a granted
    reply stops the poll; an untrusted reply resumes it (revocation
    recovery); leaving the step deactivates it. The practice field is
    first responder on entry so hotkey dictation lands in the field.
    """
    app_window.reset_state()
    payload = {
        "mode": "onboarding",
        "wizard": {
            "key_missing": False,
            "accessibility_state": "warn",
            "microphones": 2,
            "hotkey": "left_option",
        },
        "rows": [],
        "write_marker": False,
        "wizard_screen": True,
        "title": "Set up Bolo",
        "brand": "BOLO",
    }
    ui = app_window.build_ui(payload, preview=True)
    accessibility = ui["accessibility"]
    refs = ui["refs"]

    assert refs["screen"] == app_window.SCREEN_WELCOME
    assert accessibility.get("active", False) is False

    ui["advance"]()
    assert refs["screen"] == app_window.SCREEN_MICROPHONE
    assert accessibility.get("active", False) is False

    ui["advance"]()
    assert refs["screen"] == app_window.SCREEN_ACCESSIBILITY
    assert accessibility["active"] is True

    # Granted, no restart: the primary becomes Continue and the poll stops.
    ui["apply_trust"](True, False)
    button = refs["primary_button"]
    assert str(button.title()) == "Continue"
    assert str(button.action()) == "advance:"
    assert accessibility["active"] is False

    # Revoked mid-flow: the next untrusted reply recovers the warn state
    # and resumes polling so the step can flip back.
    ui["apply_trust"](False, False)
    assert str(button.title()) == "Open Settings"
    assert accessibility["active"] is True
    assert refs["facts"]["trust"] == "warn"

    # Moving on deactivates the poll, and the practice field takes focus.
    ui["advance"]()
    assert refs["screen"] == app_window.SCREEN_PRACTICE
    assert accessibility.get("active", False) is False
    assert ui["window"].firstResponder() is not None

    # A revoked grant cannot complete practice through the gate.
    assert (
        app_window.practice_complete_from_update(
            {"try_it_complete": True, "insert_trusted": False}
        )
        is False
    )
    assert (
        app_window.should_write_marker(
            {"write_marker": True}, app_window.SCREEN_READY, False, True
        )
        is False
    )
    app_window.reset_state()


def test_wizard_step_line_shows_number_and_name():
    line = app_window.wizard_step_line(app_window.SCREEN_ACCESSIBILITY, 3, 5)
    assert line == "Step 3 of 5 · Accessibility"


def test_validated_key_persists_only_on_assemblyai_acceptance():
    import os

    def fetcher(status):
        def fetch(key, url=None, timeout=None):
            return status
        return fetch

    verdict, _ = app_window.validate_and_save_key(
        "bad", env_path="/tmp/bolo-env-no-write-401", fetch=fetcher(401)
    )
    assert verdict == "invalid"
    assert not os.path.exists("/tmp/bolo-env-no-write-401")
    verdict, _ = app_window.validate_and_save_key(
        "bad", env_path="/tmp/bolo-env-no-write-503", fetch=fetcher(503)
    )
    assert verdict == "error"
    assert not os.path.exists("/tmp/bolo-env-no-write-503")
    verdict, _ = app_window.validate_and_save_key(
        "", env_path="/tmp/bolo-env-no-write-empty", fetch=fetcher(200)
    )
    assert verdict == "empty"
    assert not os.path.exists("/tmp/bolo-env-no-write-empty")



def _rust_shape(key_missing=True, trust="warn", microphones=2, hotkey="left_option",
                write_marker=True):
    """Exact field set the Rust AppWindowPayload serializes (verified
    against onboarding_window_payload in src/main.rs): mode onboarding,
    a wizard dict, no wizard_screen key, and write_marker from the
    marker state."""
    return {
        "mode": "onboarding",
        "title": "Set up Bolo",
        "welcome": "",
        "brand": "BOLO",
        "rows": [],
        "button": "Continue",
        "try_it_index": None,
        "try_it_hero": None,
        "key_entry": None,
        "write_marker": write_marker,
        "wizard": {
            "key_missing": key_missing,
            "accessibility_state": trust,
            "microphones": microphones,
            "hotkey": hotkey,
        },
    }


def test_build_ui_routes_real_rust_payload_to_wizard():
    """Live wiring regression: the actual Rust payload carries no
    wizard_screen flag, so routing on that flag alone showed the empty
    generic window in production. The mode plus wizard dict must route
    to the wizard path with the payload's real facts."""
    app_window.reset_state()
    payload = _rust_shape()
    ui = app_window.build_ui(payload, preview=True)
    refs = ui["refs"]
    assert len(refs["queue"]) == 6
    assert app_window.SCREEN_CONNECT_SPEECH in refs["queue"]
    assert refs["screen"] == app_window.SCREEN_WELCOME
    assert refs["facts"]["trust"] == "warn"
    assert refs["facts"]["microphones"] == 2
    assert refs["facts"]["hotkey"] == "left_option"
    app_window.reset_state()


def test_build_screen_payload_facts_roundtrip():
    """The preview payload must carry the same facts the runtime sends,
    so rendering all six screens from one window sequence keeps the key
    step instead of losing it to a 5-vs-6 queue."""
    facts = {
        "key_missing": True,
        "trust": "warn",
        "microphones": 3,
        "hotkey": "right_shift",
    }
    payload = app_window.build_screen_payload(
        facts, app_window.SCREEN_WELCOME, 1, 6
    )
    assert payload["wizard"] == {
        "key_missing": True,
        "accessibility_state": "warn",
        "microphones": 3,
        "hotkey": "right_shift",
    }
    restored = app_window.runtime_facts(app_window.raw_wizard_payload(payload))
    assert restored["key_missing"] is True
    assert restored["trust"] == "warn"
    assert restored["microphones"] == 3
    assert restored["hotkey"] == "right_shift"
    assert len(app_window.onboarding_screen_queue(payload)) == 6


def test_main_writes_marker_only_on_explicit_ready_finish(monkeypatch, tmp_path):
    """Live wiring regression for the marker gate.

    The old main() wrote whenever write_marker was requested and the
    screen was None (the real Rust payload path) or practice_done was
    set, so closing the window on the practice screen or even on ready
    without clicking finish could mark setup complete. The fixed gate
    requires: user close, runtime wants the marker, ready screen,
    genuine insert, AND the explicit finish button.
    """
    written = {}

    def fake_writer(path, payload):
        written["path"] = path
        written["payload"] = payload

    def run_with(screen, practice_done, finish_button, user_closed,
                 payload_extra=None):
        # Seed the post-build wizard state after main()'s reset_state()
        # runs, by replaying the finish through the state the event loop
        # would have left behind.
        payload = _rust_shape(write_marker=True)
        if payload_extra:
            payload.update(payload_extra)

        real_run = app_window.run_event_loop
        real_reset = app_window.reset_state

        def seeded_event_loop(ui):
            STATE = app_window.STATE
            STATE["screen"] = screen
            STATE["practice_done"] = practice_done
            STATE["finish_button"] = finish_button
            return user_closed

        monkeypatch.setattr(
            app_window, "read_payload", lambda: (payload, None)
        )
        monkeypatch.setattr(
            app_window,
            "build_ui",
            lambda p, preview=False: {"window": _FakeWindow()},
        )
        monkeypatch.setattr(app_window, "run_event_loop", seeded_event_loop)
        written.clear()
        rc = app_window.main(marker_file=str(tmp_path / "marker.json"),
                              marker_writer=fake_writer)
        return rc, dict(written)

    # Explicit finish on ready after a genuine insert: the only path that
    # writes.
    rc, wrote = run_with(
        app_window.SCREEN_READY, True, True, True,
        payload_extra={"write_marker": True},
    )
    assert rc == 0
    assert wrote, "explicit finish on ready must write the marker"
    assert wrote["payload"]["version"] == app_window.MARKER_VERSION

    # Close box on ready (finish_button False): no marker.
    rc, wrote = run_with(app_window.SCREEN_READY, True, False, True)
    assert rc == 0 and not wrote

    # Practice screen, insert done, close: no marker.
    rc, wrote = run_with(app_window.SCREEN_PRACTICE, True, True, True)
    assert rc == 0 and not wrote

    # Ready and finish but no genuine insert: no marker.
    rc, wrote = run_with(app_window.SCREEN_READY, False, True, True)
    assert rc == 0 and not wrote

    # Ready, insert, finish, but the runtime died first: no marker.
    rc, wrote = run_with(app_window.SCREEN_READY, True, True, False)
    assert rc == 0 and not wrote

    # Ready, insert, finish, but the runtime did not request the marker
    # (completed install reopened): no marker.
    rc, wrote = run_with(
        app_window.SCREEN_READY, True, True, True,
        payload_extra={"write_marker": False},
    )
    assert rc == 0 and not wrote

    app_window.reset_state()


class _FakeWindow:
    def orderOut_(self, sender):
        pass


def test_key_validation_unlocks_continue_and_advances_in_wizard():
    """Live key-bootstrap integration.

    Every speech request reads the key dynamically, so a freshly
    validated key works in the same runtime with no reload: on_valid
    unlocks Continue and clears key_missing, the wizard advances in
    place through microphone to Practice, and the frozen queue keeps
    every step count stable across the validation.
    """
    app_window.reset_state()
    payload = {
        "mode": "onboarding",
        "wizard": {
            "key_missing": True,
            "accessibility_state": "warn",
            "microphones": 2,
            "hotkey": "left_option",
        },
        "rows": [],
        "write_marker": True,
        "wizard_screen": True,
        "title": "Set up Bolo",
        "brand": "BOLO",
        "key_entry": {"index": 0, "placeholder": "Paste your key"},
    }
    ui = app_window.build_ui(payload, preview=True)
    refs = ui["refs"]
    queue = refs["queue"]
    assert len(queue) == 6
    assert refs["screen"] == app_window.SCREEN_WELCOME
    ui["advance"]()
    assert refs["screen"] == app_window.SCREEN_CONNECT_SPEECH
    button = refs["primary_button"]
    assert button.isEnabled() is False
    assert str(button.title()) == "Continue"

    key_refs = refs["key_refs"]
    key_refs["on_valid"]()
    # Continue stays Continue (no reload handoff), now enabled, and the
    # missing-key fact clears so nothing downstream re-adds the screen.
    assert button.isEnabled() is True
    assert str(button.title()) == "Continue"
    assert str(button.action()) == "advance:"
    assert refs["facts"]["key_missing"] is False

    ui["advance"]()
    assert refs["screen"] == app_window.SCREEN_MICROPHONE
    # The queue is frozen: validation did not drop or renumber steps.
    assert len(queue) == 6
    ui["advance"]()
    assert refs["screen"] == app_window.SCREEN_ACCESSIBILITY
    ui["advance"]()
    assert refs["screen"] == app_window.SCREEN_PRACTICE
    app_window.reset_state()


def test_provider_picker_switches_field_and_link_per_choice():
    """The key screen's provider choice reveals that provider's key field:
    the placeholder, the "Get an API key" link, and the saved-validation
    state all follow the selection, with AssemblyAI as the default."""
    app_window.reset_state()
    payload = {
        "mode": "onboarding",
        "wizard": {
            "key_missing": True,
            "accessibility_state": "ok",
            "microphones": 2,
            "hotkey": "left_option",
        },
        "rows": [],
        "write_marker": True,
        "wizard_screen": True,
        "title": "Set up Bolo",
        "brand": "BOLO",
    }
    ui = app_window.build_ui(payload, preview=True)
    refs = ui["refs"]
    ui["advance"]()
    assert refs["screen"] == app_window.SCREEN_CONNECT_SPEECH
    key_refs = refs["key_refs"]

    # The default is AssemblyAI: the recommended first choice.
    assert key_refs["provider"] == "assemblyai"
    assert key_refs["link_url"] == app_window.WIZARD_PROVIDER_LINKS["assemblyai"]

    picker = key_refs["picker"]
    controller = picker.target()
    picker.setSelectedSegment_(1)
    controller.providerChanged_(picker)

    assert key_refs["provider"] == "telnyx"
    assert key_refs["link_url"] == app_window.WIZARD_PROVIDER_LINKS["telnyx"]
    field = key_refs["field"]
    assert str(field.cell().placeholderString()) == (
        app_window.WIZARD_PROVIDER_PLACEHOLDERS["telnyx"]
    )
    # An unvalidated provider keeps the field editable and the status
    # line at its initial state.
    assert field.isEnabled() is True
    assert str(key_refs["detail_label"].stringValue()) == (
        app_window.WIZARD_KEY_STATUS_INITIAL
    )

    # Mark AssemblyAI validated, then switch back: the row shows the
    # saved state for that provider without re-asking for the key.
    key_refs["validated"]["assemblyai"] = True
    picker.setSelectedSegment_(0)
    controller.providerChanged_(picker)
    assert key_refs["provider"] == "assemblyai"
    assert field.isEnabled() is False
    assert "Key saved" in str(key_refs["detail_label"].stringValue())
    app_window.reset_state()


def test_configured_key_users_stay_on_continue():
    """An install whose key is already configured never sees the reload
    handoff: the key screen is skipped from the queue entirely."""
    payload = {
        "mode": "onboarding",
        "wizard": {
            "key_missing": False,
            "accessibility_state": "ok",
            "microphones": 2,
            "hotkey": "left_option",
        },
        "rows": [],
        "write_marker": True,
        "wizard_screen": True,
        "title": "Set up Bolo",
        "brand": "BOLO",
    }
    ui = app_window.build_ui(payload, preview=True)
    refs = ui["refs"]
    assert app_window.SCREEN_CONNECT_SPEECH not in refs["queue"]
    app_window.reset_state()


def test_finish_later_button_exits_every_unfinished_step():
    for screen in app_window.SCREEN_ORDER:
        app_window.reset_state()
        payload = _rust_shape(write_marker=True)
        payload["screen"] = screen
        ui = app_window.build_ui(payload, preview=True)
        button = ui["refs"]["finish_later_button"]
        if screen == app_window.SCREEN_READY:
            assert button is None
        else:
            assert str(button.title()) == "Finish later"
            assert str(button.keyEquivalent()) == "\x1b"
            app_window.STATE["finish_button"] = True
            button.performClick_(None)
            assert app_window.STATE["user_done"] is True
            assert app_window.STATE["finish_button"] is False
        ui["window"].close()
    app_window.reset_state()


def test_finish_later_does_not_write_completion_marker(monkeypatch, tmp_path):
    payload = _rust_shape(write_marker=True)
    payload["screen"] = app_window.SCREEN_ACCESSIBILITY
    build_ui = app_window.build_ui
    monkeypatch.setattr(app_window, "read_payload", lambda: (payload, None))
    monkeypatch.setattr(app_window, "build_ui", lambda p: build_ui(p, preview=True))

    def dismiss(ui):
        ui["refs"]["finish_later_button"].performClick_(None)
        assert ui["refs"]["facts"]["trust"] == "warn"
        return app_window.STATE["user_done"]

    monkeypatch.setattr(app_window, "run_event_loop", dismiss)
    marker = tmp_path / "onboarding.json"
    assert app_window.main(marker_file=str(marker)) == 0
    assert not marker.exists()
    app_window.reset_state()


# ---------------------------------------------------------------------------
# Canonical native-loop tests.
#
# run_event_loop() builds a repeating Foundation NSTimer
# (timerWithTimeInterval_target_selector_userInfo_repeats_) with the
# cached BootstrapPump as target, adds it to NSRunLoop.mainRunLoop() in
# NSRunLoopCommonModes, calls NSApplication.run(), and invalidates the
# timer in a finally. These tests mock only those Foundation/AppKit
# seams with fakes (never by editing production): a fake Foundation
# module exposing NSTimer/NSRunLoop/CommonModes, a fake app whose run()
# drives the scheduled pump tick while its running flag is set, and
# fake stdin/select for line delivery. No manual pump, no fallback
# loop, no test-only production interface exists to recreate.
# ---------------------------------------------------------------------------


class _LoopFixtures:
    """Builder for one run_event_loop pass with real AppKit wiring.

    The fake app.run() drives the scheduled timer target on each
    iteration while a running flag is set. The queue holds native
    "events"; a queued handler can set user_done the way a real
    user-driven event would, and run() ends after the queue drains or
    stop_ is requested.
    """

    def __init__(self, monkeypatch, ui):
        self.ui = ui
        self.app = _FakeNativeApp(ui)
        self.timer = None
        self.runloop = _FakeMainRunLoop(self)
        ui["app"] = self.app
        self._install_foundation(monkeypatch)

    def _install_foundation(self, monkeypatch):
        fake_foundation = _FakeFoundationModule(self)
        monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)
        monkeypatch.setitem(sys.modules, "Foundation.NSTimer", fake_foundation.NSTimer)
        monkeypatch.setitem(
            sys.modules, "Foundation.NSRunLoop", fake_foundation.NSRunLoop
        )


class _FakeFoundationModule:
    """Types mirroring the two Foundation names run_event_loop imports."""

    def __init__(self, fixtures):
        self.NSRunLoopCommonModes = "kCFRunLoopCommonModes"
        self.fixtures = fixtures

        outer = self

        class NSTimer:
            @staticmethod
            def timerWithTimeInterval_target_selector_userInfo_repeats_(
                interval, target, selector, info, repeats
            ):
                timer = _FakeNSTimer(
                    interval, target, selector, info, repeats, outer.fixtures
                )
                outer.fixtures.timer = timer
                return timer

        class NSRunLoop:
            @staticmethod
            def mainRunLoop():
                return outer.fixtures.runloop

        self.NSTimer = NSTimer
        self.NSRunLoop = NSRunLoop


class _FakeNSTimer:
    """The repeating integration timer; invalidate is recorded."""

    def __init__(self, interval, target, selector, info, repeats, fixtures):
        self.interval = interval
        self.target = target
        self.selector = selector
        self.info = info
        self.repeats = repeats
        self.fixtures = fixtures
        self.invalidated = 0
        self.added_modes = []

    def invalidate(self):
        self.invalidated += 1

    def fire(self):
        getattr(self.target, "tick_")(self)

    def describe(self):
        return (self.interval, self.selector, self.repeats)


class _FakeMainRunLoop:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.timers = []

    def addTimer_forMode_(self, timer, mode):
        timer.added_modes.append(mode)
        self.timers.append(timer)


class _FakeNativeApp:
    """NSApplication.run() model: drive ticks while running, end on stop."""

    def __init__(self, ui):
        self.ui = ui
        self.running = False
        self.stops = []
        self.posts = []
        self.activations = 0
        self.queue = []
        self.ran = 0

    def activateIgnoringOtherApps_(self, flag):
        assert self.running, "Activation must occur after native run begins"
        self.activations += 1

    def stop_(self, sender):
        self.stops.append(sender)
        self.running = False

    def postEvent_atStart_(self, event, at_start):
        self.posts.append(event)

    def run(self, runqueue=None):
        """Model NSApplication.run(): drive the timer until it stops.

        Each iteration services one queued native event (a real user
        event would dispatch into AppKit and set state through a
        controller) and then fires the integration timer tick, exactly
        as the scheduled repeating NSTimer fires inside run().
        """
        self.ran += 1
        self.running = True
        while self.running:
            if self.queue:
                event = self.queue.pop(0)
                handler = getattr(event, "handler", None)
                if handler is not None:
                    handler(self)
            if not self.running:
                break
            timer = self.ui.get("pump") and _current_timer(self)
            if timer is not None:
                timer.fire()
            else:
                # No scheduled timer: run() cannot be woken and must not
                # spin forever. Fail loudly rather than hang.
                raise AssertionError("run() started with no integration timer")


def _current_timer(app):
    """Find the timer run_event_loop scheduled for this app."""
    for candidate in app.ui["fixtures"].runloop.timers:
        return candidate
    return None


class _LineStdin:
    """Fake stdin: reported ready only when a line remains."""

    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        return ""


def _quiet_stdin(monkeypatch):
    """stdin never ready: no line, no EOF."""
    monkeypatch.setattr(app_window.select, "select", lambda *a, **k: ([], [], []))


def _loop_fixtures(monkeypatch, ui):
    fixtures = _LoopFixtures(monkeypatch, ui)
    ui["fixtures"] = fixtures
    return fixtures


def _native_ui(payload=None, preview=True, **kwargs):
    """Build a real wizard UI with AppKit; preview keeps it offscreen."""
    app_window.reset_state()
    if payload is None:
        payload = _rust_shape(write_marker=True)
        payload["screen"] = app_window.SCREEN_ACCESSIBILITY
    ui = app_window.build_ui(payload, preview=preview, **kwargs)
    return ui


def test_run_event_loop_schedules_repeating_common_mode_timer(monkeypatch):
    """The loop uses the real Foundation NSTimer factory and runloop add,
    with a repeating tick: target on the main runloop in common modes,
    then invalidates it after run() returns."""
    pytest.importorskip("AppKit")
    ui = _native_ui()
    fixtures = _loop_fixtures(monkeypatch, ui)
    fixtures.app.queue = []  # nothing arrives; tick sees idle stdin

    class IdleStdin:
        def readline(self):
            return "not-json\n"

    monkeypatch.setattr(app_window.sys, "stdin", IdleStdin())
    _quiet_stdin(monkeypatch)
    # Seed a user dismissal so run() returns instead of looping forever.
    monkeypatch.setattr(
        app_window, "request_runtime_trust_check", lambda: None
    )
    fixtures.app.queue.append(_Dismissal())

    closed = app_window.run_event_loop(ui)
    assert closed is True
    timer = fixtures.timer
    assert timer is not None, "the integration NSTimer must be scheduled"
    interval, selector, repeats = timer.describe()
    assert interval <= 0.1
    assert str(selector) == "tick:"
    assert repeats is True
    assert fixtures.runloop.timers == [timer]
    assert timer.added_modes == ["kCFRunLoopCommonModes"]
    assert timer.invalidated == 1
    assert "pump" not in ui
    app_window.reset_state()


class _Dismissal:
    """A queued native event the way a click on the primary button lands."""

    def __init__(self, finish_button=True):
        self.finish_button = finish_button

    def handler(self, app):
        STATE = app_window.STATE
        STATE["user_done"] = True
        STATE["finish_button"] = self.finish_button


def test_repeated_ticks_dispatch_stdin_updates(monkeypatch):
    """Every tick reads stdin and dispatches each queued update once.

    A dashboard_update arrives on tick one and a second on tick two;
    both apply through the same stdin path run_event_loop services, and
    the window keeps servicing until a user dismissal stops run()."""
    pytest.importorskip("AppKit")
    payload = {
        "mode": "dashboard",
        "title": "Bolo",
        "write_marker": False,
        "dashboard": {
            "version": "1.9.0",
            "hotkey": "right_option",
            "microphone": "default",
            "cleanup_mode": "auto",
            "history_limit": 10,
            "saved_dictations": 5,
            "saved_words": 10,
            "history": [],
        },
    }
    ui = _native_ui(payload=payload)
    refs = ui["refs"]

    updates = [
        json.dumps({
            "type": "dashboard_update",
            "dashboard": {
                "version": "1.9.0",
                "hotkey": "right_option",
                "microphone": "default",
                "cleanup_mode": "auto",
                "history_limit": 10,
                "saved_dictations": 6,
                "saved_words": 12,
                "history": [],
            },
        }),
        json.dumps({"type": "dashboard_activate"}),
        "",
    ]

    ready = iter([True, True, True])

    def fake_select(*args, **kwargs):
        try:
            return ([object()] if next(ready) else [], [], [])
        except StopIteration:
            return ([], [], [])

    monkeypatch.setattr(app_window.sys, "stdin", _LineStdin(updates))
    monkeypatch.setattr(app_window.select, "select", fake_select)
    monkeypatch.setattr(
        app_window, "request_runtime_trust_check", lambda: None
    )

    fixtures = _loop_fixtures(monkeypatch, ui)
    fixtures.app.queue = []  # EOF from the third line stops the loop

    closed = app_window.run_event_loop(ui)
    assert closed is False
    assert refs["dashboard"]["saved_dictations"] == 6
    assert refs["dashboard"]["saved_words"] == 12
    # A preview-built dashboard keeps its refs.preview flag; the loop
    # honored it and never activated the window (proven separately), and
    # the final EOF cannot fake a completion.
    assert refs.get("preview") is True
    assert app_window.STATE["finish_button"] is False
    assert fixtures.timer is not None and fixtures.timer.invalidated == 1
    app_window.reset_state()


def test_parent_eof_returns_false_and_clears_finish(monkeypatch):
    """EOF stops run(), returns False, and clears any finish flag.

    Even a queued explicit finish in flight cannot complete onboarding
    once the parent dies: the pump marks EOF, stop_ plus the wake post
    fire, and run_event_loop reports the session incomplete."""
    pytest.importorskip("AppKit")
    ui = _native_ui()
    fixtures = _loop_fixtures(monkeypatch, ui)

    monkeypatch.setattr(app_window.sys, "stdin", _LineStdin([]))
    monkeypatch.setattr(
        app_window.select, "select", lambda *a, **k: ([object()], [], [])
    )
    # A finish click landed in the queue but the parent died first.
    fixtures.app.queue = [_Dismissal(finish_button=True)]

    closed = app_window.run_event_loop(ui)
    assert closed is False
    assert app_window.STATE["user_done"] is True
    assert app_window.STATE["finish_button"] is False
    assert fixtures.app.stops, "EOF must stop run()"
    assert fixtures.app.posts, "EOF must post the wake event"
    assert fixtures.timer is not None and fixtures.timer.invalidated == 1
    app_window.reset_state()


def test_user_dismissal_stops_run_and_returns_true(monkeypatch):
    """A user dismissal inside run() ends the loop and returns True.

    The marker payload is untouched: only the explicit finish path can
    write it, and the loop never invents a completed session."""
    pytest.importorskip("AppKit")
    ui = _native_ui()
    fixtures = _loop_fixtures(monkeypatch, ui)
    marker_before = app_window.marker_payload(now_ms=1)

    monkeypatch.setattr(app_window.sys, "stdin", _LineStdin(["not-json\n"]))
    monkeypatch.setattr(
        app_window.select, "select", lambda *a, **k: ([object()], [], [])
    )
    fixtures.app.queue = [_Dismissal(finish_button=False)]

    closed = app_window.run_event_loop(ui)
    assert closed is True
    assert app_window.STATE["user_done"] is True
    assert fixtures.app.stops, "user dismissal must stop run()"
    assert fixtures.app.posts, "user dismissal must post the wake event"
    assert app_window.marker_payload(now_ms=1) == marker_before
    assert fixtures.timer is not None and fixtures.timer.invalidated == 1
    app_window.reset_state()


def test_activation_happens_once_and_only_inside_run(monkeypatch):
    """A preview-marked wizard UI never activates while run() spins.

    The tick guard checks ui["preview"] first, so the preview window
    built for screenshots stays quiet while the pump keeps ticking until
    stdin EOF stops run(). build_ui's preview branch already returns
    before any activation."""
    pytest.importorskip("AppKit")
    app_window.reset_state()
    payload = _rust_shape(write_marker=True)
    payload["screen"] = app_window.SCREEN_ACCESSIBILITY

    ui = app_window.build_ui(payload, preview=True)
    ui["preview"] = True  # the flag the tick guard reads
    ordered = {"key": 0, "regardless": 0}

    class _OrderTrackingWindow:
        """Wraps the real window so order-front calls are countable."""

        def __init__(self, real):
            self.real = real

        def makeKeyAndOrderFront_(self, sender):
            ordered["key"] += 1

        def orderFrontRegardless_(self, sender):
            ordered["regardless"] += 1

    tracking = _OrderTrackingWindow(ui["window"])
    ui["window"] = tracking

    monkeypatch.setattr(app_window.sys, "stdin", _LineStdin([""]))
    monkeypatch.setattr(
        app_window.select, "select", lambda *a, **k: ([object()], [], [])
    )
    monkeypatch.setattr(
        app_window, "request_runtime_trust_check", lambda: None
    )
    fixtures = _loop_fixtures(monkeypatch, ui)
    closed = app_window.run_event_loop(ui)
    assert closed is False
    assert fixtures.app.activations == 0
    assert ordered == {"key": 0, "regardless": 0}
    assert fixtures.timer is not None and fixtures.timer.invalidated == 1
    app_window.reset_state()


def test_live_window_activates_once_on_first_tick(monkeypatch):
    """Activation ordering for the live (non-preview) window path.

    A live UI activates exactly once on the first tick after run()
    begins, orders the window key and front regardless, and never
    activates again on any later tick. This proof is kept separate
    from the preview-suppression test above."""
    pytest.importorskip("AppKit")
    app_window.reset_state()
    payload = _rust_shape(write_marker=True)
    payload["screen"] = app_window.SCREEN_ACCESSIBILITY

    ui = app_window.build_ui(payload, preview=True)
    assert "preview" not in ui  # the preview branch returns before activating
    ordered = {"key": 0, "regardless": 0}

    class _OrderTrackingWindow:
        def __init__(self, real):
            self.real = real

        def makeKeyAndOrderFront_(self, sender):
            ordered["key"] += 1

        def orderFrontRegardless(self):
            ordered["regardless"] += 1

        def orderFrontRegardless_(self, sender):
            ordered["regardless"] += 1

    # A live window replaces the preview window: preview is absent, so
    # the first tick must activate it and only that tick.
    ui["window"] = _OrderTrackingWindow(ui["window"])

    # Three ticks: the first activates, the next two must not; the
    # third consumes the EOF line and stops run().
    lines = ["not-json\n", "not-json\n", ""]
    monkeypatch.setattr(app_window.sys, "stdin", _LineStdin(lines))
    ready = iter([True, True, True])

    def fake_select(*a, **k):
        try:
            return ([object()] if next(ready) else [], [], [])
        except StopIteration:
            return ([], [], [])

    monkeypatch.setattr(app_window.select, "select", fake_select)
    monkeypatch.setattr(
        app_window, "request_runtime_trust_check", lambda: None
    )
    fixtures = _loop_fixtures(monkeypatch, ui)
    closed = app_window.run_event_loop(ui)
    # Activation happened exactly once, on the first tick, and the
    # window was ordered key and front regardless exactly once each.
    assert fixtures.app.activations == 1
    assert ordered == {"key": 1, "regardless": 1}
    assert closed is False
    assert fixtures.timer is not None and fixtures.timer.invalidated == 1
    app_window.reset_state()


def test_timer_invalidated_when_run_raises(monkeypatch):
    """The finally invalidates the timer even if run() raises."""
    pytest.importorskip("AppKit")
    ui = _native_ui()
    fixtures = _loop_fixtures(monkeypatch, ui)

    def exploding_run(self=None):
        raise RuntimeError("run() exploded")

    monkeypatch.setattr(_FakeNativeApp, "run", exploding_run)
    monkeypatch.setattr(app_window.sys, "stdin", _LineStdin(["x\n"]))
    monkeypatch.setattr(
        app_window.select, "select", lambda *a, **k: ([], [], [])
    )
    try:
        app_window.run_event_loop(ui)
    except RuntimeError:
        pass
    else:
        raise AssertionError("run() raising must propagate")
    assert fixtures.timer is not None
    assert fixtures.timer.invalidated == 1
    assert "pump" not in ui
    app_window.reset_state()


def test_dashboard_preview_ui_never_activates_under_run(monkeypatch):
    """A dashboard UI whose refs carry preview=True never activates.

    The tick guard checks ui.preview first, then dashboard refs.preview;
    a dashboard preview window built for screenshots must stay quiet
    even while a mocked run() spins the pump."""
    pytest.importorskip("AppKit")
    payload = {
        "mode": "dashboard",
        "title": "Bolo",
        "write_marker": False,
        "dashboard": {
            "version": "1.9.0",
            "hotkey": "right_option",
            "microphone": "default",
            "cleanup_mode": "auto",
            "history_limit": 10,
            "saved_dictations": 1,
            "saved_words": 2,
            "history": [],
        },
    }
    ui = app_window.build_ui(payload, preview=True)
    assert ui.get("dashboard") is True
    assert ui["refs"]["preview"] is True

    monkeypatch.setattr(app_window.sys, "stdin", _LineStdin([]))
    monkeypatch.setattr(
        app_window.select, "select", lambda *a, **k: ([object()], [], [])
    )
    fixtures = _loop_fixtures(monkeypatch, ui)
    closed = app_window.run_event_loop(ui)
    assert closed is False
    assert fixtures.app.activations == 0
    assert not getattr(ui["window"], "ordered", 0)
    app_window.reset_state()


def test_dashboard_cli_starts_and_handles_parent_eof(tmp_path):
    """Exercise the same script entry point the installed runtime starts."""
    import subprocess

    pytest.importorskip("AppKit")
    payload = {"mode": "dashboard", "title": "Bolo", "write_marker": False,
               "dashboard": {"version": "test", "history": []}}
    env = dict(os.environ, HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [sys.executable, app_window.__file__],
        input=json.dumps(payload) + "\n", text=True, capture_output=True,
        timeout=20, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / ".bolo" / "onboarding.json").exists()
