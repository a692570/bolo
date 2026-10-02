#!/usr/bin/env python3
"""AppKit window for Bolo first-run onboarding and status.

The Rust runtime spawns this script and writes one JSON payload line to
stdin; further lines update the onboarding try-it row. Closing the window
(button or close box) exits 0, and a first-run onboarding session writes
~/.bolo/onboarding.json so the window is shown once per install.

The onboarding Accessibility warn row carries a tappable action: an
Open Accessibility Settings button deep-links System Settings, a 2s trust
poll flips the row green once the user grants, and bundle mode then swaps
in a Restart Bolo button (source mode keeps the ./restart.sh line).
"""

import json
import os
import select
import subprocess
import sys
import textwrap
import time
import warnings

import bolo_env

MARKER_VERSION = 1
MARKER_FILE = os.path.expanduser("~/.bolo/onboarding.json")
WIDTH = 560
MARGIN = 20
TEXT_X = MARGIN + 10 + 12
TOP_PAD = 18
LABEL_LINE_H = 19
DETAIL_LINE_H = 16
ROW_GAP = 12
WELCOME_GAP = 8
BUTTON_AREA_H = 58
DETAIL_WRAP_AT = 64
KEY_FIELD_W = 330
KEY_FIELD_H = 24
VALIDATE_BUTTON_W = 100
KEY_ENTRY_NAME = "ASSEMBLYAI_API_KEY"

# Brand row: wordmark plus a small green waveform glyph (matching the
# Bolo.icns family: dark surfaces, green bars).
BRAND_ROW_H = 26
BRAND_GAP = 12
BRAND_BARS = ((3, 9), (3, 15), (3, 19), (3, 12), (3, 6))
BRAND_BAR_GAP = 3
BRAND_WORDMARK_SIZE = 15.0

# Hero try-it row: when the payload marks the try-it step as the one thing
# left, its label grows and its instruction line brightens.
HERO_LABEL_SIZE = 15.0

DONE_BUTTON_W = 120
DONE_BUTTON_H = 26

ASSEMBLYAI_LIST_URL = "https://api.assemblyai.com/v2/transcript?limit=1"
KEY_VALIDATION_TIMEOUT_S = 6.0

# Accessibility grant flow: the onboarding warn row carries a button that
# deep-links System Settings to the Accessibility list (Apple never prompts
# for this permission, so the row must send the user there itself), the
# window polls trust while that row is showing, and a granted flip turns
# the row green. Bundle mode then swaps in a Restart Bolo button; source
# mode has no supervised relaunch, so the granted detail keeps the
# ./restart.sh instruction instead of a button.
ACCESSIBILITY_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)
ACCESSIBILITY_LABEL = "Accessibility"
OPEN_SETTINGS_TITLE = "Open Accessibility Settings"
RESTART_TITLE = "Restart Bolo"
GRANTED_DETAIL = "Granted."
SOURCE_GRANTED_DETAIL = "Granted. Run ./restart.sh so Bolo picks up the grant."
TRUST_POLL_INTERVAL_S = 2.0
ACTION_KINDS = ("open_settings", "restart")
ACTION_BUTTON_W = 210
ACTION_BUTTON_H = 24
ACTION_BUTTON_GAP = 6

# Learned-corrections file the learning window edits. The Rust runtime owns
# the file too (it reads pairs at startup and re-checks the mtime at each
# recording start), so deletions here take effect without a restart.
LEARNED_FILE = os.path.expanduser("~/.bolo/learned_vocabulary.json")


def marker_payload(now_ms=None):
    """Marker document written when onboarding completes."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return {"version": MARKER_VERSION, "completed_at_ms": now_ms}


def write_marker(path, payload):
    """Persist the completion marker with private permissions."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def parse_update(line):
    """Decode one stdin update line; None when it is not an object."""
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def wrap_lines(text, width=DETAIL_WRAP_AT):
    """Wrap one detail string for display; empty text stays one empty line."""
    if not text:
        return [""]
    return textwrap.wrap(text, width=width) or [""]


def is_bundle_mode():
    """Whether this window runs under the distributed Bolo.app bundle.

    The bundle launcher exports BOLO_BUNDLE_MODE=1 for the runtime, which
    its helper children (this window included) inherit.
    """
    return os.environ.get("BOLO_BUNDLE_MODE") == "1"


def row_action_kind(row):
    """Validated action kind carried by a row, or None when it has none."""
    action = row.get("action") if isinstance(row, dict) else None
    if not isinstance(action, dict):
        return None
    kind = action.get("kind")
    return kind if kind in ACTION_KINDS else None


def action_button_title(row):
    """Button label for a row action: the payload title when it carries
    one, else the default title for the kind."""
    action = row.get("action")
    if isinstance(action, dict) and isinstance(action.get("title"), str) and action["title"]:
        return action["title"]
    defaults = {"open_settings": OPEN_SETTINGS_TITLE, "restart": RESTART_TITLE}
    return defaults.get(row_action_kind(row)) or OPEN_SETTINGS_TITLE


def open_settings_command():
    """Shell argv that opens System Settings on the Accessibility list."""
    return ["open", ACCESSIBILITY_SETTINGS_URL]


def restart_command(bundle):
    """Shell argv that restarts the supervised runtime, or None from source.

    Bundle mode stops the runtime with SIGUSR1: the bundle's supervisor
    relaunches it a few seconds later (it restarts on any exit code
    outside 0/1/137/143, while SIGTERM's 143 is its deliberate
    quit-and-stay-dead code), which reopens this window with fresh rows.
    Source mode has no matching supervised relaunch, so the window keeps
    the ./restart.sh instruction instead of offering a button.
    """
    if not bundle:
        return None
    return ["pkill", "-USR1", "-f", "Bolo.app/Contents/MacOS"]


def accessibility_self_trusted():
    """Whether macOS trusts this helper process for Accessibility.

    Mirrors accessibility_trusted.py's non-prompt check: this window runs
    under the same interpreter as the paste helpers, so its reading
    matches the row the runtime rendered. Returns None when the check
    itself fails, so the poll keeps waiting instead of flipping on a lie.
    """
    try:
        import ApplicationServices as ax

        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return None


def accessibility_flip(row, trusted_now, bundle):
    """Row update after one Accessibility trust poll; None when unchanged.

    `row` is the row as last displayed, `trusted_now` the fresh reading
    from `accessibility_self_trusted`, and `bundle` whether the window
    runs under Bolo.app. Flipping to granted turns the row green with a
    "Granted." line: bundle mode swaps the settings button for a Restart
    Bolo action, source mode drops the button and keeps the ./restart.sh
    instruction in the detail.
    """
    if trusted_now is not True:
        return None
    if row.get("label") != ACCESSIBILITY_LABEL or row.get("state") == "ok":
        return None
    if bundle:
        return {
            "label": ACCESSIBILITY_LABEL,
            "detail": GRANTED_DETAIL,
            "state": "ok",
            "action": {"kind": "restart", "title": RESTART_TITLE},
        }
    return {
        "label": ACCESSIBILITY_LABEL,
        "detail": SOURCE_GRANTED_DETAIL,
        "state": "ok",
        "action": None,
    }


def write_learned_file(path, payload):
    """Atomic learned-vocabulary write mirroring the Rust runtime's writer:
    indent 2 with a trailing newline, private permissions, rename into
    place so a crash never leaves a half-written file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def delete_learned_pair(path, misheard):
    """Remove one learned pair from the corrections file.

    Returns ``(removed, error)`` where error is plain display text for the
    window when something failed; both values are None-free: a successful
    removal has ``error=None``.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return False, "The learned-words file is gone."
    except Exception:
        return False, "Could not read the learned-words file."
    corrections = data.get("corrections") if isinstance(data, dict) else None
    if not isinstance(corrections, dict) or misheard not in corrections:
        return False, "That correction is no longer saved."
    del corrections[misheard]
    try:
        write_learned_file(path, data)
    except Exception:
        return False, "Could not save the learned-words file."
    return True, None


def learning_display_payload(pairs, error, spec):
    """Pure display payload for the learning window: the welcome copy picked
    from the spec (empty state when no pairs remain), one row per pair
    showing ``misheard -> corrected``, and the plain error line last."""
    rows = []
    for pair in pairs:
        misheard = pair.get("misheard") or ""
        corrected = pair.get("corrected") or ""
        rows.append(
            {"label": "{0} -> {1}".format(misheard, corrected), "detail": "", "state": "ok"}
        )
    if error:
        rows.append({"label": error, "detail": "", "state": "warn"})
    if pairs:
        welcome = spec.get("hint_welcome") or ""
    else:
        welcome = spec.get("empty_welcome") or ""
    return {"brand": "BOLO", "welcome": welcome, "rows": rows}


def key_entry_index(payload):
    """Row index of the API-key entry field, or None when it is not shown."""
    spec = payload.get("key_entry")
    if not isinstance(spec, dict):
        return None
    index = spec.get("index")
    rows = payload.get("rows")
    if (
        isinstance(index, int)
        and isinstance(rows, list)
        and 0 <= index < len(rows)
    ):
        return index
    return None


def classify_key_response(status):
    """Map one AssemblyAI HTTP status to a validation verdict.

    A 200 from the transcript-list endpoint proves the key authenticates
    without spending any audio on transcription; 401 is a definite reject.
    Anything else is treated as a transient error rather than a rejection.
    """
    if status == 200:
        return "valid"
    if status == 401:
        return "invalid"
    return "error"


def fetch_assemblyai_status(key, url=ASSEMBLYAI_LIST_URL, timeout=KEY_VALIDATION_TIMEOUT_S):
    """Return the HTTP status code for a key probe; raise on network failure."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"Authorization": key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode()
    except urllib.error.HTTPError as error:
        return error.code


def validate_and_save_key(
    key,
    env_path=None,
    fetch=fetch_assemblyai_status,
):
    """Validate one API key and persist it when AssemblyAI accepts it.

    Returns ``(verdict, detail)`` where detail is display text for the
    onboarding row. `fetch` is injectable so tests cover the 200/401/other
    branches without network access; it must raise on transport errors.
    """
    key = (key or "").strip()
    if not key:
        return "empty", "Paste your AssemblyAI API key first, then click Validate."
    if env_path is None:
        env_path = bolo_env.default_env_path()
    try:
        status = fetch(key)
    except Exception:
        return (
            "unreachable",
            "Could not reach api.assemblyai.com. Check your internet "
            "connection and try again.",
        )
    verdict = classify_key_response(status)
    if verdict == "valid":
        bolo_env.write_env_value(env_path, KEY_ENTRY_NAME, key)
        print("[app-window] key validation: valid", file=sys.stderr, flush=True)
        return (
            "valid",
            "Key saved. Click Done and Bolo restarts with it in a few seconds.",
        )
    if verdict == "invalid":
        return (
            "invalid",
            "AssemblyAI rejected that key. Double-check it and try again.",
        )
    return (
        "error",
        "AssemblyAI returned an unexpected response. Try again in a moment.",
    )



def plan_layout(payload):
    """Pure geometry pass: line wrapping and vertical placement.

    Returns a plan with the content height, the button's y position, and
    per-row y positions for the label, optional key-entry field, and
    wrapped detail lines, all in flipped-content coordinates (origin
    top-left). The row named by ``payload["key_entry"]["index"]`` reserves
    extra vertical space for the text field plus Validate button. A truthy
    ``payload["brand"]`` reserves the brand row above the welcome lines.
    A row carrying a valid ``action`` reserves one button line under its
    detail for the row's tappable action.
    """
    rows = payload.get("rows", [])
    brand = bool(payload.get("brand"))
    welcome = payload.get("welcome") or ""
    welcome_lines = wrap_lines(welcome) if welcome else []
    key_index = key_entry_index(payload)
    row_plans = []
    y = float(TOP_PAD)
    brand_y = None
    if brand:
        brand_y = y
        y += BRAND_ROW_H + BRAND_GAP
    if welcome_lines:
        y += len(welcome_lines) * LABEL_LINE_H + WELCOME_GAP
    for index, row in enumerate(rows):
        detail_text = row.get("detail") or ""
        # Rows without detail text (learning-window pair rows) reserve no
        # detail lines, so tappable rows stay a single tight line each.
        detail_lines = wrap_lines(detail_text) if detail_text else []
        if key_index is not None and index == key_index:
            field_y = y + LABEL_LINE_H + 2
            detail_y = field_y + KEY_FIELD_H + 4
        else:
            field_y = None
            detail_y = y + LABEL_LINE_H + 2
        # A row carrying an action reserves a button line under the detail.
        action_y = None
        if row_action_kind(row) is not None:
            action_y = detail_y + len(detail_lines) * DETAIL_LINE_H + ACTION_BUTTON_GAP
        row_plans.append(
            {
                "label_y": y,
                "field_y": field_y,
                "detail_y": detail_y,
                "detail_lines": detail_lines,
                "action_y": action_y,
            }
        )
        y = detail_y + len(detail_lines) * DETAIL_LINE_H + ROW_GAP
        if action_y is not None:
            y = action_y + ACTION_BUTTON_H + ROW_GAP
    button_y = y + 6
    height = button_y + BUTTON_AREA_H
    welcome_y = None
    if welcome_lines:
        welcome_y = TOP_PAD + ((BRAND_ROW_H + BRAND_GAP) if brand else 0)
    return {
        "brand": brand,
        "brand_y": brand_y,
        "welcome_lines": welcome_lines,
        "welcome_y": welcome_y,
        "rows": row_plans,
        "key_index": key_index,
        "button_y": button_y,
        "height": height,
    }


def read_payload():
    """Read and validate the one-line JSON payload from stdin."""
    line = sys.stdin.readline()
    if not line.strip():
        return None, "empty payload on stdin"
    try:
        payload = json.loads(line)
    except ValueError:
        return None, "payload is not valid JSON"
    if not isinstance(payload, dict):
        return None, "payload is not a JSON object"
    return payload, None


def build_ui(payload):
    """Create the AppKit window; imports stay local so tests import safely.

    Mode "learning" renders its rows from the pairs the runtime sends and
    makes each pair a tappable button that removes it from the
    learned-words file, re-rendering in place; every other mode renders
    the generic label/detail rows from the payload.
    """
    from AppKit import (
        NSAttributedString,
        NSMutableParagraphStyle,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBezelStyleRounded,
        NSButton,
        NSDate,
        NSColor,
        NSDefaultRunLoopMode,
        NSFont,
        NSFontWeightMedium,
        NSFontWeightRegular,
        NSMakeRect,
        NSRunLoop,
        NSObject,
        NSSecureTextField,
        NSTextAlignmentCenter,
        NSTextAlignmentLeft,
        NSTextField,
        NSView,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskTitled,
    )
    from objc import ObjCPointerWarning

    warnings.filterwarnings("ignore", category=ObjCPointerWarning)

    class FlippedView(NSView):
        def isFlipped(self):
            return True

    class WindowController(NSObject):
        def finish_(self, sender):
            STATE["user_done"] = True

        def validateKey_(self, sender):
            refs = STATE.get("key_refs")
            if not refs:
                return
            field = refs["field"]
            key = str(field.stringValue())
            verdict, detail = validate_and_save_key(key)
            dot_color = {
                "valid": colors["ok"],
                "error": colors["pending"],
            }.get(verdict, colors["warn"])
            refs["dot"].layer().setBackgroundColor_(dot_color.CGColor())
            refs["detail_label"].setStringValue_("\n".join(wrap_lines(detail)))
            if verdict == "valid":
                # Stop the field from reporting a stale pending row if the
                # user closes the window without further edits.
                field.setEnabled_(False)

        def deleteLearned_(self, sender):
            refs = STATE.get("learning") or {}
            rerender = refs.get("rerender")
            if not rerender:
                return
            try:
                index = int(sender.tag())
            except (TypeError, ValueError):
                return
            pairs = refs.get("pairs") or []
            if not 0 <= index < len(pairs):
                return
            misheard = pairs[index].get("misheard")
            removed, error = delete_learned_pair(refs.get("file") or LEARNED_FILE, misheard)
            if removed:
                refs["pairs"] = pairs[:index] + pairs[index + 1 :]
                refs["error"] = None
            else:
                refs["error"] = error or "Could not remove that correction."
            rerender()

        def openAccessibility_(self, sender):
            print(
                "[app-window] opening Accessibility settings",
                file=sys.stderr,
                flush=True,
            )
            subprocess.Popen(open_settings_command())

        def restartBolo_(self, sender):
            command = restart_command(is_bundle_mode())
            if command is None:
                return
            print(
                "[app-window] restarting Bolo: {0}".format(" ".join(command)),
                file=sys.stderr,
                flush=True,
            )
            subprocess.Popen(command)

        def windowWillClose_(self, notification):
            STATE["user_done"] = True

    colors = {
        "ok": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.88, 0.49, 1.0),
        "warn": NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.36, 0.36, 1.0),
        "pending": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.60, 0.60, 0.62, 1.0),
    }
    accent = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.34, 0.86, 0.61, 1.0)

    learning_spec = payload.get("learning")
    learning_spec = learning_spec if isinstance(learning_spec, dict) else None
    is_learning = learning_spec is not None
    raw_pairs = learning_spec.get("pairs") if learning_spec else None
    learning_pairs = [pair for pair in (raw_pairs or []) if isinstance(pair, dict)]
    learning_error = learning_spec.get("error") if learning_spec else None
    if is_learning:
        display = learning_display_payload(learning_pairs, learning_error, learning_spec)
    else:
        display = payload
    plan = plan_layout(display)
    try_it_index = payload.get("try_it_index")
    try_it_index = try_it_index if isinstance(try_it_index, int) else None
    hero_line = payload.get("try_it_hero")
    hero_line = hero_line if isinstance(hero_line, str) else None

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.finishLaunching()

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, WIDTH, plan["height"]),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
        2,
        False,
    )
    window.setTitle_(payload.get("title") or "Bolo")
    window.setReleasedWhenClosed_(False)

    label_font = NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium)
    detail_font = NSFont.systemFontOfSize_weight_(12.0, NSFontWeightRegular)
    hero_label_font = NSFont.systemFontOfSize_weight_(
        HERO_LABEL_SIZE, NSFontWeightMedium
    )
    controller = WindowController.alloc().init()

    def build_content(content_display, content_plan):
        """Populate one content view from a display payload; reusable so the
        learning window can re-render after a deletion."""
        content = FlippedView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH, content_plan["height"])
        )

        def make_label(text, y, h, font, color, x=MARGIN):
            label = NSTextField.labelWithString_(text)
            label.setFrame_(NSMakeRect(x, y, WIDTH - MARGIN - x, h))
            label.setFont_(font)
            label.setTextColor_(color)
            label.setEditable_(False)
            label.setSelectable_(True)
            label.setBezeled_(False)
            label.setDrawsBackground_(False)
            content.addSubview_(label)
            return label

        if content_plan["brand_y"] is not None:
            # Brand row: small green waveform glyph plus the wordmark, matching
            # the icon family (dark surface, green bars) at a subtle size. Bars
            # sit on a shared baseline so the glyph reads as a waveform.
            bar_x = float(MARGIN)
            baseline = content_plan["brand_y"] + BRAND_ROW_H - 4.0
            for bar_w, bar_h in BRAND_BARS:
                bar = NSView.alloc().initWithFrame_(
                    NSMakeRect(bar_x, baseline - bar_h, bar_w, bar_h)
                )
                bar.setWantsLayer_(True)
                bar.layer().setCornerRadius_(bar_w / 2.0)
                bar.layer().setBackgroundColor_(accent.CGColor())
                content.addSubview_(bar)
                bar_x += bar_w + BRAND_BAR_GAP
            bars_width = sum(w for w, _h in BRAND_BARS) + BRAND_BAR_GAP * (len(BRAND_BARS) - 1)
            wordmark = make_label(
                content_display.get("brand") or "BOLO",
                content_plan["brand_y"] + 5,
                BRAND_ROW_H - 5,
                NSFont.boldSystemFontOfSize_(BRAND_WORDMARK_SIZE),
                NSColor.labelColor(),
                x=MARGIN + bars_width + 10,
            )
            wordmark.setSelectable_(False)

        if content_plan["welcome_y"] is not None:
            make_label(
                "\n".join(content_plan["welcome_lines"]),
                content_plan["welcome_y"],
                len(content_plan["welcome_lines"]) * LABEL_LINE_H,
                NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium),
                NSColor.labelColor(),
            )

        try_it_refs = None
        key_refs = None
        accessibility_refs = None
        key_index = content_plan["key_index"]
        for index, (row, row_plan) in enumerate(
            zip(content_display.get("rows", []), content_plan["rows"])
        ):
            is_hero_row = (
                try_it_index is not None
                and index == try_it_index
                and hero_line is not None
                and row.get("state") == "pending"
            )
            if is_learning and index < len(learning_pairs):
                # Tappable pair row: a borderless button spanning the row
                # width, left-aligned like the plain labels. Tapping it
                # removes that learned pair and re-renders the window.
                pair_button = NSButton.buttonWithTitle_target_action_(
                    row.get("label", ""), controller, "deleteLearned:"
                )
                pair_button.setBordered_(False)
                pair_button.setTag_(index)
                pair_button.setFont_(label_font)
                pair_button.setFrame_(
                    NSMakeRect(
                        MARGIN,
                        row_plan["label_y"] - 3,
                        WIDTH - 2 * MARGIN,
                        LABEL_LINE_H + 6,
                    )
                )
                pair_title = NSMutableParagraphStyle.alloc().init()
                pair_title.setAlignment_(NSTextAlignmentLeft)
                # NSAttributedString attribute keys are stable string
                # constants ("NSFont", "NSColor", "NSParagraphStyle"), used
                # as literals here.
                pair_button.setAttributedTitle_(
                    NSAttributedString.alloc().initWithString_attributes_(
                        row.get("label", ""),
                        {
                            "NSFont": label_font,
                            "NSColor": NSColor.labelColor(),
                            "NSParagraphStyle": pair_title,
                        },
                    )
                )
                content.addSubview_(pair_button)
                continue
            dot = NSView.alloc().initWithFrame_(
                NSMakeRect(MARGIN, row_plan["label_y"] + 5, 10, 10)
            )
            dot.setWantsLayer_(True)
            dot.layer().setCornerRadius_(5)
            dot.layer().setBackgroundColor_(
                colors.get(row.get("state"), colors["pending"]).CGColor()
            )
            content.addSubview_(dot)
            make_label(
                row.get("label", ""),
                row_plan["label_y"],
                LABEL_LINE_H,
                hero_label_font if is_hero_row else label_font,
                NSColor.labelColor(),
                x=TEXT_X,
            )
            if key_index is not None and index == key_index:
                # Key-entry row: secure text field plus Validate button between
                # the label and the detail line. The validation GET runs inline
                # (bounded by KEY_VALIDATION_TIMEOUT_S), which briefly pauses
                # the run loop on click; acceptable for a one-shot action.
                field = NSSecureTextField.alloc().initWithFrame_(
                    NSMakeRect(TEXT_X, row_plan["field_y"], KEY_FIELD_W, KEY_FIELD_H)
                )
                field.cell().setPlaceholderString_(
                    (payload.get("key_entry") or {}).get("placeholder")
                    or "Paste your AssemblyAI API key"
                )
                content.addSubview_(field)
                validate_button = NSButton.buttonWithTitle_target_action_(
                    "Validate", controller, "validateKey:"
                )
                validate_button.setBezelStyle_(NSBezelStyleRounded)
                validate_button.setFrame_(
                    NSMakeRect(
                        TEXT_X + KEY_FIELD_W + 10,
                        row_plan["field_y"],
                        VALIDATE_BUTTON_W,
                        KEY_FIELD_H,
                    )
                )
                content.addSubview_(validate_button)
            detail_label = make_label(
                "\n".join(row_plan["detail_lines"]),
                row_plan["detail_y"],
                len(row_plan["detail_lines"]) * DETAIL_LINE_H,
                detail_font,
                NSColor.labelColor() if is_hero_row else NSColor.secondaryLabelColor(),
                x=TEXT_X,
            )
            action_kind = row_action_kind(row)
            if action_kind is not None:
                # Row action button (the Accessibility warn row's deep
                # link, or the granted row's restart): same target-action
                # mechanism the Validate button and the learning window's
                # tappable rows already use.
                action_button = NSButton.buttonWithTitle_target_action_(
                    action_button_title(row),
                    controller,
                    "openAccessibility:" if action_kind == "open_settings" else "restartBolo:",
                )
                action_button.setBezelStyle_(NSBezelStyleRounded)
                action_button.setFrame_(
                    NSMakeRect(
                        TEXT_X,
                        row_plan["action_y"],
                        ACTION_BUTTON_W,
                        ACTION_BUTTON_H,
                    )
                )
                content.addSubview_(action_button)
                if (
                    not is_learning
                    and action_kind == "open_settings"
                    and row.get("label") == ACCESSIBILITY_LABEL
                ):
                    accessibility_refs = {
                        "row": row,
                        "dot": dot,
                        "detail_label": detail_label,
                        "button": action_button,
                    }
            if try_it_index is not None and index == try_it_index:
                try_it_refs = {"dot": dot, "detail_label": detail_label}
            if key_index is not None and index == key_index:
                key_refs = {"dot": dot, "field": field, "detail_label": detail_label}

        # Done affordance: the brand-green accent background with white text,
        # so the button reads as enabled against both light and dark windows
        # instead of the washed-out default bezel. Return triggers it too.
        button = NSButton.buttonWithTitle_target_action_(
            payload.get("button") or "Close", controller, "finish:"
        )
        button.setBordered_(False)
        button.setWantsLayer_(True)
        button.layer().setBackgroundColor_(accent.CGColor())
        button.layer().setCornerRadius_(13.0)
        button.setKeyEquivalent_("\r")
        paragraph = NSMutableParagraphStyle.alloc().init()
        paragraph.setAlignment_(NSTextAlignmentCenter)
        title_attributes = {
            "NSFont": NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium),
            "NSColor": NSColor.whiteColor(),
            "NSParagraphStyle": paragraph,
        }
        button.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                payload.get("button") or "Close", title_attributes
            )
        )
        button.setFrame_(
            NSMakeRect(
                WIDTH - MARGIN - DONE_BUTTON_W,
                content_plan["button_y"],
                DONE_BUTTON_W,
                DONE_BUTTON_H,
            )
        )
        content.addSubview_(button)
        return content, {
            "try_it_refs": try_it_refs,
            "key_refs": key_refs,
            "accessibility_refs": accessibility_refs,
        }

    content, row_refs = build_content(display, plan)
    window.setContentView_(content)
    STATE["key_refs"] = row_refs["key_refs"]

    # Accessibility trust polling: runs while the onboarding window shows
    # the untrusted Accessibility warn row, so the row flips green within
    # one poll interval of the user enabling Bolo.
    accessibility = None
    accessibility_refs = row_refs["accessibility_refs"]
    if accessibility_refs is not None and accessibility_refs["row"].get("state") == "warn":
        bundle = is_bundle_mode()

        def apply_accessibility_granted(refs=accessibility_refs, bundle=bundle):
            """Flip the row green in place and swap the button: Restart
            Bolo in bundle mode, no button from source (the granted
            detail's ./restart.sh line carries the instruction)."""
            new_row = accessibility_flip(refs["row"], True, bundle)
            if new_row is None:
                return
            print("[app-window] accessibility granted", file=sys.stderr, flush=True)
            refs["dot"].layer().setBackgroundColor_(colors["ok"].CGColor())
            refs["detail_label"].setStringValue_("\n".join(wrap_lines(new_row["detail"])))
            if row_action_kind(new_row) == "restart":
                # Keep the button's target; only the action moves.
                refs["button"].setTitle_(RESTART_TITLE)
                refs["button"].setAction_("restartBolo:")
            else:
                refs["button"].removeFromSuperview()

        accessibility = {
            "active": True,
            "next_check": time.monotonic() + TRUST_POLL_INTERVAL_S,
            "apply": apply_accessibility_granted,
        }

    if is_learning:
        # The delete action re-renders in place: same window, fresh content,
        # top-left corner pinned so the height change is not jumpy.
        def learning_rerender():
            learning_refs = STATE.get("learning") or {}
            rerender_display = learning_display_payload(
                learning_refs.get("pairs") or [],
                learning_refs.get("error"),
                learning_spec,
            )
            rerender_plan = plan_layout(rerender_display)
            rerender_content, _ = build_content(rerender_display, rerender_plan)
            frame = window.frame()
            rerender_frame = NSMakeRect(
                frame.origin.x,
                frame.origin.y + frame.size.height - rerender_plan["height"],
                WIDTH,
                rerender_plan["height"],
            )
            window.setContentView_(rerender_content)
            window.setFrame_display_(rerender_frame, True)

        STATE["learning"] = {
            "pairs": learning_pairs,
            "error": learning_error,
            "file": learning_spec.get("file") or LEARNED_FILE,
            "rerender": learning_rerender,
        }

    try_it_refs = row_refs["try_it_refs"]

    window.setDelegate_(controller)
    window.center()
    app.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)

    return {
        "window": window,
        "app": app,
        "try_it_refs": try_it_refs,
        "accessibility": accessibility,
        "run_loop": (NSRunLoop, NSDate, NSDefaultRunLoopMode),
    }


def apply_try_it_complete(ui, update):
    """Turn the try-it row green and show the capture line."""
    refs = ui.get("try_it_refs")
    if not refs:
        return
    from AppKit import NSColor

    refs["dot"].layer().setBackgroundColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.88, 0.49, 1.0).CGColor()
    )
    detail = update.get("detail") or ""
    if detail:
        refs["detail_label"].setStringValue_("\n".join(wrap_lines(detail)))


def run_event_loop(ui):
    """Pump AppKit and stdin until the user closes the window or parent dies.

    Returns True when the user closed the window (button or close box);
    False when stdin closed first, meaning the runtime went away and the
    onboarding session must not be marked complete.

    While an Accessibility warn row is showing, the loop also polls macOS
    trust every TRUST_POLL_INTERVAL_S so the row flips green the moment
    the user enables Bolo, swapping in the post-grant action.
    """
    nsrunloop, nsdate, mode = ui["run_loop"]
    window = ui["window"]
    accessibility = ui.get("accessibility")
    window.orderFrontRegardless()
    while not STATE["user_done"]:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if ready:
            line = sys.stdin.readline()
            if line == "":
                return False
            update = parse_update(line)
            if update and update.get("try_it_complete"):
                apply_try_it_complete(ui, update)
        if (
            accessibility is not None
            and accessibility["active"]
            and time.monotonic() >= accessibility["next_check"]
        ):
            accessibility["next_check"] = time.monotonic() + TRUST_POLL_INTERVAL_S
            if accessibility_self_trusted():
                accessibility["active"] = False
                accessibility["apply"]()
        nsrunloop.mainRunLoop().runMode_beforeDate_(
            mode, nsdate.dateWithTimeIntervalSinceNow_(0.05)
        )
    return True


def main():
    payload, failure = read_payload()
    if failure is not None:
        print("[app-window] {0}".format(failure), file=sys.stderr)
        return 1
    try:
        ui = build_ui(payload)
    except ImportError as error:
        print("[app-window] AppKit unavailable: {0}".format(error), file=sys.stderr)
        return 1
    user_closed = run_event_loop(ui)
    ui["window"].orderOut_(None)
    if user_closed and payload.get("write_marker"):
        write_marker(MARKER_FILE, marker_payload())
    return 0


STATE = {"user_done": False}

if __name__ == "__main__":
    sys.exit(main())
