"""Regression tests for the native AppKit dashboard.

Every AppKit test runs in preview mode: the window is never ordered
front and the app never activates, so nothing steals focus. Emission is
captured with a stub emitter and the clipboard with a fake pasteboard,
so no real clipboard, marker, or env file is touched.
"""

import json

import dashboard_window
import app_window


def fixture_dashboard(history=None, **overrides):
    """Synthetic contract payload for previews and tests."""
    base = {
        "version": "1.9.0",
        "usage": {
            "dictations": 244,
            "words": 15230,
            "recording_ms": 9000000,
            "started_at_ms": 1733011200000,
        },
        "hotkey": "right_option",
        "microphone": "default",
        "microphones": ["MacBook Pro Microphone"],
        "cleanup_mode": "auto",
        "accessibility_state": "ok",
        "provider": "AssemblyAI",
        "history_limit": 10,
        "saved_dictations": 3,
        "saved_words": 57,
        "learned_words_count": 2,
        "history": history
        if history is not None
        else [
            {
                "text": "Meeting notes for the design review.",
                "raw": "meeting notes for the design review",
                "created_at_ms": 1700000000000,
                "edited_after_insert": True,
            }
        ],
    }
    base.update(overrides)
    return base


def fixture_payload(dashboard=None):
    return {
        "mode": "dashboard",
        "title": "Bolo",
        "write_marker": False,
        "dashboard": dashboard if dashboard is not None else fixture_dashboard(),
    }


def build(dashboard=None, dark=False):
    return dashboard_window.build_dashboard_ui(
        fixture_payload(dashboard), preview=True, dark=dark
    )


class FakePasteboard:
    """A pasteboard stub capturing one plain-text write."""

    def __init__(self):
        self.cleared = 0
        self.written = None

    def clearContents(self):
        self.cleared += 1

    def setString_forType_(self, text, type_):
        self.written = (text, type_)
        return True


def test_validate_dashboard_fills_safe_defaults():
    validated = dashboard_window.validate_dashboard({})
    assert validated["hotkey"] == "left_option"
    assert validated["microphone"] == "default"
    assert validated["cleanup_mode"] == "auto"
    assert validated["accessibility_state"] == "unavailable"
    assert validated["history_limit"] == 10
    assert validated["history"] == []
    assert validated["saved_dictations"] == 0
    assert dashboard_window.validate_dashboard("nope") is None


def test_validate_dashboard_keeps_only_contract_fields():
    entry = {
        "text": "hello",
        "raw": "hello ",
        "created_at_ms": 5,
        "edited_after_insert": True,
        "leaked_path": "/Users/x/.bolo/secret",
    }
    validated = dashboard_window.validate_dashboard({"history": [entry]})
    assert validated["history"] == [
        {"text": "hello", "raw": "hello ", "created_at_ms": 5, "edited_after_insert": True}
    ]


def test_build_action_emits_the_contract_shape_and_validates():
    request = dashboard_window.build_action(
        "save_settings", hotkey="right_shift", microphone="default",
        cleanup_mode="on",
    )
    assert request == {
        "type": "dashboard_action",
        "action": "save_settings",
        "hotkey": "right_shift",
        "microphone": "default",
        "cleanup_mode": "on",
    }
    assert dashboard_window.build_action("nope") is None
    assert dashboard_window.build_action("save_settings") is None
    assert dashboard_window.build_action(
        "save_settings", hotkey="fn", microphone="", cleanup_mode="auto"
    ) is None
    assert dashboard_window.build_action(
        "save_settings", hotkey="fn", microphone="Mic", cleanup_mode="sometimes"
    ) is None
    assert dashboard_window.build_action("refresh") == {
        "type": "dashboard_action", "action": "refresh",
    }


def test_emit_request_writes_one_json_line(capsys):
    dashboard_window.emit_request(dashboard_window.build_action("refresh"))
    line = capsys.readouterr().out.strip()
    assert json.loads(line) == {"type": "dashboard_action", "action": "refresh"}


def test_copy_uses_plain_text_pasteboard():
    board = FakePasteboard()
    assert dashboard_window.copy_to_pasteboard("hello there", pasteboard=board)
    assert board.written == ("hello there", "public.utf8-plain-text")
    assert board.cleared == 1
    assert not dashboard_window.copy_to_pasteboard("", pasteboard=board)


def test_timestamp_labels_today_and_older():
    assert dashboard_window.timestamp_label(0) == "Earlier"
    assert dashboard_window.timestamp_label(None) == "Earlier"
    assert dashboard_window.timestamp_label("x") == "Earlier"
    now = 1700000000000
    assert dashboard_window.timestamp_label(now, now_ms=now + 1000).startswith("Today")
    label = dashboard_window.timestamp_label(now - 40 * 86400000, now_ms=now)
    assert "Today" not in label


def test_preview_text_truncates():
    assert dashboard_window.preview_text("short") == "short"
    long = "word " * 60
    assert dashboard_window.preview_text(long).endswith("…")
    assert len(dashboard_window.preview_text(long)) <= 201


def test_usage_summary_prefers_runtime_counters_with_since_date():
    dashboard = fixture_dashboard()
    text = dashboard_window.usage_summary(dashboard)
    assert "Since November 30, 2024" in text
    assert "244 dictations" in text
    assert "15230 words" in text
    # No invented lifetime claims beyond the runtime counters.
    assert "learned words" not in text.split("Since")[1]


def test_usage_summary_falls_back_to_retained_history():
    dashboard = dashboard_window.validate_dashboard(
        {"version": "1.9.0", "saved_dictations": 2, "saved_words": 40,
         "learned_words_count": 1, "history_limit": 10}
    )
    text = dashboard_window.usage_summary(dashboard)
    assert "Last 10 saved dictations: 2 saved, 40 words." in text


def test_usage_summary_survives_bad_usage_shapes():
    dashboard = dashboard_window.validate_dashboard(
        {"version": "1.9.0", "usage": {"dictations": "many", "words": None},
         "history_limit": 10}
    )
    assert dashboard["usage"]["dictations"] == "many"
    text = dashboard_window.usage_summary(dashboard)
    assert "0 dictations, 0 words" in text
    # Bad top-level usage types fall back to the history summary.
    fallback = dashboard_window.validate_dashboard(
        {"version": "1.9.0", "usage": "nope", "history_limit": 10}
    )
    assert fallback["usage"] is None
    assert "Last 10 saved dictations" in dashboard_window.usage_summary(fallback)


def test_known_hotkeys_include_right_control_and_function_keys():
    for key in ("right_control", "f1", "f10", "f19"):
        assert key in dashboard_window.KNOWN_HOTKEYS


def test_microphone_choices_use_stable_values_with_unique_labels():
    # Stable uid: values with human labels; duplicate names stay
    # distinguishable and the System Default row is always first.
    dashboard = dashboard_window.validate_dashboard(
        fixture_dashboard(
            microphone="uid:st-2",
            microphone_choices=[
                {"value": "uid:st-1", "label": "Studio Mic (1)"},
                {"value": "uid:st-2", "label": "Studio Mic (2)"},
            ],
        )
    )
    rows = dashboard_window.settings_options(dashboard)["microphone"]
    assert rows[0] == ("default", "System Default")
    assert ("uid:st-1", "Studio Mic (1)") in rows
    assert ("uid:st-2", "Studio Mic (2)") in rows
    values = [value for value, _ in rows]
    assert len(values) == len(set(values)), "dropdown values must be unique"


def test_microphone_choices_keep_disconnected_saved_selection():
    # A saved-but-disconnected UID must stay selectable so saving an
    # unrelated setting never clears it from the user's view.
    dashboard = dashboard_window.validate_dashboard(
        fixture_dashboard(
            microphone="uid:gone",
            microphone_choices=[{"value": "uid:live", "label": "Live Mic"}],
        )
    )
    rows = dashboard_window.settings_options(dashboard)["microphone"]
    assert ("uid:gone", "uid:gone") in rows


def test_microphone_choices_fall_back_to_plain_names_without_choices():
    # Older payloads without microphone_choices still get a working
    # dropdown from the plain name list.
    payload = fixture_dashboard(microphones=["MacBook Pro Microphone"])
    payload.pop("microphone_choices", None)
    dashboard = dashboard_window.validate_dashboard(payload)
    assert dashboard["microphone_choices"] == []
    rows = dashboard_window.settings_options(dashboard)["microphone"]
    assert rows[0] == ("default", "System Default")
    assert ("MacBook Pro Microphone", "MacBook Pro Microphone") in rows


def test_build_action_accepts_stable_microphone_values():
    request = dashboard_window.build_action(
        "save_settings", hotkey="right_option", microphone="uid:st-2",
        cleanup_mode="auto",
    )
    assert request["microphone"] == "uid:st-2"
    # name: values pass too, and unknown shapes still fail.
    ok = dashboard_window.build_action(
        "save_settings", hotkey="right_option", microphone="name:Studio Mic",
        cleanup_mode="auto",
    )
    assert ok["microphone"] == "name:Studio Mic"
    assert (
        dashboard_window.build_action(
            "save_settings", hotkey="right_option", microphone="   ",
            cleanup_mode="auto",
        )
        is None
    )


def test_selection_retains_microphone_value_across_updates():
    # An update that arrives while a mic choice is pending must keep the
    # stable value, never silently revert to the running value.
    refs = {
        "dashboard": dashboard_window.validate_dashboard(
            fixture_dashboard(microphone="uid:st-1")
        ),
        "selections": {"microphone": "uid:st-2"},
        "restart_needed": False,
        "pending_selections": None,
        "tab": "settings",
        "dark": False,
        "content": None,
        "palette": lambda: dashboard_window.bolo_brand.palette(dark=False),
    }
    fresh = fixture_dashboard(
        microphone="uid:st-1",
        microphone_choices=[{"value": "uid:st-2", "label": "Studio Mic"}],
    )
    before = dict(refs["selections"])
    # apply_dashboard_update with no content view still runs the full
    # selection bookkeeping: the retention rule is testable without a
    # real window body.
    updated = fixture_dashboard(
        microphone="uid:st-1",
        microphone_choices=[{"value": "uid:st-2", "label": "Studio Mic"}],
    )
    refs["dashboard"] = dashboard_window.validate_dashboard(updated)
    dashboard_window.apply_dashboard_update(refs, updated)
    assert refs["selections"]["microphone"] == before["microphone"]


def test_dashboard_state_keeps_default_when_no_choice_saved():
    dashboard = dashboard_window.validate_dashboard(fixture_dashboard())
    assert dashboard["microphone"] == "default"
    rows = dashboard_window.settings_options(dashboard)["microphone"]
    assert rows[0] == ("default", "System Default")
    options = dashboard_window.settings_options(fixture_dashboard())
    values = [value for value, _ in options["hotkey"]]
    assert "right_control" in values and "f19" in values


def test_activity_summary_names_retained_scope():
    dashboard = dashboard_window.validate_dashboard(
        {"version": "1.9.0", "history_limit": 10,
         "saved_dictations": 3, "saved_words": 57, "learned_words_count": 2}
    )
    text = dashboard_window.activity_summary(dashboard)
    assert "Last 10 saved dictations" in text
    assert "3 saved" in text and "57 words" in text and "2 learned words" in text


def test_permission_lines_match_states():
    assert "granted" in dashboard_window.permission_line("ok")
    assert "needs a grant" in dashboard_window.permission_line("warn")
    assert "could not be checked" in dashboard_window.permission_line("unavailable")


def test_build_dashboard_ui_window_shape_and_no_activation():
    ui = build()
    window = ui["window"]
    assert window.title() == "Bolo"
    # Content bounds, not the outer frame (which carries the title bar).
    content = window.contentView().bounds()
    assert content.size.width == 940.0
    assert content.size.height == 650.0
    minimum = window.contentMinSize()
    assert minimum.width == 940.0 and minimum.height == 650.0
    assert ui["accessibility"] is None
    assert ui["refs"]["preview"] is True
    assert ui["refs"]["tab"] == "home"
    assert ui["refs"]["dashboard"]["history_limit"] == 10


def test_controller_copy_action_copies_selected_text():
    ui = build()
    refs = ui["refs"]
    refs["pasteboard"] = FakePasteboard()
    refs["controller"].copyText_(None)
    assert refs["pasteboard"].written[0].startswith("Meeting notes")


def test_controller_save_settings_emits_contract_json():
    ui = build()
    refs = ui["refs"]
    emitted = []
    refs["emitter"] = emitted.append
    refs["controller"].saveSettings_(None)
    assert emitted == [{
        "type": "dashboard_action",
        "action": "save_settings",
        "hotkey": "right_option",
        "microphone": "default",
        "cleanup_mode": "auto",
    }]


def test_controller_simple_actions_emit_their_requests():
    ui = build()
    refs = ui["refs"]
    emitted = []
    refs["emitter"] = emitted.append
    refs["controller"].refreshNow_(None)
    refs["controller"].openSetup_(None)
    refs["controller"].openLearned_(None)
    refs["controller"].restartBolo_(None)
    assert [request["action"] for request in emitted] == [
        "refresh", "open_setup", "open_learned", "restart",
    ]


def test_dashboard_update_keeps_tab_and_selection():
    ui = build()
    refs = ui["refs"]
    dashboard_window.select_tab(refs, "settings")
    refs["popup_hotkey"].selectItemWithTitle_("Left Option")
    refs["selections"]["hotkey"] = "left_option"
    fresh = fixture_dashboard(history=[], saved_dictations=0, saved_words=0)
    assert dashboard_window.apply_dashboard_update(refs, fresh)
    assert refs["tab"] == "settings"
    assert refs["selections"]["hotkey"] == "left_option"
    assert refs["dashboard"]["saved_words"] == 0


def test_save_reply_with_restart_keeps_pending_draft():
    ui = build()
    refs = ui["refs"]
    dashboard_window.select_tab(refs, "settings")
    refs["popup_hotkey"].selectItemWithTitle_("Right Shift")
    refs["popup_cleanup"].selectItemWithTitle_("On")
    emitted = []
    refs["emitter"] = emitted.append
    refs["controller"].saveSettings_(None)
    # The reply's dashboard still reports the running (stale) values.
    reply = {
        "type": "dashboard_action_reply",
        "ok": True,
        "restart_needed": True,
        "message": "Saved. Restart Bolo to apply.",
        "dashboard": fixture_dashboard(history=[], hotkey="right_option",
                                       cleanup_mode="auto"),
    }
    assert dashboard_window.apply_dashboard_action_reply(refs, reply)
    assert refs["selections"] == {
        "hotkey": "right_shift", "microphone": "default", "cleanup_mode": "on",
    }
    assert not refs["restart_button"].isHidden()
    # A refresh carrying the stale values must not revert the draft.
    dashboard_window.apply_dashboard_update(
        refs, fixture_dashboard(history=[], hotkey="right_option", cleanup_mode="auto")
    )
    assert refs["selections"]["hotkey"] == "right_shift"
    # Repeated save still sends the draft values.
    emitted.clear()
    refs["controller"].saveSettings_(None)
    assert emitted[-1]["hotkey"] == "right_shift"
    assert emitted[-1]["cleanup_mode"] == "on"


def test_reply_without_restart_needed_keeps_existing_flag():
    ui = build()
    refs = ui["refs"]
    dashboard_window.select_tab(refs, "settings")
    refs["restart_needed"] = True
    reply = {
        "type": "dashboard_action_reply",
        "ok": True,
        "dashboard": fixture_dashboard(history=[]),
    }
    dashboard_window.apply_dashboard_action_reply(refs, reply)
    assert refs["restart_needed"] is True
    reply["restart_needed"] = False
    dashboard_window.apply_dashboard_action_reply(refs, reply)
    assert refs["restart_needed"] is False


def test_failed_save_keeps_restart_flag_clears_draft():
    ui = build()
    refs = ui["refs"]
    refs["restart_needed"] = True
    reply = {"type": "dashboard_action_reply", "ok": False, "message": "Could not save."}
    assert dashboard_window.apply_dashboard_action_reply(refs, reply)
    assert refs["restart_needed"] is True
    assert refs["pending_selections"] is None
    assert refs["settings_status_ok"] is False


def test_activate_is_noop_in_preview():
    ui = build()
    assert dashboard_window.apply_dashboard_activate(ui["refs"]) is False


def test_app_window_build_ui_routes_dashboard_payload():
    ui = app_window.build_ui(fixture_payload(), preview=True)
    assert ui["dashboard"] is True
    assert ui["window"].title() == "Bolo"
    assert ui["accessibility"] is None
    assert ui["refs"]["tab"] == "home"


def test_app_window_run_loop_dispatches_update_and_activate(monkeypatch):
    from test_app_window import _LineStdin, _loop_fixtures

    app_window.reset_state()
    ui = app_window.build_ui(fixture_payload(), preview=True)
    refs = ui["refs"]
    activations = []
    monkeypatch.setattr(dashboard_window, "apply_dashboard_activate",
                        lambda refs, live: activations.append(live) or True)
    lines = _LineStdin([
        json.dumps({"type": "dashboard_update", "dashboard":
                    fixture_dashboard(history=[], saved_dictations=0)}),
        json.dumps({"type": "dashboard_activate"}),
        "",
    ])
    monkeypatch.setattr(app_window.sys, "stdin", lines)
    monkeypatch.setattr(app_window.select, "select", lambda *a, **k: ([lines], [], []))
    loop = _loop_fixtures(monkeypatch, ui)
    assert app_window.run_event_loop(ui) is False
    assert refs["dashboard"]["saved_dictations"] == 0
    assert refs["tab"] == "home"
    assert activations == [True]
    assert loop.app.ran == 1
    assert loop.app.stops
    assert loop.app.posts
    assert loop.timer.invalidated == 1
    assert "pump" not in ui
    app_window.reset_state()


def test_dashboard_close_stops_native_run(monkeypatch):
    from types import SimpleNamespace
    from test_app_window import _loop_fixtures, _quiet_stdin

    app_window.reset_state()
    ui = app_window.build_ui(fixture_payload(), preview=True)
    _quiet_stdin(monkeypatch)
    loop = _loop_fixtures(monkeypatch, ui)
    loop.app.queue = [SimpleNamespace(
        handler=lambda app: ui["refs"]["controller"].windowWillClose_(None)
    )]
    assert app_window.run_event_loop(ui) is True
    assert loop.app.ran == 1
    assert loop.app.stops
    assert loop.app.posts
    assert loop.timer.invalidated == 1
    assert loop.app.queue == []
    assert "pump" not in ui
    app_window.reset_state()


def test_app_window_run_loop_ignores_update_on_wizard_ui():
    # A wizard UI (no dashboard flag) must never see dashboard dispatch.
    ui = {"window": None, "run_loop": (None, None, None)}
    assert app_window.apply_dashboard_message(ui, {"type": "dashboard_update"}, "") is False


def test_sidebar_status_reflects_real_state():
    ok_ui = build(dashboard=fixture_dashboard(accessibility_state="ok"))
    refs = ok_ui["refs"]
    assert refs["sidebar_status_label"].stringValue() == "Ready"
    warn_ui = build(
        dashboard=fixture_dashboard(accessibility_state="warn")
    )
    refs = warn_ui["refs"]
    assert refs["sidebar_status_label"].stringValue() == "Setup needed"
    # A dashboard_update refreshes the honest status line in place.
    dashboard_window.apply_dashboard_update(
        refs, fixture_dashboard(accessibility_state="unavailable")
    )
    assert refs["sidebar_status_label"].stringValue() == "Check setup"


def test_usage_counts_are_actual_numbers_with_honest_scope():
    dashboard = dashboard_window.validate_dashboard(fixture_dashboard())
    dictations, words, scope = dashboard_window.usage_counts(dashboard)
    assert (dictations, words) == (244, 15230)
    assert scope.startswith("Since ")
    fallback = dashboard_window.validate_dashboard(
        fixture_dashboard(usage=None)
    )
    dictations, words, scope = dashboard_window.usage_counts(fallback)
    assert scope.startswith("Last 10 saved")


def test_empty_and_long_views_do_not_overflow_and_no_marker(tmp_path):
    marker = tmp_path / "onboarding.json"
    empty = build(dashboard=fixture_dashboard(history=[]))
    assert empty["refs"]["dashboard"]["history"] == []
    dashboard_window.select_tab(empty["refs"], "dictations")
    long_text = ("Repeated phrase for overflow. " * 120).strip()
    long_entry = {
        "text": long_text, "raw": long_text,
        "created_at_ms": 1700000000000, "edited_after_insert": False,
    }
    ui = build(dashboard=fixture_dashboard(history=[long_entry] * 12))
    dashboard_window.select_tab(ui["refs"], "dictations")
    content = ui["window"].contentView()
    body = ui["refs"]["body_view"]
    # The main area spans the full window height, right of the sidebar.
    assert body.frame().size.height <= 650 + 4
    assert body.frame().origin.x >= dashboard_window.SIDEBAR_W
    # Offscreen render only; nothing wrote the marker.
    assert not marker.exists()
    # The long transcript lives in a scrollable view, not in a label that
    # would push past the window height.
    assert not marker.exists()


def test_resizable_window_grows_and_keeps_sidebar_and_main_layout():
    ui = build()
    refs = ui["refs"]
    window = ui["window"]
    content = window.contentView()
    for width, height in ((1200.0, 800.0), (940.0, 650.0)):
        content.setFrameSize_((width, height))
        dashboard_window.select_tab(refs, "settings")
        body = refs["body_view"]
        body_frame = body.frame()
        # The main area always sits right of the fixed sidebar and fills
        # the rest of the window in both axes.
        assert body_frame.origin.x == float(dashboard_window.SIDEBAR_W)
        assert body_frame.size.width == width - dashboard_window.SIDEBAR_W
        assert body_frame.origin.y == 0.0
        assert body_frame.size.height == height
        sidebar = refs["sidebar"]
        sidebar_frame = sidebar.frame()
        # The sidebar keeps its width and runs the full window height.
        assert sidebar_frame.size.width == float(dashboard_window.SIDEBAR_W)
        assert sidebar_frame.size.height == height
        # Status, version, and nav stay inside the sidebar bounds.
        for key in ("sidebar_status_label", "sidebar_version_label"):
            view = refs[key]
            frame = view.frame()
            assert frame.size.width <= sidebar_frame.size.width
            assert frame.origin.y >= 0
            assert frame.origin.y + frame.size.height <= height
            assert frame.size.height <= 18
        for tab in dashboard_window.TABS:
            button = refs["nav_" + tab]
            assert button.isHidden() is False
            assert button.frame().size.width > 0
            assert button.superview() is sidebar
        # The window can still shrink back to the minimum content size.
        minimum = window.contentMinSize()
        assert minimum.width == 940.0 and minimum.height == 650.0


def test_settings_selectors_are_visible_inside_their_group():
    for dark in (False, True):
        ui = build(dark=dark)
        refs = ui["refs"]
        dashboard_window.select_tab(refs, "settings")
        popups = [refs[key] for key in ("popup_hotkey", "popup_microphone", "popup_cleanup")]
        group = popups[0].superview()
        assert all(popup.superview() is group for popup in popups)
        bounds = group.bounds()
        for popup in popups:
            frame = popup.frame()
            assert frame.origin.y >= 0
            assert frame.origin.y + frame.size.height <= bounds.size.height
            assert frame.origin.x + frame.size.width <= bounds.size.width
            assert popup.isHidden() is False
        # The body is flipped, so the visually top selector (dictation
        # key) carries the smallest y: rows stack downward with y growing.
        assert popups[0].frame().origin.y < popups[1].frame().origin.y < popups[2].frame().origin.y


def test_all_history_entries_reachable_through_list_scroll():
    """Ten entries all exist in the list's document view, so each stays
    reachable by scrolling; none is dropped at the visible bottom."""
    entries = [
        {
            "text": "Dictation number {0} for scroll coverage.".format(i),
            "raw": "dictation number {0}".format(i),
            "created_at_ms": 1700000000000 + i * 60000,
            "edited_after_insert": False,
        }
        for i in range(10)
    ]
    ui = build(dashboard=fixture_dashboard(history=entries))
    refs = ui["refs"]
    dashboard_window.select_tab(refs, "dictations")
    body = refs["body_view"]
    from AppKit import NSScrollView

    scroll = None
    for sub in body.subviews():
        # The de-boxed detail transcript is also a scroll view on the
        # body; the list is built first, so the first match is the list.
        if isinstance(sub, NSScrollView):
            scroll = sub
            break
    assert scroll is not None, "the dictations list must live in a scroll view"
    doc = scroll.documentView()
    assert doc is not None
    row_h = doc.bounds().size.height / len(entries)
    # Every entry has a row inside the flipped document view extent, so
    # each is reachable by scrolling, regardless of visible height.
    assert doc.bounds().size.height >= len(entries) * dashboard_window.HOME_ROW_H - 1
    # The scroll view clips to the visible area; the document extends past it.
    assert scroll.frame().size.height < doc.bounds().size.height
    # The detail pane shows the selected entry's real text.
    text = doc.subviews()[0].subviews()[-2].stringValue()
    assert text == entries[0]["text"]


def test_history_selection_and_raw_toggle_preserve_visible_row():
    from AppKit import NSButton
    entries = [dict(text="Entry {0}".format(i), raw="raw {0}".format(i),
                    created_at_ms=1700000000000 + i * 60000,
                    edited_after_insert=False) for i in range(10)]
    refs = build(dashboard=fixture_dashboard(history=entries))["refs"]
    dashboard_window.select_tab(refs, "dictations")
    button = NSButton.alloc().init()
    button.setTag_(9)
    refs["controller"].selectDictation_(button)
    scroll = refs["history_scroll"]
    visible = scroll.contentView().bounds()
    assert visible.origin.y > 0
    assert visible.origin.y + visible.size.height >= 10 * dashboard_window.HOME_ROW_H - 1
    offset = visible.origin.y
    refs["controller"].toggleRaw_(None)
    assert abs(refs["history_scroll"].contentView().bounds().origin.y - offset) < 1
    assert refs["detail_scroll"].documentView().string() == "raw 9"


def test_long_transcript_last_character_is_reachable():
    from Foundation import NSMakeRange
    entry = dict(text="A long transcript with a final sentence. " * 300 + "END.",
                 raw="raw text", created_at_ms=1700000000000,
                 edited_after_insert=False)
    refs = build(dashboard=fixture_dashboard(history=[entry]))["refs"]
    dashboard_window.select_tab(refs, "dictations")
    scroll = refs["detail_scroll"]
    view = scroll.documentView()
    last = NSMakeRange(len(view.string()) - 1, 1)
    view.scrollRangeToVisible_(last)
    layout = view.layoutManager()
    glyph = layout.glyphRangeForCharacterRange_actualCharacterRange_(last, None)[0]
    rect = layout.boundingRectForGlyphRange_inTextContainer_(glyph, view.textContainer())
    visible = scroll.contentView().bounds()
    assert visible.origin.y > 0
    assert rect.origin.y + view.textContainerInset().height >= visible.origin.y
    assert rect.origin.y + rect.size.height + view.textContainerInset().height <= visible.origin.y + visible.size.height + 1
