#!/usr/bin/env python3
"""AppKit window for Bolo first-run onboarding and status.

The Rust runtime spawns this script and writes one JSON payload line to
stdin; further lines update the onboarding try-it row. Closing the window
(button or close box) exits 0, and a first-run onboarding session writes
~/.bolo/onboarding.json so the window is shown once per install.
"""

import json
import os
import select
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
        detail_lines = wrap_lines(row.get("detail") or "")
        if key_index is not None and index == key_index:
            field_y = y + LABEL_LINE_H + 2
            detail_y = field_y + KEY_FIELD_H + 4
        else:
            field_y = None
            detail_y = y + LABEL_LINE_H + 2
        row_plans.append(
            {
                "label_y": y,
                "field_y": field_y,
                "detail_y": detail_y,
                "detail_lines": detail_lines,
            }
        )
        y = detail_y + len(detail_lines) * DETAIL_LINE_H + ROW_GAP
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
    """Create the AppKit window; imports stay local so tests import safely."""
    from AppKit import (
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

        def windowWillClose_(self, notification):
            STATE["user_done"] = True


    colors = {
        "ok": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.88, 0.49, 1.0),
        "warn": NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.36, 0.36, 1.0),
        "pending": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.60, 0.60, 0.62, 1.0),
    }
    accent = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.34, 0.86, 0.61, 1.0)

    plan = plan_layout(payload)
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

    content = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, plan["height"]))
    window.setContentView_(content)

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

    if plan["brand_y"] is not None:
        # Brand row: small green waveform glyph plus the wordmark, matching
        # the icon family (dark surface, green bars) at a subtle size. Bars
        # sit on a shared baseline so the glyph reads as a waveform.
        bar_x = float(MARGIN)
        baseline = plan["brand_y"] + BRAND_ROW_H - 4.0
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
            payload.get("brand") or "BOLO",
            plan["brand_y"] + 5,
            BRAND_ROW_H - 5,
            NSFont.boldSystemFontOfSize_(BRAND_WORDMARK_SIZE),
            NSColor.labelColor(),
            x=MARGIN + bars_width + 10,
        )
        wordmark.setSelectable_(False)

    if plan["welcome_y"] is not None:
        make_label(
            "\n".join(plan["welcome_lines"]),
            plan["welcome_y"],
            len(plan["welcome_lines"]) * LABEL_LINE_H,
            NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium),
            NSColor.labelColor(),
        )

    label_font = NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium)
    detail_font = NSFont.systemFontOfSize_weight_(12.0, NSFontWeightRegular)
    hero_label_font = NSFont.systemFontOfSize_weight_(
        HERO_LABEL_SIZE, NSFontWeightMedium
    )
    controller = WindowController.alloc().init()
    try_it_refs = None
    key_refs = None
    key_index = plan["key_index"]
    for index, (row, row_plan) in enumerate(zip(payload.get("rows", []), plan["rows"])):
        is_hero_row = (
            try_it_index is not None
            and index == try_it_index
            and hero_line is not None
            and row.get("state") == "pending"
        )
        dot = NSView.alloc().initWithFrame_(NSMakeRect(MARGIN, row_plan["label_y"] + 5, 10, 10))
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
        if try_it_index is not None and index == try_it_index:
            try_it_refs = {"dot": dot, "detail_label": detail_label}
        if key_index is not None and index == key_index:
            key_refs = {"dot": dot, "field": field, "detail_label": detail_label}

    STATE["key_refs"] = key_refs

    # Done affordance: the brand-green accent background with white text,
    # so the button reads as enabled against both light and dark windows
    # instead of the washed-out default bezel. Return triggers it too.
    # NSAttributedString attribute keys are stable string constants
    # ("NSFont", "NSColor", "NSParagraphStyle"), used as literals here.
    from AppKit import NSMutableParagraphStyle, NSTextAlignmentCenter

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
    from Foundation import NSAttributedString

    button.setAttributedTitle_(
        NSAttributedString.alloc().initWithString_attributes_(
            payload.get("button") or "Close", title_attributes
        )
    )
    button.setFrame_(
        NSMakeRect(WIDTH - MARGIN - DONE_BUTTON_W, plan["button_y"], DONE_BUTTON_W, DONE_BUTTON_H)
    )
    content.addSubview_(button)

    window.setDelegate_(controller)
    window.center()
    app.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)

    return {
        "window": window,
        "app": app,
        "try_it_refs": try_it_refs,
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
    """
    nsrunloop, nsdate, mode = ui["run_loop"]
    window = ui["window"]
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
