#!/usr/bin/env python3
"""
Standalone overlay process — shows pill indicator while recording.
Launched by bolo.py, killed when recording stops.
"""
import time
from AppKit import (
    NSPanel, NSTextField, NSColor, NSFont, NSMakeRect, NSScreen,
    NSWindowStyleMaskBorderless, NSFloatingWindowLevel,
    NSApplication, NSApplicationActivationPolicyAccessory,
    NSRunLoop, NSDefaultRunLoopMode, NSTextAlignmentCenter,
)
from Foundation import NSDate

app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
app.finishLaunching()
app.activateIgnoringOtherApps_(True)

W = NSScreen.mainScreen().frame().size.width
H = NSScreen.mainScreen().frame().size.height

pw, ph = 160, 40
x = (W - pw) / 2
y = 180

win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(x, y, pw, ph), NSWindowStyleMaskBorderless, 2, False)
win.setLevel_(NSFloatingWindowLevel)
win.setOpaque_(True)
win.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.1, 1.0))
win.setIgnoresMouseEvents_(True)
win.setHasShadow_(True)
win.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 6))

win.contentView().setWantsLayer_(True)
win.contentView().layer().setCornerRadius_(20)
win.contentView().layer().setMasksToBounds_(True)

label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, (ph - 18) / 2 - 1, pw, 20))
label.setAlignment_(NSTextAlignmentCenter)
label.setFont_(NSFont.systemFontOfSize_(14.0))
label.setTextColor_(NSColor.whiteColor())
label.setBackgroundColor_(NSColor.clearColor())
label.setBezeled_(False)
label.setEditable_(False)
label.setSelectable_(False)
win.contentView().addSubview_(label)
win.orderFrontRegardless()

frames = ["●  Speak", "● ● Speak", "● ● ● Speak"]
i = 0
while True:
    label.setStringValue_(frames[i % 3])
    i += 1
    NSRunLoop.mainRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.35))
