#!/usr/bin/env python3
"""Render the polished wizard screens offscreen and write real AppKit PNGs.

Runs the same build_ui the live onboarding window runs, but never orders
the window front and never activates the app: the content view paints
the native window background itself and draws into a bitmap through
cacheDisplay, so each PNG is the genuine AppKit rendering of the actual
view hierarchy, faithful in light and dark. Every payload uses
write_marker false, so nothing touches the onboarding marker.

Usage:
  /usr/bin/python3 scripts/preview-app-window.py OUT_DIR
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app_window  # noqa: E402

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "outputs/bolo-screens"
try:
    os.makedirs(OUT_DIR, exist_ok=True)
except OSError as error:
    print(error, file=sys.stderr)
    sys.exit(1)

SCREENS = list(app_window.SCREEN_ORDER)


def wizard_payload(screen, trust="warn", microphones=2, hotkey="left_option"):
    """Exact runtime payload shape for one wizard screen.

    Facts mirror a realistic first run: missing key, the real runtime
    trust reading (warn means the settings toggle may be ON while the
    actual paste path is still untrusted, which the Accessibility step
    must explain), and a typical microphone count.
    """
    queue = list(app_window.SCREEN_ORDER)
    key_entry = None
    if screen == app_window.SCREEN_CONNECT_SPEECH:
        key_entry = {"index": 0, "placeholder": "Paste your AssemblyAI API key"}
    practice = None
    if screen == app_window.SCREEN_PRACTICE:
        practice = {"placeholder": app_window.WIZARD_PRACTICE_PLACEHOLDER}
    return {
        "mode": "onboarding",
        "title": "Set up Bolo",
        "brand": "BOLO",
        "rows": [],
        "button": "Continue",
        "key_entry": key_entry,
        "practice": practice,
        "wizard": {
            "key_missing": screen == app_window.SCREEN_CONNECT_SPEECH,
            "accessibility_state": trust,
            "microphones": microphones,
            "hotkey": hotkey,
        },
        "screen": screen,
        "wizard_screen": True,
        "screen_count": len(queue),
        "screen_position": 1 + queue.index(screen),
        "practice_done": False,
        "write_marker": False,
    }


def main():
    import Quartz
    from AppKit import NSPNGFileType

    summary = {}
    for screen in SCREENS:
        payload = wizard_payload(screen)
        ui = app_window.build_ui(payload, preview=True)
        window = ui["window"]
        content = window.contentView()
        width = app_window.WIZARD_WIDTH
        height = app_window.WIZARD_HEIGHT
        window.setFrame_display_(Quartz.CGRectMake(0, 0, width, height), False)
        rect = Quartz.CGRectMake(0, 0, width, height)
        image = content.bitmapImageRepForCachingDisplayInRect_(rect)
        content.cacheDisplayInRect_toBitmapImageRep_(rect, image)
        out_path = os.path.join(OUT_DIR, "screen-{0}.png".format(screen))
        data = image.representationUsingType_properties_(NSPNGFileType, {})
        if not data.writeToFile_atomically_(out_path, True):
            raise OSError("could not write preview: " + out_path)
        summary[screen] = {"width": width, "height": height, "png": out_path}
        print(out_path, width, "x", height)
    with open(os.path.join(OUT_DIR, "screen-all.json"), "w") as handle:
        json.dump(summary, handle, indent=2)


if __name__ == "__main__":
    main()
