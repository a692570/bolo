#!/usr/bin/env python3
"""Native macOS status HUD for Bolo."""

import json
import os
import select
import sys
import time
import warnings

from objc import ObjCPointerWarning
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSColor,
    NSDefaultRunLoopMode,
    NSFont,
    NSFontWeightMedium,
    NSMakeRect,
    NSPanel,
    NSRunLoop,
    NSScreen,
    NSTextAlignmentLeft,
    NSTextField,
    NSView,
    NSViewWidthSizable,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSFloatingWindowLevel,
)
from Foundation import NSDate


warnings.filterwarnings("ignore", category=ObjCPointerWarning)


WIDTH = 420
HEIGHT = 44
BOTTOM_MARGIN = 220
DOT_SIZE = 8
STALL_TIMEOUT = 45.0


def color(red, green, blue, alpha=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, alpha)


PHASES = {
    "dictating": ("Dictating", color(0.34, 0.86, 0.61)),
    "listening": ("Dictating", color(0.34, 0.86, 0.61)),
    "thinking": ("Thinking", color(0.58, 0.65, 1.0)),
    "transcribing": ("Thinking", color(0.58, 0.65, 1.0)),
    "processing": ("Thinking", color(0.58, 0.65, 1.0)),
    "inserting": ("Inserting", color(1.0, 0.68, 0.34)),
    "inserted": ("Inserted", color(0.45, 0.88, 0.49)),
    "copied": ("Copied", color(0.45, 0.88, 0.49)),
    "success": ("Inserted", color(0.45, 0.88, 0.49)),
    "final": ("Done", color(0.80, 0.84, 0.88)),
    "error": ("Try again", color(1.0, 0.36, 0.36)),
}


def make_label():
    label = NSTextField.alloc().initWithFrame_(NSMakeRect(40, 10, WIDTH - 56, 22))
    label.setAlignment_(NSTextAlignmentLeft)
    label.setFont_(NSFont.systemFontOfSize_weight_(14.0, NSFontWeightMedium))
    label.setTextColor_(NSColor.whiteColor())
    label.setBackgroundColor_(NSColor.clearColor())
    label.setBezeled_(False)
    label.setBordered_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setAutoresizingMask_(NSViewWidthSizable)
    return label


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
app.finishLaunching()

screen = NSScreen.mainScreen().frame()
x = screen.origin.x + ((screen.size.width - WIDTH) / 2)
y = screen.origin.y + BOTTOM_MARGIN

window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(x, y, WIDTH, HEIGHT),
    NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
    2,
    False,
)
window.setLevel_(NSFloatingWindowLevel + 1)
window.setOpaque_(False)
window.setBackgroundColor_(NSColor.clearColor())
window.setIgnoresMouseEvents_(True)
window.setHasShadow_(True)
window.setHidesOnDeactivate_(False)
window.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 6))

content = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
content.setMaterial_(NSVisualEffectMaterialHUDWindow)
content.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
content.setState_(NSVisualEffectStateActive)
content.setWantsLayer_(True)
content.layer().setCornerRadius_(HEIGHT / 2)
content.layer().setMasksToBounds_(True)
window.setContentView_(content)

dot = NSView.alloc().initWithFrame_(NSMakeRect(20, (HEIGHT - DOT_SIZE) / 2, DOT_SIZE, DOT_SIZE))
dot.setWantsLayer_(True)
dot.layer().setCornerRadius_(DOT_SIZE / 2)
dot.layer().setBackgroundColor_(PHASES["dictating"][1].CGColor())

label = make_label()
content.addSubview_(dot)
content.addSubview_(label)


def preview_text(value):
    text = " ".join(str(value or "").split())
    if len(text) > 56:
        return "..." + text[-53:]
    return text


def render(phase, preview=""):
    text, accent = PHASES.get(phase, PHASES["dictating"])
    preview = preview_text(preview)
    label.setStringValue_(preview or text)
    dot.layer().setBackgroundColor_(accent.CGColor())


phase = "dictating"
preview = ""
render(phase, preview)
window.orderFrontRegardless()
last_message_at = time.time()

while True:
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        line = sys.stdin.readline()
        if line == "":
            break
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            message = {}
        phase = message.get("phase", phase)
        preview = message.get("text", "") if phase == "dictating" else ""
        render(phase, preview)
        last_message_at = time.time()

    if time.time() - last_message_at > STALL_TIMEOUT:
        break

    NSRunLoop.mainRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode,
        NSDate.dateWithTimeIntervalSinceNow_(0.05),
    )

window.orderOut_(None)
os._exit(0)
