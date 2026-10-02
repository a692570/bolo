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


def plan_layout(payload):
    """Pure geometry pass: line wrapping and vertical placement.

    Returns a plan with the content height, the button's y position, and
    per-row y positions for the label and wrapped detail lines, all in
    flipped-content coordinates (origin top-left).
    """
    rows = payload.get("rows", [])
    welcome = payload.get("welcome") or ""
    welcome_lines = wrap_lines(welcome) if welcome else []
    row_plans = []
    y = float(TOP_PAD)
    if welcome_lines:
        y += len(welcome_lines) * LABEL_LINE_H + WELCOME_GAP
    for row in rows:
        detail_lines = wrap_lines(row.get("detail") or "")
        row_plans.append(
            {
                "label_y": y,
                "detail_y": y + LABEL_LINE_H + 2,
                "detail_lines": detail_lines,
            }
        )
        y += LABEL_LINE_H + 2 + len(detail_lines) * DETAIL_LINE_H + ROW_GAP
    button_y = y + 6
    height = button_y + BUTTON_AREA_H
    return {
        "welcome_lines": welcome_lines,
        "welcome_y": TOP_PAD if welcome_lines else None,
        "rows": row_plans,
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

        def windowWillClose_(self, notification):
            STATE["user_done"] = True

    colors = {
        "ok": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.88, 0.49, 1.0),
        "warn": NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.36, 0.36, 1.0),
        "pending": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.60, 0.60, 0.62, 1.0),
    }

    plan = plan_layout(payload)
    try_it_index = payload.get("try_it_index")
    try_it_index = try_it_index if isinstance(try_it_index, int) else None

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
    try_it_refs = None
    for index, (row, row_plan) in enumerate(zip(payload.get("rows", []), plan["rows"])):
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
            label_font,
            NSColor.labelColor(),
            x=TEXT_X,
        )
        detail_label = make_label(
            "\n".join(row_plan["detail_lines"]),
            row_plan["detail_y"],
            len(row_plan["detail_lines"]) * DETAIL_LINE_H,
            detail_font,
            NSColor.secondaryLabelColor(),
            x=TEXT_X,
        )
        if try_it_index is not None and index == try_it_index:
            try_it_refs = {"dot": dot, "detail_label": detail_label}

    controller = WindowController.alloc().init()
    button = NSButton.buttonWithTitle_target_action_(
        payload.get("button") or "Close", controller, "finish:"
    )
    button.setBezelStyle_(NSBezelStyleRounded)
    button.setFrame_(NSMakeRect(WIDTH - MARGIN - 120, plan["button_y"], 120, 24))
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
