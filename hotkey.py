#!/usr/bin/env python3
"""Native macOS Right Option hotkey monitor for Bolo."""

import json
import os
import sys
import warnings

from objc import ObjCPointerWarning
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSDefaultRunLoopMode,
    NSEvent,
    NSEventMaskFlagsChanged,
    NSRunLoop,
)
from Foundation import NSDate
from Quartz import CGEventSourceFlagsState, kCGEventSourceStateCombinedSessionState


warnings.filterwarnings("ignore", category=ObjCPointerWarning)


NX_DEVICERALTKEYMASK = 0x00000040
POLL_INTERVAL = 0.02
PARENT_PID = int(os.environ.get("BOLO_PARENT_PID") or "0")

state = False


def emit(event):
    sys.stdout.write(json.dumps({"event": event}) + "\n")
    sys.stdout.flush()


def is_right_option_down():
    try:
        flags = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)
    except Exception:
        return state
    return bool(flags & NX_DEVICERALTKEYMASK)


def set_state(next_state):
    global state
    if next_state == state:
        return
    state = next_state
    emit("press" if state else "release")


def parent_is_alive():
    if PARENT_PID <= 0:
        return True
    try:
        os.kill(PARENT_PID, 0)
    except OSError:
        return False
    return True


def flags_changed(event):
    set_state(bool(event.modifierFlags() & NX_DEVICERALTKEYMASK))


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
app.finishLaunching()

monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
    NSEventMaskFlagsChanged,
    flags_changed,
)
if monitor is None:
    print("[hotkey] NSEvent monitor unavailable", file=sys.stderr, flush=True)

state = is_right_option_down()

while True:
    if not parent_is_alive():
        break
    NSRunLoop.currentRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode,
        NSDate.dateWithTimeIntervalSinceNow_(POLL_INTERVAL),
    )
    set_state(is_right_option_down())
