#!/usr/bin/env python3
"""Return focused text context for Bolo's Rust cleanup path."""

import json

import ApplicationServices as AX
from AppKit import NSWorkspace
from Foundation import NSMakeRange

MAX_TEXT_BEFORE_CURSOR = 500
MAX_FALLBACK_TEXT = 500


def frontmost_app():
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return "", ""
        return str(app.localizedName() or ""), str(app.bundleIdentifier() or "")
    except Exception:
        return "", ""


def copy_attribute(element, attribute):
    try:
        error, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    except Exception:
        return None
    if error != 0:
        return None
    return value


def selected_range(element):
    value = copy_attribute(element, AX.kAXSelectedTextRangeAttribute)
    if value is None:
        return None
    try:
        ok, result = AX.AXValueGetValue(value, AX.kAXValueCFRangeType, None)
    except Exception:
        return None
    if not ok:
        return None
    location, length = result
    return max(0, int(location)), max(0, int(length))


def string_for_range(element, location, length):
    if length <= 0:
        return ""
    try:
        range_value = AX.AXValueCreate(AX.kAXValueCFRangeType, NSMakeRange(location, length))
        error, text = AX.AXUIElementCopyParameterizedAttributeValue(
            element,
            AX.kAXStringForRangeParameterizedAttribute,
            range_value,
            None,
        )
    except Exception:
        return ""
    if error != 0 or text is None:
        return ""
    return str(text)


def focused_element():
    try:
        system = AX.AXUIElementCreateSystemWide()
        error, element = AX.AXUIElementCopyAttributeValue(
            system,
            AX.kAXFocusedUIElementAttribute,
            None,
        )
    except Exception:
        return None
    if error != 0:
        return None
    return element


def text_before_cursor(element):
    selection = selected_range(element)
    if selection is not None:
        location, _length = selection
        start = max(0, location - MAX_TEXT_BEFORE_CURSOR)
        text = string_for_range(element, start, location - start)
        if text:
            return text[-MAX_TEXT_BEFORE_CURSOR:].strip()

    value = copy_attribute(element, AX.kAXValueAttribute)
    if value is None:
        return ""
    text = str(value)
    return text[-MAX_FALLBACK_TEXT:].strip()


def main():
    app_name, bundle_id = frontmost_app()
    element = focused_element()
    context = {
        "app_name": app_name,
        "bundle_id": bundle_id,
        "text_before_cursor": text_before_cursor(element) if element is not None else "",
    }
    print(json.dumps(context, ensure_ascii=True))


if __name__ == "__main__":
    main()
