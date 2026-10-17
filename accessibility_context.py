#!/usr/bin/env python3
"""Return focused text context for Bolo's Rust cleanup path."""

import json
import sys
from datetime import datetime

import ApplicationServices as AX
from AppKit import NSWorkspace
from Foundation import NSMakeRange, NSString

MAX_TEXT_BEFORE_CURSOR = 2048
MAX_FALLBACK_TEXT = 2048
MAX_SELECTED_TEXT = 8000

LOG_FILE = "/tmp/bolo.log"
SECURE_LABEL_KEYWORDS = ("password", "secure")
SECURE_REFUSAL_LOG_LINE = "[accessibility] secure field focused; context withheld"

# Conservative editable roles: the focused element is a paste target when
# its AXRole is one of these. The list stays small on purpose, because a
# false writable verdict silently drops the transcript (the Cmd+V lands
# nowhere), while a false non-writable verdict merely shows the copy
# popup and costs the user one manual paste.
WRITABLE_ROLES = (
    "AXTextArea",
    "AXTextField",
    "AXComboBox",
    "AXTextGroup",
    "AXSearchField",
)


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


# Pids whose Electron/Chromium accessibility already got the manual flag,
# so repeated reads stay cheap. The flag is idempotent and harmless on apps
# that ignore it, so the set only exists to skip redundant attribute writes.
_web_ax_enabled: set = set()


def enable_web_accessibility_once(pid=None):
    """Force Chromium-based apps to expose their accessibility tree.

    Electron apps (OpenChamber, Slack, Discord, Notion, WhatsApp and every
    other Chromium shell) ship with their renderer accessibility OFF until an
    assistive client asks for it. Without the flag the focused element is
    None and every context read comes back empty, which is what produced all
    the `no_words` edit-learning skips and left the per-app cleanup profiles
    blind in exactly the apps people type into most. Setting the private
    `AXManualAccessibility` attribute on the application element turns the
    tree on; Chromium then builds it asynchronously (verified live
    2026-10-17: the OpenChamber chat input resolved to AXTextArea with real
    content about two seconds after the first flag write). The write runs on
    every read for unseen pids and never blocks: the tree is simply missing
    for the read that immediately follows a first-ever flag write.
    """
    try:
        if pid is None:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return
            pid = app.processIdentifier()
        if not pid or pid in _web_ax_enabled:
            return
        _web_ax_enabled.add(pid)
        app_element = AX.AXUIElementCreateApplication(pid)
        AX.AXUIElementSetAttributeValue(
            app_element,
            "AXManualAccessibility",
            AX.kCFBooleanTrue,
        )
    except Exception:
        return


def system_wide_focused_element():
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


def app_level_focused_element():
    """Focused element of the frontmost application, read at the app level.

    Chromium builds its accessibility tree asynchronously after the manual
    flag lands, and the system-wide focused-element query can stay None the
    whole time even while the application element already reports its own
    (verified live 2026-10-17 on OpenChamber). The app-level read is the
    reliable path for those apps and a harmless repeat for native ones.
    """
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        pid = app.processIdentifier()
        if not pid:
            return None
        app_element = AX.AXUIElementCreateApplication(pid)
        error, element = AX.AXUIElementCopyAttributeValue(
            app_element,
            AX.kAXFocusedUIElementAttribute,
            None,
        )
    except Exception:
        return None
    if error != 0:
        return None
    return element


def focused_element():
    enable_web_accessibility_once()
    element = system_wide_focused_element()
    if element is not None:
        return element
    return app_level_focused_element()


def focused_element_pid(element):
    """Process id owning the focused element, or None when unreadable.

    The manual-copy fallback's edit-learning probe pins the popup process
    through this: the pid tells Bolo's runtime which app the caret context
    came from without reading any field content.
    """
    if element is None:
        return None
    try:
        error, pid = AX.AXUIElementGetPid(element, None)
    except Exception:
        return None
    if error != 0:
        return None
    return int(pid)


def is_secure_element(element):
    """Return True when the focused element must be treated as a password field.

    Fail-closed: the exact secure-text-field subrole, or the element's own
    role description, title, description, placeholder, or help text naming a
    password or secure control. Element content (AXValue, AXSelectedText) is
    never scanned, so a normal document that merely mentions passwords still
    returns context.
    """
    subrole = copy_attribute(element, AX.kAXSubroleAttribute)
    if subrole is not None and str(subrole) == str(AX.kAXSecureTextFieldSubrole):
        return True
    for attribute in (
        AX.kAXRoleDescriptionAttribute,
        AX.kAXTitleAttribute,
        AX.kAXDescriptionAttribute,
        AX.kAXPlaceholderValueAttribute,
        AX.kAXHelpAttribute,
    ):
        text = copy_attribute(element, attribute)
        if text is None:
            continue
        lowered = str(text).lower()
        if any(keyword in lowered for keyword in SECURE_LABEL_KEYWORDS):
            return True
    return False


def log_secure_refusal():
    """Append the one-line refusal note to the app log; never include field text."""
    try:
        stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as log_file:
            log_file.write(f"{stamp} {SECURE_REFUSAL_LOG_LINE}\n")
    except Exception:
        pass


def element_accepts_value_writes(element):
    """True when the element accepts kAXValueAttribute set operations.

    Never reads AXValue: AXUIElementIsAttributeSettable only reports
    setability, so this stays safe even next to fields whose value must
    not be read. Kept as its own seam so tests can stub it like
    ``copy_attribute``.
    """
    try:
        error, settable = AX.AXUIElementIsAttributeSettable(
            element, AX.kAXValueAttribute, None
        )
    except Exception:
        return False
    return error == 0 and bool(settable)


def is_writable_element(element):
    """True when the focused element is an editable paste target.

    The verdict feeds the insert-vs-copy decision: ``False`` routes the
    dictation to the manual-copy popup instead of a blind Cmd+V. Kept
    deliberately conservative (the small ``WRITABLE_ROLES`` list, then
    setability of kAXValueAttribute) because over-claiming writable is the
    error that loses text. Secure fields count as non-writable for this
    purpose and are refused before any value attribute is touched, so the
    CONTEXT withholding rules stay untouched.
    """
    if element is None:
        return False
    if is_secure_element(element):
        # A password field accepts typing, but Bolo must not paste into it
        # blind either: count it as non-writable and never read its value.
        return False
    role = copy_attribute(element, AX.kAXRoleAttribute)
    if role is not None and str(role) in WRITABLE_ROLES:
        return True
    return element_accepts_value_writes(element)


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
        if element is not None and is_secure_element(element):
            log_secure_refusal()
            print(json.dumps({"selected": False}))
            return 3
        selected = element is not None and select_text_immediately_before_caret(element, target)
        print(json.dumps({"selected": selected}))
        return 0 if selected else 3

    app_name, bundle_id = frontmost_app()
    element = focused_element()
    secure = element is not None and is_secure_element(element)
    if secure:
        log_secure_refusal()
    context = {
        "app_name": app_name,
        "bundle_id": bundle_id,
        "text_before_cursor": "" if (element is None or secure) else text_before_cursor(element),
        "selected_text": "" if (element is None or secure) else selected_text(element),
        # Paste-target verdict for the insert decision; absent field on a
        # stale helper means "unknown" and the runtime keeps pasting.
        "writable": element is not None and is_writable_element(element),
        # Owner of the focused element (None when nothing is focused or a
        # stale helper predates the field); probes use it to recognize the
        # manual-copy popup's text view without reading field content.
        "focused_pid": focused_element_pid(element),
    }
    print(json.dumps(context, ensure_ascii=True))


if __name__ == "__main__":
    raise SystemExit(main())
