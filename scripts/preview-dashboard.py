#!/usr/bin/env python3
"""Render the dashboard offscreen and write real AppKit PNGs.

Runs dashboard_window.build_dashboard_ui in preview mode (the window is
never ordered front, the app never activates), then cacheDisplay draws
the genuine view hierarchy into a bitmap, faithful in light and dark.
Every payload is explicitly synthetic fixture data and carries
write_marker false, so no onboarding marker, env, or clipboard is ever
touched and no live window opens.

Usage: /usr/bin/python3 scripts/preview-dashboard.py OUT_DIR
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dashboard_window  # noqa: E402

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "outputs/dashboard-previews"

LONG_TEXT = (
    "For tomorrow's planning session, let's keep the agenda short and leave "
    "time for questions. We should agree on the priorities first, then decide "
    "who owns each task. Send the notes before the meeting so everyone has "
    "time to read them and add anything we missed. " * 12
).strip()

# Stable synthetic October2026 timestamps, newest first, anchored in UTC.
from datetime import datetime, timedelta, timezone
_SAMPLE_TIME = datetime(2026, 10, 2, 20, 45, tzinfo=timezone.utc)
TS = [int((_SAMPLE_TIME - timedelta(hours=5 * i)).timestamp() * 1000)
      for i in range(10)]

HISTORY = [
    {
        "text": "We can move the design review to Thursday. I will send the updated notes beforehand.",
        "raw": "we can move the design review to Thursday I will send the updated notes beforehand",
        "created_at_ms": TS[0],
        "edited_after_insert": True,
    },
    {
        "text": "Send the invoice to the printer before noon.",
        "raw": "send the invoice to the printer before noon",
        "created_at_ms": TS[1],
        "edited_after_insert": False,
    },
    {
        "text": LONG_TEXT,
        "raw": LONG_TEXT,
        "created_at_ms": TS[2],
        "edited_after_insert": False,
    },
    {
        "text": "Standing notes for the Monday sync, keep the agenda short.",
        "raw": "standing notes for the monday sync, keep the agenda short",
        "created_at_ms": TS[3],
        "edited_after_insert": False,
    },
    {
        "text": "Draft reply: thanks for the review, I will ship the fix today.",
        "raw": "draft reply thanks for the review i will ship the fix today",
        "created_at_ms": TS[4],
        "edited_after_insert": True,
    },
]

TEN_HISTORY = HISTORY + [
    {
        "text": "Quick list for groceries, coffee, oats, and lemons.",
        "raw": "quick list for groceries coffee oats and lemons",
        "created_at_ms": TS[5],
        "edited_after_insert": False,
    },
    {
        "text": "Note the bug reproduces only after the cache is warm.",
        "raw": "note the bug reproduces only after the cache is warm",
        "created_at_ms": TS[6],
        "edited_after_insert": False,
    },
    {
        "text": "Remember to send the signed form back this week.",
        "raw": "remember to send the signed form back this week",
        "created_at_ms": TS[7],
        "edited_after_insert": False,
    },
    {
        "text": "Half the team prefers the Thursday standup time.",
        "raw": "half the team prefers the thursday standup time",
        "created_at_ms": TS[8],
        "edited_after_insert": False,
    },
    {
        "text": "Try the new cleanup mode on the weekly report.",
        "raw": "try the new cleanup mode on the weekly report",
        "created_at_ms": TS[9],
        "edited_after_insert": False,
    },
]


def fixture(history=None, accessibility="ok"):
    """Explicitly synthetic dashboard payload; nothing here is real data."""
    return {
        "mode": "dashboard",
        "title": "Bolo",
        "write_marker": False,
        "dashboard": {
            "version": "1.9.6",
            "usage": {
                "dictations": 244,
                "words": 15230,
                "recording_ms": 9000000,
                "started_at_ms": int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000),
            },
            "hotkey": "left_option",
            "microphone": "default",
            "microphones": ["MacBook Pro Microphone", "Studio Display Mic"],
            "cleanup_mode": "auto",
            "accessibility_state": accessibility,
            "provider": "AssemblyAI",
            "history_limit": 10,
            "saved_dictations": len(HISTORY if history is None else history),
            "saved_words": sum(len(item["text"].split()) for item in (HISTORY if history is None else history)),
            "learned_words_count": 4,
            "history": HISTORY if history is None else history,
        },
    }


def shots():
    return [
        ("home", fixture(), "home", False),
        ("home-warn", fixture(accessibility="warn"), "home", False),
        ("home-empty", fixture(history=[]), "home", False),
        ("dictations", fixture(), "dictations", False),
        ("dictations-empty", fixture(history=[]), "dictations", False),
        ("dictations-long", fixture(history=HISTORY), "dictations", False),
        ("dictations-long-bottom", fixture(history=HISTORY), "dictations", False),
        ("dictations-ten", fixture(history=TEN_HISTORY), "dictations", False),
        ("settings", fixture(), "settings", False),
    ]


def main():
    from AppKit import NSPNGFileType
    import Quartz

    os.makedirs(OUT_DIR, exist_ok=True)
    summary = {}
    for name, payload, tab, dark in shots():
        for appearance in ("light", "dark"):
            ui = dashboard_window.build_dashboard_ui(payload, preview=True, dark=(appearance == "dark"))
            refs = ui["refs"]
            dashboard_window.select_tab(refs, tab)
            if name.startswith("dictations-long"):
                # Select the long transcript (third entry, index 2) so the
                # shot actually proves scrollable long content, and show
                # the raw variant for contrast.
                refs["selected_index"] = 2
                refs["show_raw"] = True
                dashboard_window.select_tab(refs, tab)
            if name == "dictations-long-bottom":
                from Foundation import NSMakeRange
                view = refs["detail_scroll"].documentView()
                view.scrollRangeToVisible_(NSMakeRange(len(view.string()) - 1, 1))
            window = ui["window"]
            content = window.contentView()
            width, height = dashboard_window.WIDTH, dashboard_window.HEIGHT
            # Render the actual content view bounds, not the outer
            # window frame: the frame includes the title bar, so forcing
            # it to 940x650 shrank the content and left a black band.
            content.setBoundsSize_((width, height))
            bounds = content.bounds()
            width, height = bounds.size.width, bounds.size.height
            rect = Quartz.CGRectMake(0, 0, width, height)
            image = content.bitmapImageRepForCachingDisplayInRect_(rect)
            content.cacheDisplayInRect_toBitmapImageRep_(rect, image)
            out_path = os.path.join(OUT_DIR, "{0}-{1}.png".format(name, appearance))
            data = image.representationUsingType_properties_(NSPNGFileType, {})
            if not data.writeToFile_atomically_(out_path, True):
                raise OSError("could not write preview: " + out_path)
            key = "{0}-{1}".format(name, appearance)
            summary[key] = {"width": width, "height": height, "png": out_path,
                            "tab": tab, "dark": appearance == "dark"}
            print(out_path, width, "x", height)
    with open(os.path.join(OUT_DIR, "previews.json"), "w") as handle:
        json.dump(summary, handle, indent=2)


if __name__ == "__main__":
    main()
