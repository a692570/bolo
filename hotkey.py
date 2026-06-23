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
    NSEventMaskKeyDown,
    NSRunLoop,
)
from Foundation import NSDate
from Quartz import (
    CGEventSourceFlagsState,
    CGEventSourceKeyState,
    kCGEventSourceStateCombinedSessionState,
)

if os.environ.get("BOLO_HOTKEY", "right_option") not in (
    "right_option",
    "right_control",
    "right_shift",
    "fn",
):
    from Quartz import CGEventMaskBit, kCGEventKeyDown, kCGEventKeyUp


warnings.filterwarnings("ignore", category=ObjCPointerWarning)


NX_DEVICERALTKEYMASK = 0x00000040
NX_DEVICERCTLKEYMASK = 0x00002000
NX_DEVICERSHIFTKEYMASK = 0x00000020
NX_SECONDARYFNMASK = 0x00004000
POLL_INTERVAL = 0.02
RECHECK_INTERVAL = POLL_INTERVAL
RECHECK_REQUIRED_SAMPLES = int(os.environ.get("BOLO_HOTKEY_RECHECK_SAMPLES") or "4")
PARENT_PID = int(os.environ.get("BOLO_PARENT_PID") or "0")

HOTKEY = os.environ.get("BOLO_HOTKEY", "right_option")
ACTION = os.environ.get("BOLO_HOTKEY_ACTION", "dictation")

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
last_recheck_at = 0.0
pending_recheck_state = None
pending_recheck_count = 0


def emit(event):
    sys.stdout.write(json.dumps({"event": event}) + "\n")
    sys.stdout.flush()


def emit_post_insert_edit(action):
    sys.stdout.write(json.dumps({"event": "post_insert_edit", "action": action}) + "\n")
    sys.stdout.flush()


def key_down(event):
    key_code = int(event.keyCode())
    flags = int(event.modifierFlags())
    command_down = bool(flags & (1 << 20))
    if key_code == 51:
        emit_post_insert_edit("backspace")
    elif key_code == 0 and command_down:
        emit_post_insert_edit("cmd_a")


def is_hotkey_down():
    if USE_FLAGS_CHANGED:
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

    if TARGET_KEYCODE is not None:
        try:
            return bool(
                CGEventSourceKeyState(
                    kCGEventSourceStateCombinedSessionState,
                    TARGET_KEYCODE,
                )
            )
        except Exception:
            return state

    return state


def set_state(next_state):
    global pending_recheck_count, pending_recheck_state, state
    if next_state == state:
        return
    state = next_state
    pending_recheck_state = None
    pending_recheck_count = 0
    if ACTION == "paste_last":
        if state:
            emit("paste_last")
        return
    emit("press" if state else "release")


def recheck_os_state():
    global last_recheck_at, pending_recheck_count, pending_recheck_state
    now = time.monotonic()
    if now - last_recheck_at < RECHECK_INTERVAL:
        return
    last_recheck_at = now

    actual = is_hotkey_down()
    if actual == state:
        pending_recheck_state = None
        pending_recheck_count = 0
        return

    if pending_recheck_state != actual:
        pending_recheck_state = actual
        pending_recheck_count = 1
        return

    pending_recheck_count += 1
    if pending_recheck_count < RECHECK_REQUIRED_SAMPLES:
        return

    event = "press" if actual else "release"
    print(
        f"[hotkey] OS recheck corrected missed {event} after {pending_recheck_count} samples",
        file=sys.stderr,
        flush=True,
    )
    set_state(actual)


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

edit_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
    NSEventMaskKeyDown,
    key_down,
)
if edit_monitor is None:
    print("[hotkey] key-down monitor unavailable", file=sys.stderr, flush=True)

while True:
    if not parent_is_alive():
        break
    NSRunLoop.currentRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode,
        NSDate.dateWithTimeIntervalSinceNow_(POLL_INTERVAL),
    )
    recheck_os_state()
