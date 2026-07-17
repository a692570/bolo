#!/usr/bin/env python3
"""First-run onboarding dialog for Bolo hotkey selection."""

import json
import os
import sys

HOTKEY_CHOICES = [
    ("right_option", "Right Option", "Best for MacBook built-in keyboards."),
    ("right_control", "Right Control", "Best for external and Windows keyboards."),
    ("f19", "F19", "Best for mechanical keyboards. Rarely used by other apps."),
    ("caps_lock", "Caps Lock", "Hold to dictate. Tap still toggles caps."),
]
SUPPORTED_HOTKEYS = {
    "right_option",
    "right_control",
    "right_shift",
    "fn",
    "caps_lock",
    *(f"f{number}" for number in range(1, 20)),
}

ENV_FILE = os.path.expanduser("~/.bolo/env")


def _find_icon():
    root = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(root, "bolo-icon.png"),
        os.path.join(root, "docs", "assets", "bolo-icon.png"),
        os.path.join(root, "icon_options", "bolo_mic.png"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def show_dialog():
    try:
        from AppKit import (
            NSAlert,
            NSAlertStyleInformational,
            NSApp,
            NSApplication,
            NSImage,
            NSPopUpButton,
            NSMakeRect,
        )
    except ImportError:
        return _fallback_prompt()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)

    alert = NSAlert.alloc().init()
    alert.setMessageText_("Welcome to Bolo")
    alert.setInformativeText_(
        "Pick the key you'll hold to dictate.\n\n"
        "Recommendations based on your keyboard:\n\n"
        "  Right Option - Best for MacBook built-in keyboards\n"
        "  Right Control - Best for external and Windows keyboards\n"
        "  F19 - Best for mechanical keyboards\n"
        "  Caps Lock - Hold to dictate, tap still toggles caps\n\n"
        "You can change this anytime in ~/.bolo/env"
    )
    alert.setAlertStyle_(NSAlertStyleInformational)

    icon_path = _find_icon()
    if icon_path:
        image = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image:
            image.setSize_((64, 64))
            alert.setIcon_(image)

    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(0, 0, 320, 28), False
    )
    for _, name, _ in HOTKEY_CHOICES:
        popup.addItemWithTitle_(name)
    popup.selectItemAtIndex_(0)
    alert.setAccessoryView_(popup)

    alert.addButtonWithTitle_("OK")
    alert.addButtonWithTitle_("Use Default")

    response = alert.runModal()
    if response != 1000:
        return "right_option"

    idx = popup.indexOfSelectedItem()
    if 0 <= idx < len(HOTKEY_CHOICES):
        return HOTKEY_CHOICES[idx][0]
    return "right_option"


def _fallback_prompt():
    print("\n=== Welcome to Bolo ===\n")
    print("Pick the key you'll hold to dictate.\n")
    print("Recommendations based on your keyboard:\n")
    for i, (_, name, desc) in enumerate(HOTKEY_CHOICES):
        print(f"  {i + 1}. {name} - {desc}")
    print()
    while True:
        choice = input("Enter a number (1-4), or type a supported key name: ").strip()
        if not choice:
            return "right_option"
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(HOTKEY_CHOICES):
                return HOTKEY_CHOICES[idx][0]
        normalized = choice.lower().replace(" ", "_")
        if normalized in SUPPORTED_HOTKEYS:
            return normalized
        print("Unsupported hotkey. Use right_option, right_control, right_shift, fn, f1-f19, or caps_lock.")


def save_hotkey(hotkey):
    if hotkey not in SUPPORTED_HOTKEYS:
        raise ValueError(f"unsupported hotkey: {hotkey}")
    env_dir = os.path.dirname(ENV_FILE)
    os.makedirs(env_dir, mode=0o700, exist_ok=True)
    os.chmod(env_dir, 0o700)
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            lines = f.readlines()
    new_lines = []
    found = False
    for line in lines:
        if line.strip().startswith("BOLO_HOTKEY="):
            new_lines.append(f"BOLO_HOTKEY={hotkey}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"BOLO_HOTKEY={hotkey}\n")
    temp_file = f"{ENV_FILE}.tmp"
    with open(temp_file, "w") as f:
        f.writelines(new_lines)
    os.chmod(temp_file, 0o600)
    os.replace(temp_file, ENV_FILE)


def main():
    configured_hotkey = os.environ.get("BOLO_HOTKEY")
    if configured_hotkey:
        if configured_hotkey in SUPPORTED_HOTKEYS:
            sys.exit(0)
        raise SystemExit(f"Unsupported BOLO_HOTKEY: {configured_hotkey}")

    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                if line.strip().startswith("BOLO_HOTKEY="):
                    value = line.split("=", 1)[1].strip().strip('"\'')
                    if value in SUPPORTED_HOTKEYS:
                        sys.exit(0)

    try:
        hotkey = show_dialog()
    except Exception:
        hotkey = _fallback_prompt()

    save_hotkey(hotkey)
    print(f"[onboarding] hotkey set to: {hotkey}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
