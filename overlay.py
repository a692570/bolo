#!/usr/bin/env python3
"""
Standalone overlay process for Bolo.
Receives JSON messages on stdin to update phase and preview text.
"""

import json
import select
import sys

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSColor,
    NSDefaultRunLoopMode,
    NSFloatingWindowLevel,
    NSFont,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSTextAlignmentCenter,
    NSTextField,
    NSRunLoop,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSDate


def make_label(frame, size, color, alignment=NSTextAlignmentCenter):
    label = NSTextField.alloc().initWithFrame_(frame)
    label.setAlignment_(alignment)
    label.setFont_(NSFont.systemFontOfSize_(size))
    label.setTextColor_(color)
    label.setBackgroundColor_(NSColor.clearColor())
    label.setBezeled_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setLineBreakMode_(4)
    return label


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
app.finishLaunching()

screen = NSScreen.mainScreen().frame().size
width, height = 520, 72
x = (screen.width - width) / 2
y = 180

win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(x, y, width, height),
    NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
    2,
    False,
)
win.setLevel_(NSFloatingWindowLevel + 1)
win.setOpaque_(True)
win.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.09, 0.09, 0.09, 0.96))
win.setIgnoresMouseEvents_(True)
win.setHasShadow_(True)
win.setHidesOnDeactivate_(False)
win.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 6))
win.contentView().setWantsLayer_(True)
win.contentView().layer().setCornerRadius_(22)
win.contentView().layer().setMasksToBounds_(True)

status_label = make_label(NSMakeRect(18, 43, width - 36, 18), 12.0, NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.7))
preview_label = make_label(NSMakeRect(18, 12, width - 36, 28), 15.0, NSColor.whiteColor())
win.contentView().addSubview_(status_label)
win.contentView().addSubview_(preview_label)
win.orderFrontRegardless()

phase = "listening"
preview = ""
frames = ["Speak", "Speak .", "Speak ..", "Speak ..."]
frame_idx = 0


def render():
    global frame_idx
    if phase == "processing":
        status_label.setStringValue_("Processing")
    elif phase == "error":
        status_label.setStringValue_("Error")
    elif phase == "final":
        status_label.setStringValue_("Done")
    else:
        status_label.setStringValue_(frames[frame_idx % len(frames)])
        frame_idx += 1
    preview_label.setStringValue_(preview)


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
        preview = message.get("text", preview)

    render()
    NSRunLoop.mainRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode,
        NSDate.dateWithTimeIntervalSinceNow_(0.1),
    )
