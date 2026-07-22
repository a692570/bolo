#!/usr/bin/env python3
"""Return focused text context for Bolo's Rust cleanup path."""

import json
import sys

import ApplicationServices as AX
from AppKit import NSWorkspace
from Foundation import NSMakeRange, NSString

MAX_TEXT_BEFORE_CURSOR = 500
MAX_FALLBACK_TEXT = 500
MAX_SELECTED_TEXT = 8000


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


def target_range_before_caret(value, location, selection_length, target):
    if selection_length != 0:
        return None
    target = target.strip()
    if not target:
        return None

    value_ns = NSString.stringWithString_(str(value))
    target_ns = NSString.stringWithString_(target)
    value_length = int(value_ns.length())
    target_length = int(target_ns.length())
    caret = int(location)
    if caret < 0 or caret > value_length:
        return None

    trailing_length = 0
    if caret > 0 and str(value_ns.characterAtIndex_(caret - 1)).isspace():
        caret -= 1
        trailing_length = 1
    if caret < target_length:
        return None

    start = caret - target_length
    candidate = str(value_ns.substringWithRange_(NSMakeRange(start, target_length)))
    if candidate != target:
        return None
    return start, target_length + trailing_length


def select_text_immediately_before_caret(element, target):
    selection = selected_range(element)
    value = copy_attribute(element, AX.kAXValueAttribute)
    if selection is None or value is None:
        return False
    location, length = selection
    target_range = target_range_before_caret(value, location, length, target)
    if target_range is None:
        return False
    try:
        range_value = AX.AXValueCreate(
            AX.kAXValueCFRangeType,
            NSMakeRange(*target_range),
        )
        error = AX.AXUIElementSetAttributeValue(
            element,
            AX.kAXSelectedTextRangeAttribute,
            range_value,
        )
    except Exception:
        return False
    return error == 0


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


def selected_text(element):
    value = copy_attribute(element, AX.kAXSelectedTextAttribute)
    if value is not None:
        text = str(value).strip()
        if text:
            return text[:MAX_SELECTED_TEXT]

    selection = selected_range(element)
    if selection is None:
        return ""
    location, length = selection
    text = string_for_range(element, location, length).strip()
    if text:
        return text[:MAX_SELECTED_TEXT]

    value = copy_attribute(element, AX.kAXValueAttribute)
    if value is None:
        return ""
    full_text = str(value)
    if length <= 0 or location >= len(full_text):
        return ""
    return full_text[location : location + length].strip()[:MAX_SELECTED_TEXT]


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--select-before-caret":
        element = focused_element()
        target = sys.stdin.read()
        selected = element is not None and select_text_immediately_before_caret(element, target)
        print(json.dumps({"selected": selected}))
        return 0 if selected else 3

    app_name, bundle_id = frontmost_app()
    element = focused_element()
    context = {
        "app_name": app_name,
        "bundle_id": bundle_id,
        "text_before_cursor": text_before_cursor(element) if element is not None else "",
        "selected_text": selected_text(element) if element is not None else "",
    }
    print(json.dumps(context, ensure_ascii=True))


if __name__ == "__main__":
    raise SystemExit(main())
