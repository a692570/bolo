#!/usr/bin/env python3
"""Native macOS hotkey monitor for Bolo."""

import json
import os
import sys
import time
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
from Quartz import (
    CGEventSourceFlagsState,
    kCGEventSourceStateCombinedSessionState,
)

if os.environ.get("BOLO_HOTKEY", "right_option") not in ("right_option", "right_control", "right_shift", "fn"):
    from Quartz import CGEventMaskBit, kCGEventKeyDown, kCGEventKeyUp


warnings.filterwarnings("ignore", category=ObjCPointerWarning)


NX_DEVICERALTKEYMASK = 0x00000040
NX_DEVICERCTLKEYMASK = 0x00002000
NX_DEVICERSHIFTKEYMASK = 0x00000020
NX_SECONDARYFNMASK = 0x00004000
POLL_INTERVAL = 0.02
STALE_THRESHOLD = 2.0
PARENT_PID = int(os.environ.get("BOLO_PARENT_PID") or "0")

HOTKEY = os.environ.get("BOLO_HOTKEY", "right_option")

KEYCODE_MAP = {
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
    "f13": 105,
    "f14": 107,
    "f15": 113,
    "f16": 106,
    "f17": 64,
    "f18": 79,
    "f19": 80,
    "caps_lock": 57,
}

TARGET_KEYCODE = KEYCODE_MAP.get(HOTKEY)
USE_FLAGS_CHANGED = HOTKEY in ("right_option", "right_control", "right_shift", "fn")
USE_KEY_EVENTS = TARGET_KEYCODE is not None and not USE_FLAGS_CHANGED

state = False
state_since = None


def emit(event):
    sys.stdout.write(json.dumps({"event": event}) + "\n")
    sys.stdout.flush()


def is_hotkey_down():
    try:
        flags = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState)
    except Exception:
        return state

    if HOTKEY == "right_option":
        return bool(flags & NX_DEVICERALTKEYMASK)
    elif HOTKEY == "right_control":
        return bool(flags & NX_DEVICERCTLKEYMASK)
    elif HOTKEY == "right_shift":
        return bool(flags & NX_DEVICERSHIFTKEYMASK)
    elif HOTKEY == "fn":
        return bool(flags & NX_SECONDARYFNMASK)

    return state


def set_state(next_state):
    global state, state_since
    if next_state == state:
        return
    state = next_state
    state_since = time.monotonic() if state else None
    emit("press" if state else "release")


def parent_is_alive():
    if PARENT_PID <= 0:
        return True
    try:
        os.kill(PARENT_PID, 0)
    except OSError:
        return False
    return True


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
app.finishLaunching()

if USE_FLAGS_CHANGED:

    def flags_changed(event):
        if HOTKEY == "right_option":
            set_state(bool(event.modifierFlags() & NX_DEVICERALTKEYMASK))
        elif HOTKEY == "right_control":
            set_state(bool(event.modifierFlags() & NX_DEVICERCTLKEYMASK))
        elif HOTKEY == "right_shift":
            set_state(bool(event.modifierFlags() & NX_DEVICERSHIFTKEYMASK))
        elif HOTKEY == "fn":
            set_state(bool(event.modifierFlags() & NX_SECONDARYFNMASK))

    monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        NSEventMaskFlagsChanged,
        flags_changed,
    )
    if monitor is None:
        print("[hotkey] NSEvent monitor unavailable", file=sys.stderr, flush=True)

elif USE_KEY_EVENTS:
    NSKeyDown = 10

    key_mask = CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventKeyUp)

    def key_event(event):
        if event.keyCode() == TARGET_KEYCODE:
            set_state(event.type() == NSKeyDown)

    monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        key_mask,
        key_event,
    )
    if monitor is None:
        print("[hotkey] NSEvent monitor unavailable", file=sys.stderr, flush=True)

else:
    print(f"[hotkey] unknown hotkey: {HOTKEY}", file=sys.stderr, flush=True)
    sys.exit(1)

state = is_hotkey_down()

while True:
    if not parent_is_alive():
        break
    NSRunLoop.currentRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode,
        NSDate.dateWithTimeIntervalSinceNow_(POLL_INTERVAL),
    )
    if USE_FLAGS_CHANGED:
        actual = is_hotkey_down()
        if state and not actual and state_since is not None:
            if time.monotonic() - state_since > STALE_THRESHOLD:
                print("[hotkey] stale state detected, forcing release", file=sys.stderr, flush=True)
                set_state(False)
                continue
        set_state(actual)
