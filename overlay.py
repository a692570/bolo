#!/usr/bin/env python3
"""
Standalone overlay process for Bolo.
Receives JSON messages on stdin to update phase and preview text.
"""

import json
import select
import sys
import time

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
    NSViewWidthSizable,
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
    label.setUsesSingleLineMode_(False)
    label.setAllowsDefaultTighteningForTruncation_(False)
    label.setAutoresizingMask_(NSViewWidthSizable)
    return label


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
app.finishLaunching()

screen = NSScreen.mainScreen().frame().size
MIN_WIDTH = 460
MAX_WIDTH = 760
MIN_HEIGHT = 76
MAX_HEIGHT = 196
H_PADDING = 18
TOP_PADDING = 14
BOTTOM_PADDING = 14
STATUS_HEIGHT = 18
STATUS_GAP = 8
DEFAULT_WIDTH = 520
DEFAULT_HEIGHT = 76
width, height = DEFAULT_WIDTH, DEFAULT_HEIGHT
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

status_label = make_label(
    NSMakeRect(H_PADDING, height - TOP_PADDING - STATUS_HEIGHT, width - (H_PADDING * 2), STATUS_HEIGHT),
    12.0,
    NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.7),
)
preview_label = make_label(
    NSMakeRect(H_PADDING, BOTTOM_PADDING, width - (H_PADDING * 2), 30),
    15.0,
    NSColor.whiteColor(),
)
win.contentView().addSubview_(status_label)
win.contentView().addSubview_(preview_label)
win.orderFrontRegardless()

phase = "listening"
preview = ""
listening_frames = ["Listening", "Listening .", "Listening ..", "Listening ..."]
transcribing_frames = ["Transcribing", "Transcribing .", "Transcribing ..", "Transcribing ..."]
inserting_frames = ["Inserting", "Inserting .", "Inserting ..", "Inserting ..."]
frame_idx = 0


def resize_for_text(text):
    content = (text or "").strip()
    target_width = DEFAULT_WIDTH
    if content:
        target_width = min(MAX_WIDTH, max(MIN_WIDTH, 460 + max(0, len(content) - 48) * 4))
    preview_width = target_width - (H_PADDING * 2)
    preview_label.setFrame_(NSMakeRect(H_PADDING, 0, preview_width, 1000))
    preview_label.setStringValue_(content)
    preview_label.sizeToFit()
    preview_height = max(30, preview_label.frame().size.height)
    preview_height = min(preview_height, MAX_HEIGHT - TOP_PADDING - BOTTOM_PADDING - STATUS_HEIGHT - STATUS_GAP)
    target_height = min(MAX_HEIGHT, max(MIN_HEIGHT, TOP_PADDING + STATUS_HEIGHT + STATUS_GAP + preview_height + BOTTOM_PADDING))
    frame = win.frame()
    x = (screen.width - target_width) / 2
    new_frame = NSMakeRect(x, frame.origin.y, target_width, target_height)
    win.setFrame_display_(new_frame, True)
    status_label.setFrame_(NSMakeRect(H_PADDING, target_height - TOP_PADDING - STATUS_HEIGHT, preview_width, STATUS_HEIGHT))
    preview_label.setFrame_(NSMakeRect(H_PADDING, BOTTOM_PADDING, preview_width, preview_height))


# Status label colors per phase
_COLOR_LISTENING = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.76, 1.0, 0.9)   # light blue
_COLOR_TRANSCRIBING = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.76, 0.33, 0.9)  # amber
_COLOR_INSERTING = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.65, 1.0, 0.9)     # blue
_COLOR_SUCCESS = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.30, 0.85, 0.50, 0.9)     # green
_COLOR_ERROR = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.35, 0.35, 0.9)        # red
_COLOR_FINAL = NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.7)                              # dim white


def render():
    global frame_idx
    if phase == "transcribing":
        status_label.setStringValue_(transcribing_frames[frame_idx % len(transcribing_frames)])
        status_label.setTextColor_(_COLOR_TRANSCRIBING)
        frame_idx += 1
    elif phase == "processing":
        # Legacy alias: treat as transcribing
        status_label.setStringValue_(transcribing_frames[frame_idx % len(transcribing_frames)])
        status_label.setTextColor_(_COLOR_TRANSCRIBING)
        frame_idx += 1
    elif phase == "inserting":
        status_label.setStringValue_(inserting_frames[frame_idx % len(inserting_frames)])
        status_label.setTextColor_(_COLOR_INSERTING)
        frame_idx += 1
    elif phase == "success":
        status_label.setStringValue_("✓ Inserted")
        status_label.setTextColor_(_COLOR_SUCCESS)
    elif phase == "error":
        status_label.setStringValue_("Error")
        status_label.setTextColor_(_COLOR_ERROR)
    elif phase == "final":
        status_label.setStringValue_("Done")
        status_label.setTextColor_(_COLOR_FINAL)
    else:
        # listening (default)
        status_label.setStringValue_(listening_frames[frame_idx % len(listening_frames)])
        status_label.setTextColor_(_COLOR_LISTENING)
        frame_idx += 1
    resize_for_text(preview)


import os

STALL_TIMEOUT = 30.0  # Exit if no messages for 30s (parent probably died)
last_message_at = time.time()

while True:
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        line = sys.stdin.readline()
        if line == "":
            # Stdin closed - parent died, exit immediately
            break
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            message = {}
        phase = message.get("phase", phase)
        preview = message.get("text", preview)
        last_message_at = time.time()

    # Auto-exit if parent stopped sending updates (overlay stuck prevention)
    if time.time() - last_message_at > STALL_TIMEOUT:
        break

    render()
    NSRunLoop.mainRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode,
        NSDate.dateWithTimeIntervalSinceNow_(0.1),
    )

# Clean exit - close window
win.orderOut_(None)
os._exit(0)
