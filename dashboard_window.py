#!/usr/bin/env python3
"""Native AppKit dashboard: Home, Dictations, and Settings in one window.

The Rust runtime spawns this helper with a mode "dashboard" payload and
keeps talking over stdin: `dashboard_update` lines refresh facts and
history, `dashboard_action_reply` lines answer the window's requests.
The window prints one `dashboard_action` JSON line per user action on
stdout. Everything renders with AppKit in the shared warm-paper identity
from bolo_brand: system typography for body text, Georgia for headings.
Counts and transcripts come only from the payload's retained history
(the last N saved dictations), never from files this window reads, and
no file path, key, or debug line is ever shown in the UI.

Layout notes: the sidebar is a FlippedView (y grows downward) so nav
rows and the footer never invert on resize. Main headings are system
sans 22 semibold, sections 14/15 semibold, body 13, metadata 11/12;
only the small "bolo" wordmark keeps the serif brand voice.
"""

import json
import sys
import time

import bolo_brand

WIDTH = 940
HEIGHT = 650
# The sidebar is a fixed 184pt column running the full window height;
# the main content starts right of it and stretches on resize.
SIDEBAR_W = 184
MAIN_W = WIDTH - SIDEBAR_W
# The main content margin is a compact 28pt; the main area starts right
# of the fixed sidebar and stretches on resize.
MARGIN = 28
INNER_W = MAIN_W - 2 * MARGIN
BODY_TOP = 24
# Visual type scale: system sans everywhere except the small wordmark.
TITLE_SIZE = 22
SECTION_SIZE = 14
BODY_SIZE = 13
META_SIZE = 11
HOME_ROW_H = 62

TABS = ("home", "dictations", "settings")
TAB_TITLES = {"home": "Home", "dictations": "Dictations", "settings": "Settings"}
CLEANUP_MODES = ("auto", "on", "off")
CLEANUP_TITLES = {"auto": "Auto", "on": "On", "off": "Off"}
KNOWN_HOTKEYS = (
    "left_option",
    "right_option",
    "right_control",
    "right_shift",
    "fn",
    "caps_lock",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10",
    "f11", "f12", "f13", "f14", "f15", "f16", "f17", "f18", "f19",
)
ACTIONS = ("refresh", "save_settings", "restart", "open_setup", "open_learned")

# Mutable module state: the window renderer stores its refs here so the
# single AppKit controller can reach them from any action callback.
STATE = {"refs": None}
_CONTROLLER = {"cls": None}


def hotkey_title(hotkey):
    """Human name for a configured dictation key, via app_window."""
    import app_window

    return app_window.hotkey_display_name(hotkey)


def validate_dashboard(value):
    """Normalize one dashboard payload; None when it is not an object.

    Every field falls back to a safe default so one malformed field can
    never break the window. History entries keep only the contract
    fields, newest-first as sent by the runtime.
    """
    if not isinstance(value, dict):
        return None
    out = {}

    def text(name, default=""):
        raw = value.get(name)
        return raw if isinstance(raw, str) else default

    def count(name):
        raw = value.get(name)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            return raw
        return 0

    out["version"] = text("version")
    out["hotkey"] = text("hotkey") or "left_option"
    out["microphone"] = text("microphone") or "default"
    microphones = value.get("microphones")
    out["microphones"] = [
        item for item in (microphones or []) if isinstance(item, str) and item
    ]
    # Stable dropdown choices: {value, label} per device. Values are
    # uid:/name: wire forms from the runtime; labels stay human-readable
    # names. Older payloads without the field fall back to plain names.
    choices = value.get("microphone_choices")
    out["microphone_choices"] = [
        {
            "value": item["value"],
            "label": item.get("label") if isinstance(item.get("label"), str) else item["value"],
        }
        for item in (choices if isinstance(choices, list) else [])
        if isinstance(item, dict)
        and isinstance(item.get("value"), str)
        and item["value"]
    ]
    cleanup = value.get("cleanup_mode")
    out["cleanup_mode"] = cleanup if cleanup in CLEANUP_MODES else "auto"
    state = value.get("accessibility_state")
    out["accessibility_state"] = (
        state if state in ("ok", "warn", "unavailable") else "unavailable"
    )
    out["provider"] = text("provider")
    limit = value.get("history_limit")
    out["history_limit"] = (
        limit
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        else 10
    )
    history = []
    raw_history = value.get("history")
    for entry in raw_history if isinstance(raw_history, list) else []:
        if not isinstance(entry, dict):
            continue
        entry_text = entry.get("text")
        entry_text = entry_text if isinstance(entry_text, str) else ""
        entry_raw = entry.get("raw")
        entry_raw = entry_raw if isinstance(entry_raw, str) else entry_text
        created = entry.get("created_at_ms")
        created = (
            created
            if isinstance(created, int) and not isinstance(created, bool)
            else 0
        )
        edited = entry.get("edited_after_insert")
        history.append(
            {
                "text": entry_text,
                "raw": entry_raw,
                "created_at_ms": created,
                "edited_after_insert": bool(edited),
            }
        )
    out["history"] = history
    out["saved_dictations"] = count("saved_dictations")
    out["saved_words"] = count("saved_words")
    out["learned_words_count"] = count("learned_words_count")
    # Optional cumulative usage from the runtime: dictations, words,
    # recording time, and when the counters started. Absent usage falls
    # back to the retained history (see usage_summary).
    usage = value.get("usage")
    out["usage"] = usage if isinstance(usage, dict) else None
    return out


def _local_date_ms(now_ms=None):
    """Midnight at the start of today, local time, in epoch millis."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    stamp = time.localtime(now_ms / 1000.0)
    start = time.struct_time(
        (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 0, 0, 0, 0, 0, -1)
    )
    return int(time.mktime(start) * 1000)


def _format_since_date(now_ms=None):
    """The Since date label for the usage strip, in local time."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return time.strftime("%B %-d, %Y", time.localtime(now_ms / 1000.0))


def palette_extras(dark=False):
    """Dashboard-local surfaces layered on the shared brand palette.

    Pale stone sidebar, card surfaces, hairlines, the soft fill for
    secondary actions, and a gentle clay tint for selected nav and rows.
    """
    if dark:
        return {
            # Neutral charcoal canvas, slightly darker stone sidebar.
            "canvas": (0.133, 0.133, 0.141),
            "sidebar": (0.110, 0.110, 0.118),
            "card": (0.180, 0.180, 0.188),
            "hairline": (0.271, 0.271, 0.279),
            "tint": (0.312, 0.224, 0.184),
            "soft": (0.208, 0.208, 0.218),
        }
    return {
        # Neutral ivory canvas, slightly darker warm stone sidebar.
        "canvas": (0.973, 0.969, 0.957),
        "sidebar": (0.933, 0.929, 0.909),
        "card": (0.996, 0.995, 0.991),
        "hairline": (0.886, 0.882, 0.866),
        "tint": (0.957, 0.898, 0.859),
        "soft": (0.945, 0.941, 0.929),
    }


def usage_counts(dashboard):
    """Actual counts for the Home usage blocks plus an honest scope note.

    With runtime usage the totals are cumulative with a Since date;
    without it the retained-history scope is named, never invented.
    """
    usage = dashboard.get("usage")
    if isinstance(usage, dict):
        dictations = usage.get("dictations")
        words = usage.get("words")
        if not isinstance(dictations, int) or isinstance(dictations, bool):
            dictations = 0
        if not isinstance(words, int) or isinstance(words, bool):
            words = 0
        started = usage.get("started_at_ms")
        if not isinstance(started, int) or isinstance(started, bool) or started <= 0:
            started = None
        return dictations, words, "Since {0}".format(_format_since_date(started))
    return (
        dashboard["saved_dictations"],
        dashboard["saved_words"],
        "Last {0} saved".format(dashboard["history_limit"]),
    )


def build_action(action, hotkey=None, microphone=None, cleanup_mode=None):
    """One contract request object, or None when values fail validation.

    save_settings carries all three values; every other action carries
    none. Settings values are validated here, before anything is ever
    emitted to the runtime.
    """
    if action not in ACTIONS:
        return None
    request = {"type": "dashboard_action", "action": action}
    if action == "save_settings":
        if not isinstance(hotkey, str) or not hotkey:
            return None
        if not isinstance(microphone, str) or not microphone:
            return None
        if cleanup_mode not in CLEANUP_MODES:
            return None
        # The contract sends the literal system default as "default";
        # stable device values are uid:/name: wire forms; a legacy
        # plain name is still accepted for older frontends.
        if microphone == "default":
            pass
        elif microphone.startswith("uid:") or microphone.startswith("name:"):
            pass
        elif not microphone.strip():
            return None
        request["hotkey"] = hotkey
        request["microphone"] = microphone
        request["cleanup_mode"] = cleanup_mode
    return request


def emit_request(request, out=None):
    """Print one contract request line on stdout for the runtime."""
    stream = out if out is not None else sys.stdout
    stream.write(json.dumps(request) + "\n")
    stream.flush()


def copy_to_pasteboard(text, pasteboard=None):
    """Copy plain text to the macOS pasteboard; True on success.

    `pasteboard` is injectable so tests can prove the action without
    touching the real clipboard.
    """
    if not isinstance(text, str) or not text:
        return False
    if pasteboard is None:
        from AppKit import NSPasteboard

        pasteboard = NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    ok = pasteboard.setString_forType_(text, "public.utf8-plain-text")
    return bool(ok)


def timestamp_label(created_at_ms, now_ms=None):
    """A readable timestamp: today shows the clock, older shows the date."""
    if not isinstance(created_at_ms, int) or isinstance(created_at_ms, bool):
        return "Earlier"
    if created_at_ms <= 0:
        return "Earlier"
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    stamp = time.localtime(created_at_ms / 1000.0)
    today = time.localtime(now_ms / 1000.0)
    if stamp.tm_year == today.tm_year and stamp.tm_yday == today.tm_yday:
        return time.strftime("Today at %H:%M", stamp)
    return time.strftime("%b %d at %H:%M", stamp)


def preview_text(text, limit=200):
    """One truncated line for list cards; full text lives in the detail."""
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def usage_summary(dashboard, now_ms=None):
    """The Home usage strip: actual cumulative counts with a Since date.

    When the runtime sends usage (dictations, words, recording_ms,
    started_at_ms), the counts are real cumulative totals since that
    start date. Without usage the strip falls back to the retained
    saved history and labels that scope explicitly, never inventing a
    lifetime claim.
    """
    usage = dashboard.get("usage")
    if isinstance(usage, dict):
        dictations = usage.get("dictations")
        words = usage.get("words")
        if not isinstance(dictations, int) or isinstance(dictations, bool):
            dictations = 0
        if not isinstance(words, int) or isinstance(words, bool):
            words = 0
        started = usage.get("started_at_ms")
        since = (
            _format_since_date(started)
            if isinstance(started, int) and not isinstance(started, bool) and started > 0
            else _format_since_date(now_ms)
        )
        return (
            "Since {0}: {1} dictations, {2} words."
        ).format(since, dictations, words)
    return (
        "Last {0} saved dictations: {1} saved, {2} words. "
        "{3} learned words."
    ).format(
        dashboard["history_limit"],
        dashboard["saved_dictations"],
        dashboard["saved_words"],
        dashboard["learned_words_count"],
    )


def activity_summary(dashboard):
    """Backwards-compatible alias for the retained-history summary."""
    return usage_summary(dashboard)


def permission_line(state):
    """True, plain permission copy for one accessibility_state."""
    lines = {
        "ok": "Accessibility is granted. Bolo can type wherever your cursor is.",
        "warn": "Accessibility needs a grant before Bolo can type. Open setup to finish.",
        "unavailable": "Accessibility could not be checked right now. Open setup to review.",
    }
    return lines.get(state) or lines["unavailable"]


def microphone_choices(dashboard):
    """Dropdown (value, title) pairs for the microphone popup.

    Starts with the System Default choice; device rows come from the
    runtime's stable `microphone_choices` values so duplicate display
    names stay distinguishable. Older payloads without the field fall
    back to plain names. The current selection is always selectable so
    a disconnected saved device is never dropped from the list.
    """
    rows = [("default", "System Default")]
    for choice in dashboard.get("microphone_choices") or []:
        if choice["value"] == "default":
            continue
        if any(value == choice["value"] for value, _ in rows):
            continue
        rows.append((choice["value"], choice["label"]))
    current = dashboard["microphone"]
    if current and current != "default":
        if not any(value == current for value, _ in rows):
            # Legacy plain-name payload or a current selection the new
            # list does not carry: keep it selectable, labeled by name.
            label = current
            for choice in dashboard.get("microphone_choices") or []:
                if choice["value"] == current:
                    label = choice["label"]
                    break
            rows.append((current, label))
    if len(rows) == 1 and dashboard["microphones"]:
        # No stable choices arrived: fall back to plain name values.
        for mic in dashboard["microphones"]:
            if mic == "default":
                continue
            rows.append((mic, mic))
    return rows


def settings_options(dashboard):
    """Popup options as (value, title) pairs; current values included.

    The microphone list always starts with the system default; the
    current hotkey stays selectable even when it is not a known key.
    """
    hotkeys = []
    current = dashboard["hotkey"]
    if current not in KNOWN_HOTKEYS:
        hotkeys.append((current, hotkey_title(current)))
    for key in KNOWN_HOTKEYS:
        hotkeys.append((key, hotkey_title(key)))
    cleanup = [(mode, CLEANUP_TITLES[mode]) for mode in CLEANUP_MODES]
    return {
        "hotkey": hotkeys,
        "microphone": microphone_choices(dashboard),
        "cleanup": cleanup,
    }


def select_value(values, wanted):
    """The popup index for `wanted`, or 0 when it is missing.

    Accepts either a list of plain value strings (as the renderer
    builds) or (value, title) pairs (as settings_options returns).
    """
    if wanted is None:
        return 0
    for index, item in enumerate(values):
        value = item[0] if isinstance(item, (tuple, list)) else item
        if value == wanted:
            return index
    return 0


def set_settings_status(refs, text, ok=None):
    """Store and, when visible, show the Settings status line."""
    refs["settings_status_text"] = text
    refs["settings_status_ok"] = ok
    label = refs.get("settings_status_label")
    if label is not None and refs.get("tab") == "settings":
        pal = refs["palette"]()
        color = {
            True: bolo_brand.native_color(pal["success"]),
            False: bolo_brand.native_color(pal["error"]),
            None: bolo_brand.native_color(pal["muted"]),
        }.get(ok)
        label.setStringValue_(text)
        label.setTextColor_(color)


def set_restart_needed(refs, restart_needed):
    """Remember and show the Restart Bolo affordance only when true.

    An absent (None) flag never changes the pending state: a refresh
    reply that carries no restart_needed field keeps an existing pending
    restart visible, while an explicit False clears it and the pending
    draft selections with it.
    """
    if restart_needed is None:
        button = refs.get("restart_button")
        if button is not None and refs.get("tab") == "settings":
            button.setHidden_(not refs.get("restart_needed"))
        return
    refs["restart_needed"] = bool(restart_needed)
    if not refs["restart_needed"]:
        refs["pending_selections"] = None
    button = refs.get("restart_button")
    if button is not None and refs.get("tab") == "settings":
        button.setHidden_(not refs["restart_needed"])


def apply_dashboard_update(refs, value):
    """Apply one dashboard_update payload; keeps tab and selections.

    Settings selections survive an update when they still exist among
    the fresh options; the dictation selection falls back to the first
    entry when its index is gone. Nothing here is an editable text
    field, so no text selection can ever be moved.
    """
    dashboard = validate_dashboard(value)
    if dashboard is None:
        return False
    previous = refs.get("selections") or {}
    refs["dashboard"] = dashboard
    _update_sidebar_status(refs)
    options = settings_options(dashboard)
    selections = {}
    for name, key, option_list in (
        ("hotkey", "hotkey", options["hotkey"]),
        ("microphone", "microphone", options["microphone"]),
        ("cleanup_mode", "cleanup_mode", options["cleanup"]),
    ):
        wanted = previous.get(name)
        if wanted is not None and any(value == wanted for value, _ in option_list):
            selections[name] = wanted
        else:
            selections[name] = dashboard[key]
    # A pending draft (saved but awaiting restart) survives refreshes:
    # the returned dashboard still reports the running values, so the
    # user's pick is overlaid back instead of silently reverting.
    pending = refs.get("pending_selections")
    if pending and refs.get("restart_needed"):
        pending_mic = pending.get("microphone")
        choices = dashboard.get("microphone_choices") or []
        if pending_mic and pending_mic != "default" and not any(
            choice["value"] == pending_mic for choice in choices
        ):
            dashboard["microphone_choices"] = list(choices) + [
                {"value": pending_mic, "label": pending_mic}
            ]
        options = settings_options(dashboard)
        for name, option_list in (
            ("hotkey", options["hotkey"]),
            ("microphone", options["microphone"]),
            ("cleanup_mode", options["cleanup"]),
        ):
            wanted = pending.get(name)
            if wanted is not None:
                selections[name] = wanted
    refs["selections"] = selections
    selected = refs.get("selected_index")
    if selected is not None and not 0 <= selected < len(dashboard["history"]):
        refs["selected_index"] = 0 if dashboard["history"] else None
    render_body(refs)
    return True


def apply_dashboard_action_reply(refs, reply):
    """Apply one dashboard_action_reply; fresh dashboard first, then status."""
    if not isinstance(reply, dict) or reply.get("type") != "dashboard_action_reply":
        return False
    ok = bool(reply.get("ok"))
    restart_needed = reply.get("restart_needed")
    # A reply without restart_needed carries no verdict on the pending
    # restart: keep whatever flag is already showing (None means no
    # change), never invent a clear or a set from silence.
    dashboard = reply.get("dashboard")
    if ok and isinstance(dashboard, dict):
        # The user's saved draft is the source of truth while the runtime
        # restarts: the returned dashboard can still carry the running
        # (pre-save) values, so stash the selections before the fresh
        # dashboard can overwrite them, and store the restart flag first
        # so the overlay in apply_dashboard_update sees it.
        refs["pending_selections"] = dict(refs.get("selections") or {})
        if restart_needed is not None:
            refs["restart_needed"] = bool(restart_needed)
    if isinstance(dashboard, dict):
        apply_dashboard_update(refs, dashboard)
    if not ok:
        refs["pending_selections"] = None
    message = reply.get("message")
    if not isinstance(message, str) or not message:
        message = "Saved." if ok else "Could not save your changes."
    set_settings_status(refs, message, ok=ok)
    set_restart_needed(refs, restart_needed)
    return True


def _controller_class_cached(NSObject):
    """Build the dashboard controller once per process, like the wizard's."""
    cached = _CONTROLLER.get("cls")
    if cached is not None:
        return cached

    class DashboardController(NSObject):
        """All dashboard button actions; refs come from STATE."""

        def _refs(self):
            return STATE.get("refs") or {}

        def _emit_(self, request):
            refs = self._refs()
            emitter = refs.get("emitter") or emit_request
            emitter(request)

        def homeNav_(self, sender):
            refs = self._refs()
            refs["tab"] = "home"
            restyle_nav(refs)
            render_body(refs)

        def dictationsNav_(self, sender):
            refs = self._refs()
            refs["tab"] = "dictations"
            restyle_nav(refs)
            render_body(refs)

        def settingsNav_(self, sender):
            refs = self._refs()
            refs["tab"] = "settings"
            restyle_nav(refs)
            render_body(refs)

        def _simple_action(self, action):
            request = build_action(action)
            if request is not None:
                self._emit_(request)

        def refreshNow_(self, sender):
            self._simple_action("refresh")

        def openSetup_(self, sender):
            self._simple_action("open_setup")

        def openLearned_(self, sender):
            self._simple_action("open_learned")

        def restartBolo_(self, sender):
            self._simple_action("restart")

        def _selected_value(self, popup, values):
            if popup is None or not values:
                return None
            index = popup.indexOfSelectedItem()
            if not 0 <= index < len(values):
                return None
            return values[index]

        def saveSettings_(self, sender):
            refs = self._refs()
            hotkey = self._selected_value(
                refs.get("popup_hotkey"), refs.get("hotkey_values")
            )
            if hotkey is None:
                hotkey = (refs.get("selections") or {}).get("hotkey")
            microphone = self._selected_value(
                refs.get("popup_microphone"), refs.get("mic_values")
            )
            if microphone is None:
                microphone = (refs.get("selections") or {}).get("microphone")
            cleanup = self._selected_value(
                refs.get("popup_cleanup"), refs.get("cleanup_values")
            )
            if cleanup is None:
                cleanup = (refs.get("selections") or {}).get("cleanup_mode")
            request = build_action(
                "save_settings",
                hotkey=hotkey,
                microphone=microphone,
                cleanup_mode=cleanup,
            )
            if request is None:
                set_settings_status(
                    refs, "Choose valid settings before saving.", ok=False
                )
                return
            # Write the live popup picks back into refs so the pending
            # draft (stashed when the reply arrives) holds what the user
            # actually saved, not the pre-save values.
            refs["selections"] = {
                "hotkey": hotkey,
                "microphone": microphone,
                "cleanup_mode": cleanup,
            }
            set_settings_status(refs, "Saving changes.", ok=None)
            self._emit_(request)

        def copyText_(self, sender):
            refs = self._refs()
            try:
                index = int(sender.tag()) if sender is not None else 0
            except (TypeError, ValueError):
                return
            history = (refs.get("dashboard") or {}).get("history") or []
            if not 0 <= index < len(history):
                return
            copied = copy_to_pasteboard(
                history[index]["text"], pasteboard=refs.get("pasteboard")
            )
            label = refs.get("copied_label")
            if label is not None:
                label.setStringValue_("Copied." if copied else "Could not copy.")

        def selectDictation_(self, sender):
            refs = self._refs()
            try:
                index = int(sender.tag()) if sender is not None else 0
            except (TypeError, ValueError):
                return
            history = (refs.get("dashboard") or {}).get("history") or []
            if not 0 <= index < len(history):
                return
            scroll = refs.get("history_scroll")
            if scroll is not None:
                doc = scroll.documentView()
                if doc is not None:
                    refs["list_scroll_offset"] = scroll.contentView().bounds().origin.y
            refs["selected_index"] = index
            render_body(refs)

        def toggleRaw_(self, sender):
            refs = self._refs()
            refs["show_raw"] = not refs.get("show_raw")
            scroll = refs.get("history_scroll")
            if scroll is not None:
                doc = scroll.documentView()
                if doc is not None:
                    refs["list_scroll_offset"] = scroll.contentView().bounds().origin.y
            render_body(refs)

        def windowWillClose_(self, notification):
            import app_window

            app_window.STATE["user_done"] = True

    _CONTROLLER["cls"] = DashboardController
    return DashboardController


def restyle_nav(refs):
    """Recolor the three sidebar pills and icon tints for the current tab."""
    pal = refs["palette"]()
    extras = palette_extras(dark=refs["dark"])
    for name in TABS:
        button = refs.get("nav_" + name)
        if button is None:
            continue
        active = refs["tab"] == name
        if active:
            button.setAttributedTitle_(_styled_title(
                TAB_TITLES[name], pal["text"], size=13, medium=True,
                align="left",
            ))
            button.layer().setBackgroundColor_(
                bolo_brand.native_color(extras["tint"]).CGColor()
            )
        else:
            button.setAttributedTitle_(_styled_title(
                TAB_TITLES[name], pal["muted"], size=13, medium=False,
                align="left",
            ))
            button.layer().setBackgroundColor_(None)
        icon = refs.get("nav_icon_" + name)
        if icon is not None:
            icon.setContentTintColor_(bolo_brand.native_color(
                pal["text"] if active else pal["muted"]
            ))


def _styled_title(text, color, size=13, medium=False, align="center"):
    """An attributed button title in the brand palette."""
    from AppKit import (
        NSAttributedString,
        NSFont,
        NSFontWeightMedium,
        NSFontWeightRegular,
        NSMutableParagraphStyle,
        NSTextAlignmentCenter,
        NSTextAlignmentLeft,
    )

    paragraph = NSMutableParagraphStyle.alloc().init()
    paragraph.setAlignment_(
        NSTextAlignmentLeft if align == "left" else NSTextAlignmentCenter
    )
    if align == "left":
        # Nav rows: icon sits at the left pad, the label clears it.
        paragraph.setFirstLineHeadIndent_(40)
    font = NSFont.systemFontOfSize_weight_(
        size, NSFontWeightMedium if medium else NSFontWeightRegular
    )
    return NSAttributedString.alloc().initWithString_attributes_(
        text, {"NSFont": font, "NSColor": bolo_brand.native_color(color),
               "NSParagraphStyle": paragraph}
    )


def _status_text(state):
    """A short, honest readiness word for the sidebar status line."""
    return {
        "ok": "Ready",
        "warn": "Setup needed",
        "unavailable": "Check setup",
    }.get(state, "Check setup")


def _update_sidebar_status(refs):
    """Refresh the sidebar readiness dot and word from real state."""
    if refs.get("sidebar_status_dot") is None and refs.get("sidebar_status_label") is None:
        return
    pal = refs["palette"]()
    dashboard = refs.get("dashboard") or {}
    state = dashboard.get("accessibility_state") or "unavailable"
    setup = refs.get("sidebar_setup_button")
    if setup is not None:
        setup.setHidden_(state == "ok")
    dot = refs.get("sidebar_status_dot")
    if dot is not None:
        key = {"ok": "success", "warn": "error", "unavailable": "muted"}[state]
        dot.layer().setBackgroundColor_(
            bolo_brand.native_color(pal[key]).CGColor()
        )
    label = refs.get("sidebar_status_label")
    if label is not None:
        label.setStringValue_(_status_text(state))


def _build_sidebar(controller, refs, content, payload):
    """The quiet writing-desk column: brand, nav pills, status, version.

    The 184pt sidebar runs the full window height and keeps its width
    on resize; only its height follows the window.
    """
    import app_window

    from AppKit import NSTextField, NSMakeRect, NSView
    from AppKit import NSViewHeightSizable, NSViewMinYMargin

    pal = refs["palette"]()
    dark = refs["dark"]
    extras = palette_extras(dark=dark)
    sidebar = app_window._APPKIT_CLASSES[0].alloc().initWithFrame_(
        NSMakeRect(0, 0, SIDEBAR_W, HEIGHT)
    )
    sidebar.setWantsLayer_(True)
    sidebar.layer().setBackgroundColor_(
        bolo_brand.native_color(extras["sidebar"]).CGColor()
    )
    sidebar.setAutoresizingMask_(NSViewHeightSizable)
    content.addSubview_(sidebar)
    refs["sidebar"] = sidebar

    mark_view_cls = app_window._APPKIT_CLASSES[1]
    mark = mark_view_cls.alloc().initWithFrame_(NSMakeRect(20, 20, 20, 20))
    mark.configureWithDark_(dark)
    sidebar.addSubview_(mark)

    field = NSTextField.labelWithString_("bolo")
    field.setFrame_(NSMakeRect(48, 20, 110, 22))
    field.setFont_(app_window.heading_font(17, bold=True))
    field.setTextColor_(bolo_brand.native_color(pal["text"]))
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    sidebar.addSubview_(field)

    for index, tab in enumerate(TABS):
        button = _nav_button(controller, refs, tab, 8, 68 + index * 40, sidebar)
        refs["nav_" + tab] = button
    restyle_nav(refs)

    # The readiness cluster sits near the bottom of the column and is
    # pinned to the far (visual bottom) edge on resize, flipping its
    # own y so the dot and label move together.
    dot = NSView.alloc().initWithFrame_(NSMakeRect(20, HEIGHT - 76, 8, 8))
    dot.setWantsLayer_(True)
    dot.layer().setCornerRadius_(4)
    sidebar.addSubview_(dot)
    refs["sidebar_status_dot"] = dot

    status = NSTextField.labelWithString_(_status_text(
        refs["dashboard"]["accessibility_state"]
    ))
    status.setFrame_(NSMakeRect(36, HEIGHT - 79, SIDEBAR_W - 52, 15))
    status.setFont_(_body_font(12))
    status.setTextColor_(bolo_brand.native_color(pal["text"]))
    status.setBezeled_(False)
    status.setDrawsBackground_(False)
    status.setEditable_(False)
    status.setSelectable_(False)
    sidebar.addSubview_(status)
    refs["sidebar_status_label"] = status

    version = refs["dashboard"]["version"] or ""
    version_text = "Bolo {0}".format(version) if version else "Bolo"
    version_label = NSTextField.labelWithString_(version_text)
    version_label.setFrame_(NSMakeRect(20, HEIGHT - 40, SIDEBAR_W - 36, 15))
    version_label.setFont_(_body_font(12))
    version_label.setTextColor_(bolo_brand.native_color(pal["muted"]))
    version_label.setBezeled_(False)
    version_label.setDrawsBackground_(False)
    version_label.setEditable_(False)
    version_label.setSelectable_(False)
    sidebar.addSubview_(version_label)
    refs["sidebar_version_label"] = version_label

    setup = _button(
        controller, refs, sidebar, "Open setup",
        20, HEIGHT - 132, SIDEBAR_W - 40, 32, "openSetup:", soft=True,
    )
    refs["sidebar_setup_button"] = setup
    # Flexible top margins anchor this fixed-size cluster to the bottom.
    for view in (dot, status, version_label, setup):
        view.setAutoresizingMask_(NSViewMinYMargin)
    _update_sidebar_status(refs)


def _nav_button(controller, refs, tab, x, y, parent):
    """One borderless nav pill with an SF Symbol icon and gentle clay tint."""
    from AppKit import (
        NSButton,
        NSImageView,
        NSMakeRect,
        NSImage,
        NSImageScaleProportionallyUpOrDown,
    )

    selectors = {
        "home": "homeNav:",
        "dictations": "dictationsNav:",
        "settings": "settingsNav:",
    }
    symbols = {
        "home": "house",
        "dictations": "text.alignleft",
        "settings": "slider.horizontal.3",
    }
    button = NSButton.buttonWithTitle_target_action_(
        TAB_TITLES[tab], refs["controller"], selectors[tab]
    )
    button.setBezelStyle_(6)
    button.setBordered_(False)
    button.setWantsLayer_(True)
    button.layer().setCornerRadius_(8.0)
    button.layer().setBackgroundColor_(None)
    button.setFrame_(NSMakeRect(x, y, SIDEBAR_W - 40, 36))
    parent.addSubview_(button)

    icon = NSImageView.alloc().initWithFrame_(NSMakeRect(x + 16, y + 10, 16, 16))
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        symbols[tab], TAB_TITLES[tab]
    )
    if image is not None:
        image.setSize_((16, 16))
        icon.setImage_(image)
        icon.setContentHuggingPriority_forOrientation_(260, 0)
    icon.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    icon.setEditable_(False)
    icon.setAnimates_(False)
    parent.addSubview_(icon)
    refs["nav_icon_" + tab] = icon
    return button


def _clear_body(refs):
    """Remove the previous body view so re-renders never stack."""
    old = refs.get("body_view")
    if old is not None:
        old.removeFromSuperview()
        refs["body_view"] = None


def _label(parent, text, frame, font, color, wrap=False, truncate=False):
    from AppKit import (
        NSTextField,
        NSMakeRect,
        NSTextAlignmentLeft,
        NSLineBreakByWordWrapping,
        NSLineBreakByTruncatingTail,
    )

    field = NSTextField.labelWithString_(text)
    field.setFrame_(NSMakeRect(*frame))
    field.setFont_(font)
    field.setTextColor_(bolo_brand.native_color(color))
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(True)
    if truncate:
        field.setUsesSingleLineMode_(True)
        field.setLineBreakMode_(NSLineBreakByTruncatingTail)
    elif wrap:
        field.setLineBreakMode_(NSLineBreakByWordWrapping)
    parent.addSubview_(field)
    return field


def _text_view(parent, text, frame, font, color, dark):
    """A scrollable, selectable, non-editable text view for long text."""
    from AppKit import (
        NSMakeRect,
        NSScrollView,
        NSTextView,
        NSScrollElasticityNone,
    )

    text = text or ""
    scroll_view = NSScrollView.alloc().initWithFrame_(NSMakeRect(*frame))
    scroll_view.setHasVerticalScroller_(True)
    scroll_view.setDrawsBackground_(False)
    scroll_view.setBorderType_(0)
    view = NSTextView.alloc().initWithFrame_(
        NSMakeRect(0, 0, frame[2] - 4, frame[3] - 4)
    )
    view.setString_(text)
    view.setFont_(font)
    view.setTextColor_(bolo_brand.native_color(color))
    view.setDrawsBackground_(False)
    view.setTextContainerInset_((4, 8))
    view.setEditable_(False)
    view.setSelectable_(True)
    view.setVerticallyResizable_(True)
    view.setHorizontallyResizable_(False)
    scroll_view.setDocumentView_(view)
    parent.addSubview_(scroll_view)
    return scroll_view, view


def _button(controller, refs, parent, title, x, y, w, h, action,
            tag=0, filled=False, soft=False):
    """One borderless rounded button.

    filled buttons carry the clay brand color, soft buttons the warm
    low-contrast fill; both keep their native bezel behavior, focus
    ring, and keyboard accessibility underneath the layer surface.
    """
    from AppKit import NSButton, NSMakeRect

    pal = refs["palette"]()
    extras = palette_extras(dark=refs["dark"])
    button = NSButton.buttonWithTitle_target_action_(title, refs["controller"], action)
    button.setBezelStyle_(6)  # NSBezelStyleRounded keeps native press/focus
    button.setBordered_(True)
    button.setFrame_(NSMakeRect(x, y, w, h))
    button.setTag_(tag)
    button.setAttributedTitle_(_styled_title(
        title,
        pal["button_text"] if filled else pal["text"],
        size=13,
        medium=filled,
    ))
    if filled or soft:
        # The visible surface is the layer; the native bezel is dropped
        # so the flat clay or soft fill stays exact in light and dark.
        button.setBordered_(False)
        button.setWantsLayer_(True)
        button.layer().setCornerRadius_(7.0)
        if filled:
            button.layer().setBackgroundColor_(
                bolo_brand.native_color(pal["button"]).CGColor()
            )
        else:
            button.layer().setBackgroundColor_(
                bolo_brand.native_color(extras["soft"]).CGColor()
            )
    parent.addSubview_(button)
    return button


def _card(parent, frame, dark, radius=12.0, border=True):
    """A flat surface card: shared surface color, soft corner, hairline."""
    from AppKit import NSMakeRect, NSView

    pal = bolo_brand.palette(dark=dark)
    extras = palette_extras(dark=dark)
    card = NSView.alloc().initWithFrame_(NSMakeRect(*frame))
    card.setWantsLayer_(True)
    card.layer().setCornerRadius_(radius)
    card.layer().setBackgroundColor_(
        bolo_brand.native_color(extras["card"]).CGColor()
    )
    if border:
        card.layer().setBorderWidth_(0.5)
        card.layer().setBorderColor_(
            bolo_brand.native_color(pal["border"]).CGColor()
        )
    parent.addSubview_(card)
    return card


def _fill(parent, frame, dark, radius=8.0):
    """A borderless flat fill used for the selected transcript row."""
    from AppKit import NSMakeRect, NSView

    extras = palette_extras(dark=dark)
    view = NSView.alloc().initWithFrame_(NSMakeRect(*frame))
    view.setWantsLayer_(True)
    view.layer().setCornerRadius_(radius)
    view.layer().setBackgroundColor_(
        bolo_brand.native_color(extras["tint"]).CGColor()
    )
    parent.addSubview_(view)
    return view


def _hairline(parent, frame, dark):
    """A one-point hairline separator, card-local coordinates."""
    from AppKit import NSMakeRect, NSView

    extras = palette_extras(dark=dark)
    line = NSView.alloc().initWithFrame_(NSMakeRect(*frame))
    line.setWantsLayer_(True)
    line.layer().setBackgroundColor_(
        bolo_brand.native_color(extras["hairline"]).CGColor()
    )
    parent.addSubview_(line)
    return line


def _render_home(refs, body, controller):
    """Compact utilitarian Home: sans title, quiet usage row, one unified
    recent-dictations surface, and a learned-words footer."""
    import app_window

    from AppKit import NSMakeRect

    pal = refs["palette"]()
    dark = refs["dark"]
    dashboard = refs["dashboard"]
    height = body.bounds().size.height or HEIGHT
    x = MARGIN
    w = INNER_W

    # Title and instruction, header at y28 (flipped view: y grows down).
    y = 28
    _label(
        body, "Home", (x, y, w, 28),
        _body_font(TITLE_SIZE, medium=True), pal["text"],
    )
    y += 30
    _label(
        body,
        "Hold {0} to dictate in any app.".format(
            hotkey_title(dashboard["hotkey"])
        ),
        (x, y, w - 140, 18),
        _body_font(BODY_SIZE), pal["muted"],
    )
    # A small real keycap for the configured hotkey, upper right.
    cap = _card(body, (x + w - 120, y - 2, 120, 26), dark, radius=6.0)
    _label(
        cap, hotkey_title(dashboard["hotkey"]), (12, 5, 96, 16),
        _body_font(BODY_SIZE, medium=True), pal["text"],
    )
    y += 44

    # Quiet usage row: actual counts separated by labels, one line, then
    # the honest scope note on its own line so the two never overlap.
    dictations, words, scope = usage_counts(dashboard)
    half = 200
    _label(
        body, "{:,}".format(dictations), (x, y, half - 24, 24),
        _body_font(19, medium=True), pal["text"],
    )
    _label(
        body, "Dictations", (x, y + 25, half - 24, 15),
        _body_font(META_SIZE + 1), pal["muted"],
    )
    _label(
        body, "{:,}".format(words), (x + half, y, half, 24),
        _body_font(19, medium=True), pal["text"],
    )
    _label(
        body, "Words", (x + half, y + 25, half, 15),
        _body_font(META_SIZE + 1), pal["muted"],
    )
    y += 46
    _label(
        body, scope, (x, y, w, 15),
        _body_font(META_SIZE + 1), pal["muted"], truncate=True,
    )
    y += 30

    # Recent dictations: section header with a compact View all action,
    # then one unified neutral surface with hairline dividers, not cards.
    section_y = y
    _label(
        body, "Recent dictations", (x, section_y, w - 100, 20),
        _body_font(SECTION_SIZE, medium=True), pal["text"],
    )
    view_all = _button(
        controller, refs, body, "View all", x + w - 76, section_y - 3,
        76, 26, "dictationsNav:", soft=True,
    )
    view_all.setKeyEquivalent_("")  # keep Return for the window default
    y = section_y + 28

    latest = dashboard["history"][:5]
    if not latest:
        surface = _card(body, (x, y, w, 72), dark)
        _label(
            surface,
            "Nothing saved yet. Hold {0} and speak a sentence.".format(
                hotkey_title(dashboard["hotkey"])
            ),
            (16, 26, w - 32, 20),
            _body_font(BODY_SIZE), pal["muted"],
        )
        y += 88
    else:
        # The surface grows with the real row count and never paints past
        # the 650pt window: at least 5 rows fit with the footer below.
        surface_h = len(latest) * HOME_ROW_H + 8
        surface = _card(body, (x, y, w, surface_h), dark, radius=10.0)
        for index, entry in enumerate(latest):
            # Card-local coords: the unflipped card draws visual top at
            # H - y - h; stack rows downward from the surface top.
            row_top = 4 + index * HOME_ROW_H
            if index:
                _hairline(surface, (16, surface_h - row_top, w - 32, 1), dark)
            stamp = timestamp_label(entry["created_at_ms"])
            _label(
                surface, stamp, (16, surface_h - row_top - 18, 220, 14),
                _small_font(), pal["muted"],
            )
            _label(
                surface, entry["text"],
                (16, surface_h - row_top - 40, w - 130, 18),
                _body_font(BODY_SIZE), pal["text"], truncate=True,
            )
            _button(
                controller, refs, surface, "Copy",
                w - 92, surface_h - row_top - 44, 68, 30, "copyText:",
                tag=index, soft=True,
            )
        y += surface_h + 20

    # Learned-words footer pinned near the visual bottom with margins that
    # keep everything inside the 650pt window even with 5 rows.
    footer_y = max(y + 6, height - 92)
    _label(
        body, "Learned words", (x, footer_y, 110, 18),
        _body_font(BODY_SIZE, medium=True), pal["text"],
    )
    _label(
        body,
        "{0} learned".format(dashboard["learned_words_count"]),
        (x + 118, footer_y, 140, 18),
        _body_font(BODY_SIZE), pal["muted"],
    )
    _button(
        controller, refs, body, "Open learned words",
        x + w - 176, footer_y - 4, 176, 30, "openLearned:",
        soft=True,
    )


def _body_font(size, medium=False):
    from AppKit import NSFont, NSFontWeightMedium, NSFontWeightRegular

    return NSFont.systemFontOfSize_weight_(
        size, NSFontWeightMedium if medium else NSFontWeightRegular
    )


def _small_font():
    return _body_font(11)


def _render_dictations(refs, body, controller):
    """All retained entries reachable through a real scrolling list, with
    a readable detail pane, raw/clean toggle, and Copy."""
    import app_window

    from AppKit import (
        NSButton,
        NSMakeRect,
        NSScrollView,
        NSTextView,
    )

    pal = refs["palette"]()
    dark = refs["dark"]
    dashboard = refs["dashboard"]
    history = dashboard["history"]
    height = body.bounds().size.height or HEIGHT
    x = MARGIN
    w = INNER_W

    _label(
        body, "Dictations", (x, 28, w, 28),
        _body_font(TITLE_SIZE, medium=True), pal["text"],
    )

    list_width = 260
    list_x = x
    # Flipped body: y grows downward; the list fills below the header.
    list_y = 70
    list_h = height - list_y - 20

    if not history:
        card = _card(body, (x, list_y, w, 112), dark)
        _label(
            card, "No dictations saved yet.", (24, 70, w - 48, 20),
            _body_font(15, medium=True), pal["text"],
        )
        _label(
            card,
            "Hold {0} and speak a sentence. Saved dictations appear here.".format(
                hotkey_title(dashboard["hotkey"])
            ),
            (24, 20, w - 48, 40),
            _body_font(BODY_SIZE), pal["muted"], wrap=True,
        )
        return

    detail_x = list_x + list_width + 16
    detail_w = w - list_width - 16
    selected = refs.get("selected_index")
    if selected is None or not 0 <= selected < len(history):
        selected = 0
        refs["selected_index"] = 0
    entry = history[selected]

    # A real scroll view so every history entry stays reachable even when
    # ten rows exceed the visible height. The document view is flipped,
    # so rows stack downward and survive resizes.
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(list_x, list_y, list_width, list_h))
    scroll.setHasVerticalScroller_(True)
    scroll.setDrawsBackground_(False)
    scroll.setBorderType_(0)
    body.addSubview_(scroll)

    doc = app_window._APPKIT_CLASSES[0].alloc().initWithFrame_(
        NSMakeRect(0, 0, list_width, len(history) * HOME_ROW_H)
    )
    scroll.setDocumentView_(doc)
    for index, item in enumerate(history):
        row_top = index * HOME_ROW_H
        is_selected = index == selected
        row = _fill(doc, (0, row_top, list_width, HOME_ROW_H - 2), dark) if is_selected else None
        if row is None:
            row = _card(doc, (0, row_top, list_width, HOME_ROW_H - 2), dark, radius=0.0, border=False)
        _label(
            row, timestamp_label(item["created_at_ms"]),
            (16, 12, list_width - 32, 14), _small_font(), pal["muted"],
        )
        _label(
            row, item["text"], (16, 34, list_width - 32, 18),
            _body_font(BODY_SIZE), pal["text"], truncate=True,
        )
        # The row is the click target: a transparent borderless button
        # stretched over it routes the click without hiding the labels.
        select = _button(
            controller, refs, row, "", 0, 0, list_width, HOME_ROW_H - 2,
            "selectDictation:", tag=index,
        )
        select.setBordered_(False)
        select.setTitle_("")
        select.setWantsLayer_(True)
        select.layer().setBackgroundColor_(None)

    # Detail pane: timestamp, Copy, raw/clean toggle in one small toolbar
    # row, then the full transcript in its own scrollable text view.
    detail = _card(body, (detail_x, list_y, detail_w, list_h), dark)
    _label(
        detail, timestamp_label(entry["created_at_ms"]),
        (20, list_h - 30, detail_w - 220, 16), _small_font(), pal["muted"],
    )
    _button(
        controller, refs, detail, "Copy",
        detail_w - 92, list_h - 38, 72, 30, "copyText:", tag=selected, soft=True,
    )
    _button(
        controller, refs, detail,
        "Show raw" if not refs.get("show_raw") else "Show clean",
        detail_w - 176, list_h - 38, 76, 30, "toggleRaw:", soft=True,
    )
    shown = entry["raw"] if refs.get("show_raw") else entry["text"]
    detail_scroll, _ = _text_view(
        detail, shown,
        (16, 16, detail_w - 32, list_h - 64),
        _body_font(BODY_SIZE), pal["text"], dark,
    )
    refs["detail_scroll"] = detail_scroll
    refs["history_scroll"] = scroll
    # Keep the selected row visible: a full re-render recreates the
    # scroll view, so restore the scroll offset the selection was at and
    # scroll the selected row into view when it is out of sight.
    previous = refs.get("list_scroll_offset")
    if previous is not None:
        doc.scrollPoint_((0.0, previous))
    row_top = selected * HOME_ROW_H
    row_bottom = row_top + HOME_ROW_H
    visible_top = scroll.contentView().bounds().origin.y
    visible_bottom = visible_top + scroll.contentView().bounds().size.height
    if row_top < visible_top or row_bottom > visible_bottom:
        doc.scrollPoint_((0.0, max(0.0, row_top - 8)))


def _render_settings(refs, body, controller):
    """Compact settings: title, one tidy selector group, save/restart,
    status, and a separate Setup & vocabulary surface."""
    import app_window

    from AppKit import NSMakeRect, NSPopUpButton

    pal = refs["palette"]()
    dark = refs["dark"]
    dashboard = refs["dashboard"]
    options = settings_options(dashboard)
    selections = refs["selections"]
    x = MARGIN
    w = INNER_W

    _label(
        body, "Settings", (x, 28, w, 28),
        _body_font(TITLE_SIZE, medium=True), pal["text"],
    )
    _label(
        body,
        "Set Bolo up for the way you write.",
        (x, 58, w, 18),
        _body_font(BODY_SIZE), pal["muted"],
    )

    group_y = 88
    group_h = 3 * 56 + 8
    card = _card(body, (x, group_y, w, group_h), dark, radius=10.0)
    y = group_h - 50  # card-local: visual top row

    def row(title, description, y, options_list, popup_key, values_key):
        _label(
            card, title, (20, y + 24, 260, 18), _body_font(BODY_SIZE, medium=True),
            pal["text"],
        )
        _label(
            card, description, (20, y + 6, 280, 15), _body_font(META_SIZE + 1),
            pal["muted"],
        )
        popup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(w - 320 - 20, y + 10, 320, 28)
        )
        values = []
        for value, title_text in options_list:
            popup.addItemWithTitle_(title_text)
            values.append(value)
        index = select_value(values, selections.get(popup_key))
        popup.selectItemAtIndex_(index)
        # Keep the user's pick in refs: the selection must survive a tab
        # switch and a dashboard_update, never silently revert to the
        # running values while a save is pending restart.
        card.addSubview_(popup)
        refs[popup_key] = popup
        refs[values_key] = values
        return popup

    def separator(y):
        _hairline(card, (20, y, w - 40, 1), dark)

    # Rows stack downward from the visual top of the unflipped card.
    row(
        "Dictation key", "Hold while speaking.",
        y, options["hotkey"], "popup_hotkey", "hotkey_values",
    )
    separator(y - 4)
    row(
        "Microphone", "Applies to your next recording.",
        y - 56, options["microphone"], "popup_microphone", "mic_values",
    )
    separator(y - 60)
    row(
        "Cleanup", "Punctuation and formatting.",
        y - 112, options["cleanup"], "popup_cleanup", "cleanup_values",
    )

    # Buttons and status sit on the canvas below the group, with a fixed
    # autoresizing cluster so a taller window keeps them readable.
    buttons_y = group_y + group_h + 18
    _button(
        controller, refs, body, "Save changes",
        x, buttons_y, 130, 34, "saveSettings:", filled=True,
    )
    restart = _button(
        controller, refs, body, "Restart Bolo",
        x + 142, buttons_y, 130, 34, "restartBolo:", soft=True,
    )
    restart.setHidden_(not refs.get("restart_needed"))
    refs["restart_button"] = restart

    _label(
        body, "Dictation key and cleanup changes need a restart.",
        (x, buttons_y + 44, w, 18),
        _body_font(META_SIZE + 1), pal["muted"],
    )
    status_y = buttons_y + 72
    if refs.get("settings_status_text"):
        status = _label(
            body,
            refs["settings_status_text"],
            (x, status_y, w - 20, 32),
            _body_font(META_SIZE + 1),
            {
                True: pal["success"],
                False: pal["error"],
                None: pal["muted"],
            }.get(refs.get("settings_status_ok"), pal["muted"]),
            wrap=True,
        )
        refs["settings_status_label"] = status

    # A separate, tidy Setup & vocabulary surface beneath.
    setup_y = status_y + (44 if refs.get("settings_status_text") else 4)
    setup = _card(body, (x, setup_y, w, 88), dark, radius=10.0)
    _label(
        setup, "Setup & vocabulary", (20, 58, 300, 18),
        _body_font(BODY_SIZE, medium=True), pal["text"],
    )
    _label(
        setup,
        "Permissions, the API key, and learned words live in their own windows.",
        (20, 40, w - 40, 15),
        _body_font(META_SIZE + 1), pal["muted"],
    )
    _button(
        controller, refs, setup, "Open setup", 20, 8, 110, 28, "openSetup:",
        soft=True,
    )
    _button(
        controller, refs, setup, "Learned words", 140, 8, 130, 28,
        "openLearned:", soft=True,
    )


def render_body(refs):
    """Re-render only the body for the current tab; sidebar stays put."""
    from AppKit import NSMakeRect, NSView

    import app_window

    _clear_body(refs)
    content = refs.get("content")
    if content is None:
        # Telemetry-only callers (selection-retention tests) never built
        # a window: the selection bookkeeping above is already complete.
        return
    dark = refs["dark"]
    flipped_cls = app_window._APPKIT_CLASSES[0]
    bounds = content.bounds()
    body = flipped_cls.alloc().initWithFrame_(
        NSMakeRect(SIDEBAR_W, 0, bounds.size.width - SIDEBAR_W, bounds.size.height)
    )
    body.setWantsLayer_(True)
    body.layer().setBackgroundColor_(
        bolo_brand.native_color(palette_extras(dark=dark)["canvas"]).CGColor()
    )
    # The main area stretches right of the fixed sidebar. Page contents
    # keep their readable width when the window grows.
    from AppKit import NSViewWidthSizable, NSViewHeightSizable

    body.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    refs["body_view"] = body
    refs["copied_label"] = None
    refs["settings_status_label"] = None
    tab = refs["tab"]
    if tab == "home":
        _render_home(refs, body, refs["controller"])
    elif tab == "dictations":
        _render_dictations(refs, body, refs["controller"])
    else:
        _render_settings(refs, body, refs["controller"])
        refresh_popups(refs)
    content.addSubview_(body)


def refresh_popups(refs):
    """Re-select the Settings popups from refs after a body render.

    The renderer builds popups with the running values when no pending
    draft exists; this keeps the visible selection equal to the stored
    selections whenever a draft survives an update.
    """
    pairs = (
        ("popup_hotkey", "hotkey_values", "hotkey"),
        ("popup_microphone", "mic_values", "microphone"),
        ("popup_cleanup", "cleanup_values", "cleanup_mode"),
    )
    for popup_key, values_key, name in pairs:
        popup = refs.get(popup_key)
        values = refs.get(values_key) or []
        if popup is None or not values:
            continue
        wanted = (refs.get("selections") or {}).get(name)
        popup.selectItemAtIndex_(select_value(values, wanted))


def build_dashboard_ui(payload, preview=False, dark=None):
    """Build the dashboard window; imports stay local so tests import fast.

    940x650, resizable, warm paper on the content view so offscreen
    renders match light and dark onscreen. `preview` never orders the
    window front and never activates the app, so the preview script can
    cacheDisplay the hierarchy offscreen. Returns the standard ui dict
    (window/app/run_loop/accessibility/finish/close) plus dashboard refs
    so app_window.run_event_loop can dispatch dashboard messages here.
    """
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSMakeRect,
        NSRunLoop,
        NSDate,
        NSDefaultRunLoopMode,
        NSView,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
    )
    from AppKit import NSObject

    import app_window

    FlippedView, BrandMarkView, _, _ = app_window._appkit_classes_cached(
        NSView, NSObject
    )
    DashboardController = _controller_class_cached(NSObject)

    raw_dashboard = payload.get("dashboard")
    dashboard = validate_dashboard(
        raw_dashboard if isinstance(raw_dashboard, dict) else payload
    )
    if dashboard is None:
        dashboard = validate_dashboard({})

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.finishLaunching()
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, WIDTH, HEIGHT),
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskResizable
        | NSWindowStyleMaskMiniaturizable,
        2,
        False,
    )
    window.setTitle_(payload.get("title") or "Bolo")
    window.setReleasedWhenClosed_(False)
    window.setContentMinSize_((WIDTH, HEIGHT))
    if dark is not None:
        from AppKit import NSAppearance

        window.setAppearance_(
            NSAppearance.appearanceNamed_(
                "NSAppearanceNameDarkAqua" if dark else "NSAppearanceNameAqua"
            )
        )
    if dark is None:
        try:
            dark = bolo_brand.is_dark(window.effectiveAppearance())
        except Exception:
            dark = False
    pal = bolo_brand.palette(dark=dark)
    controller = DashboardController.alloc().init()
    window.setDelegate_(controller)

    refs = {
        "window": window,
        "dashboard": dashboard,
        "tab": "home",
        "selected_index": 0 if dashboard["history"] else None,
        "show_raw": False,
        "controller": controller,
        "selections": {
            "hotkey": dashboard["hotkey"],
            "microphone": dashboard["microphone"],
            "cleanup_mode": dashboard["cleanup_mode"],
        },
        "restart_needed": False,
        "pending_selections": None,
        "settings_status_text": "",
        "settings_status_ok": None,
        "dark": dark,
        "palette": lambda: bolo_brand.palette(dark=dark),
        "emitter": None,
        "content": None,
        "preview": bool(preview),
    }
    STATE["refs"] = refs

    content = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
    window.setContentView_(content)
    from AppKit import NSColor

    content.setWantsLayer_(True)
    content.layer().setBackgroundColor_(
        bolo_brand.native_color(pal["background"]).CGColor()
    )
    refs["content"] = content
    _build_sidebar(controller, refs, content, payload)
    render_body(refs)
    if preview:
        return {
            "window": window,
            "app": app,
            "run_loop": (NSRunLoop, NSDate, NSDefaultRunLoopMode),
            "accessibility": None,
            "finish": None,
            "close": None,
            "primary_button": None,
            "content": content,
            "refs": refs,
            "dashboard": True,
        }
    app.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)
    return {
        "window": window,
        "app": app,
        "run_loop": (NSRunLoop, NSDate, NSDefaultRunLoopMode),
        "accessibility": None,
        "finish": None,
        "close": None,
        "primary_button": None,
        "content": content,
        "refs": refs,
        "dashboard": True,
    }


def select_tab(refs, tab):
    """Switch tabs in place and re-render the body; header stays put."""
    if tab not in TABS:
        return
    refs["tab"] = tab
    restyle_nav(refs)
    render_body(refs)


def apply_dashboard_activate(refs, live=True):
    """Bring the dashboard window front on menu reopen (type activate).

    Only the live window activates the app and makes the key front; the
    preview path never activates anything. Returns True when applied.
    """
    if not isinstance(refs, dict) or refs.get("window") is None:
        return False
    if refs.get("preview"):
        return False
    if not live:
        return False
    try:
        app = refs["window"].windowController() and None
    except Exception:
        app = None
    from AppKit import NSApplication

    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    refs["window"].makeKeyAndOrderFront_(None)
    return True
