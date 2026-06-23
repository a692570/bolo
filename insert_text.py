#!/usr/bin/env python3
"""Paste text while preserving the full macOS pasteboard payload."""

import os
import select
import sys
import time
import warnings

from objc import ObjCPointerWarning
from AppKit import NSPasteboard, NSPasteboardItem, NSStringPboardType
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)


warnings.filterwarnings("ignore", category=ObjCPointerWarning)


RESTORE_TIMEOUT = float(os.environ.get("BOLO_INSERT_RESTORE_TIMEOUT", "0.35"))


def snapshot_pasteboard(pasteboard):
    items = []
    for item in pasteboard.pasteboardItems() or []:
        data_by_type = {}
        for item_type in item.types() or []:
            data = item.dataForType_(item_type)
            if data is not None:
                data_by_type[item_type] = bytes(data)
        if data_by_type:
            items.append(data_by_type)
    return items


def restore_pasteboard(pasteboard, snapshot):
    pasteboard.clearContents()
    restored = []
    for data_by_type in snapshot:
        item = NSPasteboardItem.alloc().init()
        for item_type, data in data_by_type.items():
            item.setData_forType_(data, item_type)
        restored.append(item)
    if restored:
        pasteboard.writeObjects_(restored)


def post_cmd_v():
    key_code_v = 9
    down = CGEventCreateKeyboardEvent(None, key_code_v, True)
    up = CGEventCreateKeyboardEvent(None, key_code_v, False)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)


def wait_for_pasteboard_to_change(pasteboard, temporary_change_count, text):
    deadline = time.monotonic() + RESTORE_TIMEOUT
    while time.monotonic() < deadline:
        if pasteboard.changeCount() != temporary_change_count:
            current = pasteboard.stringForType_(NSStringPboardType)
            if current != text:
                return True
        time.sleep(0.025)
    return False


def main():
    text = sys.stdin.read()
    if not text:
        return 0

    pasteboard = NSPasteboard.generalPasteboard()
    snapshot = snapshot_pasteboard(pasteboard)
    pasteboard.clearContents()
    if not pasteboard.setString_forType_(text, NSStringPboardType):
        return 2

    temporary_change_count = pasteboard.changeCount()
    post_cmd_v()

    externally_changed = wait_for_pasteboard_to_change(
        pasteboard,
        temporary_change_count,
        text,
    )
    if not externally_changed:
        restore_pasteboard(pasteboard, snapshot)

    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        sys.stdin.read()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
