#!/usr/bin/env python3
"""Native AppKit dashboard: Home, Dictations, and Settings in one window.

The Rust runtime spawns this helper with a mode "dashboard" payload and
keeps talking over stdin: `dashboard_update` lines refresh facts and
history, `dashboard_action_reply` lines answer the window's requests.
The window prints one `dashboard_action` JSON line per user action on
stdout. Everything renders with AppKit in the shared warm-paper identity
from bolo_brand: system typography for body text, Georgia for the
wordmark, and the moss voice-waveform accent for dictation identity.
Counts and transcripts come only from the payload's retained history
(the last N saved dictations), never from files this window reads, and
no file path, key, or debug line is ever shown in the UI.

Layout notes: the sidebar is a FlippedView (y grows downward) so nav
rows and the footer never invert on resize. The de-boxed body lays
content directly on the canvas with 0.5pt hairline separators and
small-caps section labels instead of bordered cards: the only fills
are the sidebar's selected-row tint, the primary Save button, and
leading accent bars for selection. Page labels are 11pt small caps;
stats use monospaced tabular digits; body stays 13pt; metadata 11/12.
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
PAGE_LABEL_SIZE = 11
BODY_SIZE = 13
META_SIZE = 11
HERO_SIZE = 38
STAT_SIZE = 17
TRACKING = 0.8
HOME_ROW_H = 58  # dictations list rows (scroll math derives from this)
RECENT_ROW_H = 36  # Home recent rows: one dense line plus hover affordance
RECENT_MAX = 8  # recent dictations shown on Home before "View all"
CHART_W = 260
CHART_H = 64

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


def _daily_words(history, days=7, now_ms=None):
    """(letters, totals) for the last `days` local days, oldest first.

    Totals are the word counts of the retained history entries bucketed
    by each entry's real local day: days without saved entries total
    zero, entries outside the window are ignored, and nothing is ever
    extrapolated. Letters are the weekday initials of each day.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    day_specs = []
    for offset in range(days):
        stamp = time.localtime((now_ms - offset * 86400000) / 1000.0)
        day_specs.append((time.strftime("%Y-%m-%d", stamp), time.strftime("%a", stamp)))
    day_specs.reverse()
    totals = {date: 0 for date, _ in day_specs}
    for entry in history:
        created = entry.get("created_at_ms")
        if not isinstance(created, int) or isinstance(created, bool) or created <= 0:
            continue
        key = time.strftime("%Y-%m-%d", time.localtime(created / 1000.0))
        if key in totals:
            totals[key] += len(entry["text"].split())
    letters = [letter for _, letter in day_specs]
    return letters, [totals[date] for date, _ in day_specs]


def palette_extras(dark=False):
    """Dashboard-local surfaces layered on the shared brand palette.

    The de-boxed dashboard draws content straight onto the canvas, so
    these are only the quiet structural tones: the canvas itself, the
    warm stone sidebar, the 0.5pt hairline separators, and the gentle
    clay tint for the sidebar's selected nav row.
    """
    if dark:
        return {
            # Neutral charcoal canvas; the sidebar goes a step darker and
            # warm-hued so the two surfaces differ in value and in hue.
            # The nav tint is the light warm clay with ink labels: the
            # same selected-row treatment as light mode (a light pill on
            # a dark desk), A/B tested as the crispest figure-ground.
            "canvas": (0.133, 0.133, 0.141),
            "sidebar": (0.100, 0.096, 0.086),
            "hairline": (0.271, 0.271, 0.279),
            "tint": (0.847, 0.788, 0.694),
        }
    return {
        # Neutral ivory canvas, slightly darker warm stone sidebar. The
        # nav tint is a clear clay peach so the selected row reads at a
        # glance, not a whisper.
        "canvas": (0.973, 0.969, 0.957),
        "sidebar": (0.933, 0.929, 0.909),
        "hairline": (0.886, 0.882, 0.866),
        "tint": (0.933, 0.867, 0.800),
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


def _recorded_label(dashboard):
    """Human "2h 30m" for the cumulative recording time; None if absent.

    Comes only from the runtime's usage counters; never estimated.
    """
    usage = dashboard.get("usage")
    if not isinstance(usage, dict):
        return None
    ms = usage.get("recording_ms")
    if not isinstance(ms, int) or isinstance(ms, bool) or ms <= 0:
        return None
    minutes = ms // 60000
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "{0}h {1}m".format(hours, minutes)
    return "{0}m".format(minutes)


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
    # "Oct 2" (no zero padding): the macOS date idiom, not "Oct 02".
    return time.strftime("%b ", stamp) + str(stamp.tm_mday) + time.strftime(
        " at %H:%M", stamp
    )


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

        def recentDictation_(self, sender):
            """A Home recent row click: select that entry in Dictations."""
            refs = self._refs()
            try:
                index = int(sender.tag()) if sender is not None else 0
            except (TypeError, ValueError):
                return
            history = (refs.get("dashboard") or {}).get("history") or []
            if not 0 <= index < len(history):
                return
            refs["selected_index"] = index
            refs.pop("list_scroll_offset", None)
            refs["tab"] = "dictations"
            restyle_nav(refs)
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
    """Recolor the sidebar nav rows, icon tints, and count badges."""
    pal = refs["palette"]()
    extras = palette_extras(dark=refs["dark"])
    dashboard = refs.get("dashboard") or {}
    saved = dashboard.get("saved_dictations") or 0
    for name in TABS:
        button = refs.get("nav_" + name)
        if button is None:
            continue
        active = refs["tab"] == name
        count = str(saved) if name == "dictations" and saved else None
        # The dark-mode pill is light clay, so its labels go ink, the
        # same pairing as the light-mode pill; inactive rows keep the
        # palette's normal text and muted tones.
        if active:
            tone = bolo_brand.INK if refs["dark"] else pal["text"]
        else:
            tone = pal["muted"]
        button.setAttributedTitle_(_nav_title(TAB_TITLES[name], count, tone))
        button.layer().setBackgroundColor_(
            bolo_brand.native_color(extras["tint"]).CGColor() if active else None
        )
        icon = refs.get("nav_icon_" + name)
        if icon is not None:
            icon.setContentTintColor_(bolo_brand.native_color(tone))


def _nav_title(text, count, color):
    """A nav row title: the page name plus its right-aligned count."""
    from AppKit import (
        NSAttributedString,
        NSMutableAttributedString,
        NSMutableParagraphStyle,
        NSTextTab,
        NSTextAlignmentLeft,
        NSTextAlignmentRight,
    )

    paragraph = NSMutableParagraphStyle.alloc().init()
    paragraph.setAlignment_(NSTextAlignmentLeft)
    # Nav rows: icon sits at the left pad, the label clears it, and a
    # right tab stop pins the count to the row's trailing edge.
    paragraph.setFirstLineHeadIndent_(40)
    paragraph.setTabStops_([
        NSTextTab.alloc().initWithTextAlignment_location_options_(
            NSTextAlignmentRight, 156, None
        ),
    ])
    title = NSMutableAttributedString.alloc().initWithString_attributes_(
        text,
        {"NSFont": _body_font(13, medium=True),
         "NSColor": bolo_brand.native_color(color),
         "NSParagraphStyle": paragraph},
    )
    if count:
        title.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                "\t" + count,
                {"NSFont": _mono_font(11),
                 "NSColor": bolo_brand.native_color(color),
                 "NSParagraphStyle": paragraph},
            )
        )
    return title


def _styled_title(text, color, size=13, medium=False, align="center", indent=0):
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
    if indent:
        paragraph.setFirstLineHeadIndent_(indent)
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
    """Refresh the sidebar readiness dot, word, and nav count badges."""
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
    restyle_nav(refs)


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

    setup = _text_button(
        controller, refs, sidebar, "Open setup",
        20, HEIGHT - 130, SIDEBAR_W - 40, 24, "openSetup:", size=12,
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
    # The pill keeps 8pt margins on both sidebar edges: symmetric insets.
    button.setFrame_(NSMakeRect(x, y, SIDEBAR_W - 16, 36))
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


def _label(parent, text, frame, font, color, wrap=False, truncate=False,
           align="left"):
    from AppKit import (
        NSTextField,
        NSMakeRect,
        NSTextAlignmentLeft,
        NSTextAlignmentRight,
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
    if align == "right":
        field.setAlignment_(NSTextAlignmentRight)
    if truncate:
        field.setUsesSingleLineMode_(True)
        field.setLineBreakMode_(NSLineBreakByTruncatingTail)
    elif wrap:
        field.setLineBreakMode_(NSLineBreakByWordWrapping)
    parent.addSubview_(field)
    return field


def _text_view(parent, text, frame, font, color, dark):
    """A scrollable, selectable, non-editable text view for long text.

    The container inset and line padding are zeroed on the leading edge
    so the transcript text sits exactly on the pane's left edge, aligned
    with the metadata line above it.
    """
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
    view.setTextContainerInset_((0, 8))
    view.textContainer().setLineFragmentPadding_(0)
    view.setEditable_(False)
    view.setSelectable_(True)
    view.setVerticallyResizable_(True)
    view.setHorizontallyResizable_(False)
    scroll_view.setDocumentView_(view)
    parent.addSubview_(scroll_view)
    return scroll_view, view


def _mono_font(size, medium=False):
    """Tabular-digits system font so every stat aligns in columns."""
    from AppKit import NSFont, NSFontWeightMedium, NSFontWeightRegular

    return NSFont.monospacedDigitSystemFontOfSize_weight_(
        size, NSFontWeightMedium if medium else NSFontWeightRegular
    )


def _small_caps(parent, text, frame, color, size=PAGE_LABEL_SIZE, align="left"):
    """A small-caps section label: medium weight, uppercase, tracked."""
    from AppKit import (
        NSAttributedString,
        NSMutableParagraphStyle,
        NSMakeRect,
        NSTextField,
        NSTextAlignmentLeft,
        NSTextAlignmentRight,
    )

    paragraph = NSMutableParagraphStyle.alloc().init()
    paragraph.setAlignment_(
        NSTextAlignmentRight if align == "right" else NSTextAlignmentLeft
    )
    string = NSAttributedString.alloc().initWithString_attributes_(
        text.upper(),
        {"NSFont": _body_font(size, medium=True),
         "NSColor": bolo_brand.native_color(color),
         "NSKernAttributeName": TRACKING,
         "NSParagraphStyle": paragraph},
    )
    field = NSTextField.labelWithString_(text.upper())
    field.setFrame_(NSMakeRect(*frame))
    field.setAttributedStringValue_(string)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    parent.addSubview_(field)
    return field


def _filled_button(controller, refs, parent, title, x, y, w, h, action):
    """The one primary action: a flat clay fill on the native button."""
    from AppKit import NSButton, NSMakeRect

    pal = refs["palette"]()
    button = NSButton.buttonWithTitle_target_action_(
        title, refs["controller"], action
    )
    button.setBezelStyle_(6)  # keeps native press/focus behavior
    button.setBordered_(False)
    button.setWantsLayer_(True)
    button.layer().setCornerRadius_(7.0)
    button.layer().setBackgroundColor_(
        bolo_brand.native_color(pal["button"]).CGColor()
    )
    button.setFrame_(NSMakeRect(x, y, w, h))
    button.setAttributedTitle_(_styled_title(
        title, pal["button_text"], size=13, medium=True,
    ))
    parent.addSubview_(button)
    return button


def _text_button(controller, refs, parent, title, x, y, w, h, action,
                 tag=0, color=None, size=BODY_SIZE):
    """A quiet borderless text action in the clay accent."""
    from AppKit import NSButton, NSMakeRect

    pal = refs["palette"]()
    button = NSButton.buttonWithTitle_target_action_(
        title, refs["controller"], action
    )
    button.setBezelStyle_(6)
    button.setBordered_(False)
    button.setWantsLayer_(True)
    button.layer().setBackgroundColor_(None)
    button.setFrame_(NSMakeRect(x, y, w, h))
    button.setTag_(tag)
    button.setAttributedTitle_(_styled_title(
        title, color or pal["accent"], size=size, medium=True, align="left",
    ))
    parent.addSubview_(button)
    return button


def _overlay_button(controller, refs, parent, action, x, y, w, h, tag=0):
    """A transparent full-area button: the whole row is the click target."""
    from AppKit import NSButton, NSMakeRect

    button = NSButton.buttonWithTitle_target_action_(
        "", refs["controller"], action
    )
    button.setBezelStyle_(6)
    button.setBordered_(False)
    button.setWantsLayer_(True)
    button.layer().setBackgroundColor_(None)
    button.setFrame_(NSMakeRect(x, y, w, h))
    button.setTag_(tag)
    button.setTitle_("")
    parent.addSubview_(button)
    return button


def _chevron(parent, x, y, color, size=12):
    """A quiet trailing chevron: the disclosure affordance for row links."""
    from AppKit import (
        NSImage,
        NSImageView,
        NSMakeRect,
        NSImageScaleProportionallyUpOrDown,
    )

    icon = NSImageView.alloc().initWithFrame_(NSMakeRect(x, y, size, size))
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        "chevron.right", None
    )
    if image is not None:
        image.setSize_((size, size))
        icon.setImage_(image)
        icon.setContentTintColor_(bolo_brand.native_color(color))
        icon.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        icon.setEditable_(False)
        icon.setAnimates_(False)
        parent.addSubview_(icon)
    return icon


def _copy_cluster(controller, refs, parent, x, y, action, tag=0, color=None,
                  tip="Copy"):
    """An icon-only copy affordance: a tinted SF Symbol under a hit target."""
    import app_window

    from AppKit import (
        NSImage,
        NSImageView,
        NSMakeRect,
        NSImageScaleProportionallyUpOrDown,
    )

    pal = refs["palette"]()
    FlippedView = app_window._APPKIT_CLASSES[0]
    cluster = FlippedView.alloc().initWithFrame_(NSMakeRect(x, y, 28, 26))
    icon = NSImageView.alloc().initWithFrame_(NSMakeRect(6, 5, 16, 16))
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
        "doc.on.doc", tip
    )
    if image is not None:
        image.setSize_((16, 16))
        icon.setImage_(image)
        icon.setContentTintColor_(bolo_brand.native_color(color or pal["muted"]))
        icon.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        icon.setEditable_(False)
        icon.setAnimates_(False)
        cluster.addSubview_(icon)
    else:
        # Symbol art unavailable on this OS: a tiny text fallback.
        _label(cluster, "Copy", (0, 7, 28, 14), _small_font(), pal["muted"])
    button = _overlay_button(
        controller, refs, cluster, action, 0, 0, 28, 26, tag=tag
    )
    button.setToolTip_(tip)
    parent.addSubview_(cluster)
    return cluster


_CLASSES = {"tuple": None}


def _dashboard_classes_cached():
    """Hover rows, the waveform glyph, and the chart view.

    One ObjC registration per process: PyObjC raises when a class is
    redefined in the same runtime, so repeated builds reuse the cache.
    """
    cached = _CLASSES.get("tuple")
    if cached is not None:
        return cached
    import app_window

    from AppKit import NSObject, NSView

    FlippedView = app_window._appkit_classes_cached(NSView, NSObject)[0]

    class HoverRowView(FlippedView):
        """A row that reveals its copy affordance while the pointer stays
        inside it, and hides it again when the pointer leaves."""

        def setRevealView_(self, view):
            self._reveal = view

        def mouseEntered_(self, event):
            reveal = getattr(self, "_reveal", None)
            if reveal is not None:
                reveal.setHidden_(False)

        def mouseExited_(self, event):
            reveal = getattr(self, "_reveal", None)
            if reveal is not None:
                reveal.setHidden_(True)

        def updateTrackingAreas(self):
            from AppKit import (
                NSTrackingActiveAlways,
                NSTrackingArea,
                NSTrackingMouseEnteredAndExited,
            )

            for area in list(self.trackingAreas()):
                self.removeTrackingArea_(area)
            self.addTrackingArea_(
                NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                    self.bounds(),
                    NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways,
                    self,
                    None,
                )
            )

    class WaveformView(FlippedView):
        """The moss voice-waveform accent, drawn from bolo_brand."""

        def configureWithColor_(self, rgb):
            self._rgb = rgb
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            bounds = self.bounds()
            bolo_brand.draw_waveform(
                0, 0, bounds.size.width, bounds.size.height,
                getattr(self, "_rgb", bolo_brand.MOSS),
            )

    class ChartView(FlippedView):
        """Seven real words-per-day bars on a hairline baseline."""

        def configureWithSpec_(self, spec):
            self._spec = spec
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            import AppKit

            spec = getattr(self, "_spec", None)
            if not spec:
                return
            values = spec["values"]
            letters = spec["letters"]
            bounds = self.bounds()
            width = bounds.size.width
            height = bounds.size.height
            count = len(values)
            bar_w, gap = 14.0, 18.0
            pad = (width - (count * bar_w + (count - 1) * gap)) / 2.0
            base_y = height - 14.0
            peak = max(1, max(values or [0]))
            max_index = values.index(max(values))
            regular = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                10.0, AppKit.NSFontWeightRegular
            )
            medium = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                10.0, AppKit.NSFontWeightMedium
            )
            for index, value in enumerate(values):
                bar_x = pad + index * (bar_w + gap)
                if value > 0:
                    # A nonzero day always earns at least a 3pt mark so a
                    # quiet day still reads as data, never a rendering bug.
                    bar_h = max(3.0, (base_y - 12.0) * value / float(peak))
                    bar = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        AppKit.NSMakeRect(bar_x, base_y - bar_h, bar_w, bar_h),
                        2.0, 2.0,
                    )
                    bolo_brand.native_color(spec["bar"]).setFill()
                    bar.fill()
                    if bar_h < 8.0:
                        # A near-invisible bar carries its own value so a
                        # quiet day still reads as data, not a glitch.
                        quiet_text = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                            "{:,}".format(value),
                            {"NSFont": AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                                9.0, AppKit.NSFontWeightMedium
                             ),
                             "NSColor": bolo_brand.native_color(spec["muted"])},
                        )
                        quiet_text.drawAtPoint_((
                            bar_x + (bar_w - quiet_text.size().width) / 2.0,
                            base_y - bar_h - 12.0,
                        ))
                else:
                    # A quiet dot on the baseline marks a day with no
                    # saved dictations: zero reads as data, not a bug.
                    dot = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        AppKit.NSMakeRect(bar_x + bar_w / 2 - 1, base_y - 1, 2, 2),
                        1.0, 1.0,
                    )
                    bolo_brand.native_color(spec["hairline"]).setFill()
                    dot.fill()
                letter = letters[index] if index < len(letters) else ""
                today = index == count - 1
                text = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    letter,
                    {"NSFont": medium if today else regular,
                     "NSColor": bolo_brand.native_color(
                         spec["text"] if today else spec["muted"]
                     )},
                )
                text.drawAtPoint_((
                    bar_x + (bar_w - text.size().width) / 2.0,
                    height - 12.0,
                ))
            # The peak bar carries its value: max context without clutter.
            peak_text = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                "{:,}".format(values[max_index]),
                {"NSFont": AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                    9.0, AppKit.NSFontWeightMedium
                 ),
                 "NSColor": bolo_brand.native_color(spec["muted"])},
            )
            peak_x = pad + max_index * (bar_w + gap)
            peak_text.drawAtPoint_((
                peak_x + (bar_w - peak_text.size().width) / 2.0, 2.0,
            ))
            baseline = AppKit.NSBezierPath.bezierPath()
            # The baseline spans exactly the bar block, not the full
            # width: the axis ends where the data ends. It uses the muted
            # ink so the axis is actually visible at 0.5pt.
            baseline.moveToPoint_((pad, base_y))
            baseline.lineToPoint_((width - pad, base_y))
            bolo_brand.native_color(spec["muted"]).setStroke()
            baseline.setLineWidth_(0.5)
            baseline.stroke()

    _CLASSES["tuple"] = (HoverRowView, WaveformView, ChartView)
    return _CLASSES["tuple"]


def _hairline(parent, frame, dark):
    """A one-point hairline separator, parent-local coordinates."""
    from AppKit import NSMakeRect, NSView

    extras = palette_extras(dark=dark)
    line = NSView.alloc().initWithFrame_(NSMakeRect(*frame))
    line.setWantsLayer_(True)
    line.layer().setBackgroundColor_(
        bolo_brand.native_color(extras["hairline"]).CGColor()
    )
    parent.addSubview_(line)
    return line


def _accent_bar(parent, frame, color):
    """The 3pt selection bar on a row's leading edge: selection without
    a filled box."""
    from AppKit import NSMakeRect, NSView

    bar = NSView.alloc().initWithFrame_(NSMakeRect(*frame))
    bar.setWantsLayer_(True)
    bar.layer().setCornerRadius_(1.5)
    bar.layer().setBackgroundColor_(
        bolo_brand.native_color(color).CGColor()
    )
    parent.addSubview_(bar)
    return bar


def _render_home(refs, body, controller):
    """Home as a reading page: hero word count, the real 7-day rhythm,
    the recent list as the main body, and a compact learned-words row."""
    import app_window

    from AppKit import NSMakeRect, NSTextField

    pal = refs["palette"]()
    dark = refs["dark"]
    dashboard = refs["dashboard"]
    x = MARGIN
    w = INNER_W
    HoverRowView, WaveformView, ChartView = _dashboard_classes_cached()

    # Page label left; the dictation hint with its voice accent right.
    _small_caps(body, "Home", (x, 26, 200, 16), pal["muted"])
    hint_text = "Hold {0} to dictate in any app.".format(
        hotkey_title(dashboard["hotkey"])
    )
    hint_attr = _styled_title(hint_text, pal["muted"], size=12, align="left")
    hint_w = hint_attr.size().width
    hint = NSTextField.labelWithString_(hint_text)
    hint.setFrame_(NSMakeRect(x + w - hint_w - 4, 25, hint_w + 4, 17))
    hint.setAttributedStringValue_(hint_attr)
    hint.setBezeled_(False)
    hint.setDrawsBackground_(False)
    hint.setEditable_(False)
    hint.setSelectable_(False)
    body.addSubview_(hint)
    wave = WaveformView.alloc().initWithFrame_(
        NSMakeRect(x + w - hint_w - 34, 27, 26, 12)
    )
    wave.configureWithColor_(pal["success"])
    body.addSubview_(wave)

    # Hero stats: the word count anchors the page; dictations and the
    # recorded time sit bottom-aligned beside it; the honest scope line
    # runs under all three. All tabular digits.
    dictations, words, scope = usage_counts(dashboard)
    recorded = _recorded_label(dashboard)
    _label(
        body, "{:,}".format(words), (x, 48, 220, 42),
        _mono_font(HERO_SIZE, medium=True), pal["text"],
    )
    _small_caps(body, "Words", (x, 94, 140, 14), pal["muted"])
    _label(
        body, "{:,}".format(dictations), (x + 168, 66, 120, 24),
        _mono_font(STAT_SIZE, medium=True), pal["text"],
    )
    _small_caps(body, "Dictations", (x + 168, 94, 140, 14), pal["muted"])
    if recorded:
        _label(
            body, recorded, (x + 336, 66, 140, 24),
            _mono_font(STAT_SIZE, medium=True), pal["text"],
        )
        _small_caps(body, "Recorded", (x + 336, 94, 140, 14), pal["muted"])
    _label(
        body, scope, (x, 118, 380, 15),
        _body_font(META_SIZE + 1), pal["muted"], truncate=True,
    )

    # The real 7-day words-per-day rhythm from retained history only:
    # omitted entirely when the history carries no in-window data. The
    # header right-aligns with the hint cluster above it, one shared
    # right edge for the whole top-right column.
    letters, values = _daily_words(dashboard["history"])
    if sum(values) > 0:
        chart_x = x + w - CHART_W
        _small_caps(
            body, "Saved words · last 7 days",
            (chart_x, 48, CHART_W, 12), pal["muted"], size=10, align="right",
        )
        chart = ChartView.alloc().initWithFrame_(
            NSMakeRect(chart_x, 66, CHART_W, CHART_H)
        )
        chart.configureWithSpec_({
            "values": values,
            "letters": letters,
            "bar": pal["success"],
            "hairline": palette_extras(dark=dark)["hairline"],
            "muted": pal["muted"],
            "text": pal["text"],
        })
        body.addSubview_(chart)

    # Recent dictations: the page's main body, one dense row per entry.
    body_h = body.bounds().size.height or HEIGHT
    y = 142
    _small_caps(body, "Recent dictations", (x, y, 300, 14), pal["muted"])
    all_history = dashboard["history"]
    latest = all_history[:RECENT_MAX]
    if len(all_history) > RECENT_MAX:
        view_all = _text_button(
            controller, refs, body, "View all", x + w - 80, y - 3, 80, 22,
            "dictationsNav:",
        )
        view_all.setKeyEquivalent_("")  # keep Return for the window default
    y += 18
    _hairline(body, (x, y, w, 1), dark)
    y += 1

    # The learned-words footer pins to the body's bottom edge so the
    # page reads full-height; the recent rows stretch to fill the space
    # between the section hairline and the footer, capped so a single
    # entry never becomes a giant band.
    footer_y = body_h - 46
    if not latest:
        # The same composed, centered empty state as the Dictations
        # page: voice accent, headline, instruction, centered both ways.
        avail = footer_y - y
        block_y = y + max(16, (avail - 72) // 2)
        center_x = x + w // 2
        empty_wave = WaveformView.alloc().initWithFrame_(
            NSMakeRect(center_x - 22, block_y, 44, 20)
        )
        empty_wave.configureWithColor_(pal["success"])
        body.addSubview_(empty_wave)
        _label(
            body, "Nothing saved yet.", (center_x - 200, block_y + 34, 400, 20),
            _body_font(15, medium=True), pal["text"], align="center",
        )
        _label(
            body,
            "Hold {0} and speak a sentence.".format(
                hotkey_title(dashboard["hotkey"])
            ),
            (center_x - 240, block_y + 60, 480, 18),
            _body_font(BODY_SIZE), pal["muted"], align="center",
        )
    else:
        count = len(latest)
        # Rows keep a dense Raycast-like rhythm (36-44pt); any spare
        # height stays as page-end whitespace above the pinned footer
        # rather than ballooning the rows.
        row_h = max(RECENT_ROW_H, min(44, (footer_y - y) // count))
        for index, entry in enumerate(latest):
            row_y = y + index * row_h
            row = HoverRowView.alloc().initWithFrame_(
                NSMakeRect(x, row_y, w, row_h)
            )
            body.addSubview_(row)
            if index:
                _hairline(row, (0, 0, w, 1), dark)
            # Row content centers vertically in the stretched band.
            content_y = (row_h - 18) // 2
            _label(
                row, timestamp_label(entry["created_at_ms"]),
                (0, content_y + 1, 100, 14), _small_font(), pal["muted"],
            )
            _label(
                row, entry["text"], (104, content_y, w - 232, 18),
                _body_font(BODY_SIZE), pal["text"], truncate=True,
            )
            _label(
                row, "{0} words".format(len(entry["text"].split())),
                (w - 128, content_y + 1, 88, 14), _small_font(), pal["muted"],
                align="right",
            )
            # The row is the click target; Copy appears only on hover.
            _overlay_button(
                controller, refs, row, "recentDictation:",
                0, 0, w - 32, row_h, tag=index,
            )
            copy_cluster = _copy_cluster(
                controller, refs, row, w - 30, (row_h - 26) // 2,
                "copyText:", tag=index,
            )
            copy_cluster.setHidden_(True)
            row.setRevealView_(copy_cluster)

    # Learned words: one compact footer row pinned to the body's bottom,
    # whole-row clickable into the vocabulary window.
    _hairline(body, (x, footer_y, w, 1), dark)
    _small_caps(
        body, "Learned words", (x, footer_y + 11, 140, 14), pal["muted"],
    )
    _label(
        body, "{:,}".format(dashboard["learned_words_count"]),
        (x + w - 72, footer_y + 10, 40, 15),
        _mono_font(META_SIZE + 1), pal["muted"], align="right",
    )
    _chevron(body, x + w - 26, footer_y + 12, pal["muted"])
    _overlay_button(
        controller, refs, body, "openLearned:",
        x, footer_y + 1, w, RECENT_ROW_H,
    )


def _body_font(size, medium=False):
    from AppKit import NSFont, NSFontWeightMedium, NSFontWeightRegular

    return NSFont.systemFontOfSize_weight_(
        size, NSFontWeightMedium if medium else NSFontWeightRegular
    )


def _small_font():
    return _body_font(11)


def _render_dictations(refs, body, controller):
    """Master-detail on one surface: a scrolling list beside the full
    transcript, a hairline between them, Copy and Raw in the toolbar."""
    import app_window

    from AppKit import NSMakeRect, NSScrollView

    pal = refs["palette"]()
    dark = refs["dark"]
    dashboard = refs["dashboard"]
    history = dashboard["history"]
    height = body.bounds().size.height or HEIGHT
    x = MARGIN
    w = INNER_W

    _small_caps(body, "Dictations", (x + 14, 26, 200, 16), pal["muted"])

    list_width = 260
    list_x = x
    # Flipped body: y grows downward; the list fills below the header.
    list_y = 64
    list_h = height - list_y - 24

    if not history:
        # A composed, centered empty state: the voice accent, one
        # headline, and the real instruction for the first dictation.
        WaveformView = _dashboard_classes_cached()[1]
        center_x = x + w // 2
        block_y = list_y + max(20, (list_h - 116) // 2)
        wave = WaveformView.alloc().initWithFrame_(
            NSMakeRect(center_x - 22, block_y, 44, 20)
        )
        wave.configureWithColor_(pal["success"])
        body.addSubview_(wave)
        _label(
            body, "No dictations saved yet.",
            (center_x - 200, block_y + 38, 400, 20),
            _body_font(15, medium=True), pal["text"], align="center",
        )
        _label(
            body,
            "Hold {0} and speak a sentence. Saved dictations appear here.".format(
                hotkey_title(dashboard["hotkey"])
            ),
            (center_x - 240, block_y + 64, 480, 18),
            _body_font(BODY_SIZE), pal["muted"], align="center",
        )
        return

    detail_x = x + list_width + 28
    detail_w = x + w - detail_x
    selected = refs.get("selected_index")
    if selected is None or not 0 <= selected < len(history):
        selected = 0
        refs["selected_index"] = 0
    entry = history[selected]

    # A real scroll view so every history entry stays reachable even when
    # ten rows exceed the visible height. Rows sit straight on the
    # canvas, separated by hairlines, no box around them.
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(list_x, list_y, list_width, list_h)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setDrawsBackground_(False)
    scroll.setBorderType_(0)
    body.addSubview_(scroll)

    FlippedView = app_window._APPKIT_CLASSES[0]
    doc = FlippedView.alloc().initWithFrame_(
        NSMakeRect(0, 0, list_width, len(history) * HOME_ROW_H)
    )
    scroll.setDocumentView_(doc)
    for index, item in enumerate(history):
        row_top = index * HOME_ROW_H
        is_selected = index == selected
        row = FlippedView.alloc().initWithFrame_(
            NSMakeRect(0, row_top, list_width, HOME_ROW_H)
        )
        doc.addSubview_(row)
        if index:
            _hairline(row, (16, 0, list_width - 32, 1), dark)
        if is_selected:
            # Selection reads as a clay leading bar plus medium-weight
            # text, not a filled box.
            _accent_bar(row, (0, 10, 3, 42), pal["accent"])
        _label(
            row, timestamp_label(item["created_at_ms"]),
            (14, 10, list_width - 26, 14), _small_font(), pal["muted"],
        )
        _label(
            row, item["text"], (14, 30, list_width - 26, 18),
            _body_font(BODY_SIZE, medium=is_selected), pal["text"],
            truncate=True,
        )
        # The row is the click target: a transparent button stretched
        # over it routes the click without hiding the labels.
        _overlay_button(
            controller, refs, row, "selectDictation:",
            0, 0, list_width, HOME_ROW_H, tag=index,
        )

    # The hairline seam between the list and the detail column.
    _hairline(body, (list_x + list_width + 12, list_y, 1, list_h), dark)

    # Detail toolbar: real metadata left, the quiet actions right. The
    # cluster sits on the first list row's timestamp line so the two
    # columns share one optical top line.
    meta = "{0} · {1} words".format(
        timestamp_label(entry["created_at_ms"]), len(entry["text"].split())
    )
    if entry["edited_after_insert"]:
        meta += " · edited"
    _label(
        body, meta, (detail_x, list_y + 9, 240, 16),
        _small_font(), pal["muted"], truncate=True,
    )
    _text_button(
        controller, refs, body,
        "Show raw" if not refs.get("show_raw") else "Show clean",
        detail_x + detail_w - 136, list_y + 4, 92, 24, "toggleRaw:", size=12,
    )
    _copy_cluster(
        controller, refs, body, detail_x + detail_w - 30, list_y + 3,
        "copyText:", tag=selected, color=pal["text"],
    )
    shown = entry["raw"] if refs.get("show_raw") else entry["text"]
    detail_scroll, _ = _text_view(
        body, shown,
        (detail_x, list_y + 36, detail_w, list_h - 36),
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
    """De-boxed settings: hairline selector rows straight on the canvas,
    the one primary Save, and disclosure rows for setup and vocabulary."""
    import app_window

    from AppKit import NSMakeRect, NSPopUpButton

    pal = refs["palette"]()
    dark = refs["dark"]
    dashboard = refs["dashboard"]
    options = settings_options(dashboard)
    selections = refs["selections"]
    x = MARGIN
    w = INNER_W

    _small_caps(body, "Settings", (x, 26, 200, 16), pal["muted"])
    _label(
        body,
        "Set Bolo up for the way you write.",
        (x, 50, w, 18),
        _body_font(BODY_SIZE), pal["muted"],
    )

    def popup_row(title, description, top, options_list, popup_key, values_key):
        _label(
            body, title, (x, top + 2, 260, 18),
            _body_font(BODY_SIZE, medium=True), pal["text"],
        )
        _label(
            body, description, (x, top + 22, 320, 15),
            _body_font(META_SIZE + 1), pal["muted"],
        )
        popup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(x + w - 200, top + 4, 200, 28)
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
        body.addSubview_(popup)
        refs[popup_key] = popup
        refs[values_key] = values
        return popup

    _small_caps(body, "Dictation", (x, 78, 200, 14), pal["muted"])
    popup_row(
        "Dictation key", "Hold while speaking.",
        96, options["hotkey"], "popup_hotkey", "hotkey_values",
    )
    _hairline(body, (x, 152, w, 1), dark)
    popup_row(
        "Microphone", "Applies to your next recording.",
        158, options["microphone"], "popup_microphone", "mic_values",
    )
    _hairline(body, (x, 214, w, 1), dark)
    popup_row(
        "Cleanup", "Punctuation and formatting.",
        220, options["cleanup"], "popup_cleanup", "cleanup_values",
    )
    # A closing hairline ends the selector group before the save cluster.
    _hairline(body, (x, 276, w, 1), dark)

    # The one filled action on the page; Restart stays a quiet text
    # action until a saved change actually needs it.
    buttons_y = 300
    _filled_button(
        controller, refs, body, "Save changes",
        x, buttons_y, 130, 32, "saveSettings:",
    )
    restart = _text_button(
        controller, refs, body, "Restart Bolo",
        x + 146, buttons_y + 4, 120, 24, "restartBolo:",
    )
    restart.setHidden_(not refs.get("restart_needed"))
    refs["restart_button"] = restart

    _label(
        body, "Dictation key and cleanup changes need a restart.",
        (x, buttons_y + 42, w, 18),
        _body_font(META_SIZE + 1), pal["muted"],
    )
    status_y = buttons_y + 70
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

    # Setup and vocabulary as disclosure rows: whole-row links into the
    # existing windows, hairline-separated like the rest of the page.
    setup_y = status_y + (44 if refs.get("settings_status_text") else 4)
    _small_caps(body, "Setup & vocabulary", (x, setup_y, 220, 14), pal["muted"])
    setup_y += 20

    def link_row(title, description, top, action, trailing=None):
        _label(
            body, title, (x, top + 6, 300, 18),
            _body_font(BODY_SIZE, medium=True), pal["text"],
        )
        _label(
            body, description, (x, top + 25, 420, 15),
            _body_font(META_SIZE + 1), pal["muted"],
        )
        if trailing is not None:
            _label(
                body, trailing, (x + w - 76, top + 8, 44, 15),
                _mono_font(META_SIZE + 1), pal["muted"], truncate=True,
                align="right",
            )
        _chevron(body, x + w - 26, top + 9, pal["muted"])
        _overlay_button(controller, refs, body, action, x, top, w, 44)

    link_row(
        "Open setup",
        "Permissions, the API key, and onboarding.",
        setup_y + 6, "openSetup:",
    )
    _hairline(body, (x, setup_y + 50, w, 1), dark)
    link_row(
        "Learned words",
        "Vocabulary learned from your corrections.",
        setup_y + 56, "openLearned:",
        trailing="{0}".format(dashboard["learned_words_count"]),
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
    # The hairline seam on the body's leading edge marks the sidebar
    # boundary and survives every re-render with the body itself.
    from AppKit import NSViewHeightSizable

    seam = _hairline(
        body, (0, 0, 1, body.bounds().size.height or HEIGHT), dark
    )
    seam.setAutoresizingMask_(NSViewHeightSizable)
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
