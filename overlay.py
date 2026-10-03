#!/usr/bin/env python3
"""Native macOS status pill overlay for Bolo.

Compact nonactivating panel near the bottom of the main display's safe
area: it never takes keyboard focus, so the user keeps typing or holding
the dictation key while it is visible. Every state has a clear label
with a colored cue dot; dictating also shows the live partial transcript
as a secondary line when one is available. The pill wears the shared
warm palette from bolo_brand (ink surface, ivory text, clay accents,
moss for success, the failure red for retry) and carries the same
custom b mark the setup window shows, so dictation and setup read as
one product.

Protocol: one JSON object per stdin line with "phase" and optional
"text". After STALL_TIMEOUT seconds without a message the overlay
closes itself, so a dead runtime never leaves a stale pill on screen.
"""

import json
import select
import sys
import time
import warnings

import bolo_brand
from objc import ObjCPointerWarning


warnings.filterwarnings("ignore", category=ObjCPointerWarning)


MIN_WIDTH = 240
MAX_WIDTH = 460
HEIGHT = 44
TRANSCRIPT_LINE_H = 14
BOTTOM_MARGIN = 220
DOT_SIZE = 8
STALL_TIMEOUT = 45.0
MARK_SIZE = 20
MARK_X = 16
TEXT_X = 46


def phase_palette(dark=True):
    """Semantic brand colors for the pill surface, always on the warm
    ink palette from bolo_brand so the cue colors match setup."""
    return bolo_brand.palette(dark=dark)


# Phase labels and cue colors. "listening" shares the dictating cue so
# older runtime payloads keep rendering the same way; every runtime
# phase has an explicit label so the pill never shows a blank line.
PHASES = {
    "dictating": ("Dictating", ("accent",)),
    "listening": ("Dictating", ("accent",)),
    "connecting": ("Connecting", ("muted",)),
    "thinking": ("Thinking", ("muted",)),
    "transcribing": ("Thinking", ("muted",)),
    "processing": ("Thinking", ("muted",)),
    "inserting": ("Inserting", ("accent",)),
    "inserted": ("Inserted", ("success",)),
    "copied": ("Inserted", ("success",)),
    "success": ("Inserted", ("success",)),
    "final": ("Done", ("muted",)),
    "error": ("Try again", ("error",)),
}


def phase_color(phase, dark=True):
    """Native cue color for one phase, from the shared brand palette."""
    key = PHASES.get(phase, PHASES["dictating"])[1][0]
    return bolo_brand.native_color(phase_palette(dark=dark)[key])


def phase_label(phase):
    return PHASES.get(phase, PHASES["dictating"])[0]


def preview_text(value):
    """One plain line for the optional live transcript tail."""
    text = " ".join(str(value or "").split())
    if len(text) > 56:
        return "..." + text[-53:]
    return text


def is_error_phase(phase):
    return PHASES.get(phase, PHASES["dictating"])[1] == PHASES["error"][1]


def pill_width(preview, phase):
    """Width for one state: long live previews clamp, short labels stay
    compact instead of stretching to the maximum width."""
    text = preview_text(preview) if phase == "dictating" else ""
    label = phase_label(phase)
    measured = max(len(text), len(label))
    width = MIN_WIDTH + measured * 6
    return max(MIN_WIDTH, min(MAX_WIDTH, width))


# --- Pure placement geometry (no AppKit import needed to test) ---
#
# Frames are plain (x, y, width, height) tuples in the same global
# bottom-left coordinate space NSScreen.frame()/visibleFrame() and
# NSEvent.mouseLocation() use, so multi-monitor layouts (secondary
# display to the left with negative x origin, vertically stacked
# monitors, a Dock eating the bottom of a visibleFrame) are unit
# testable without a live display.


def frame_contains(frame, px, py):
    """True when the point lies inside the open frame rectangle."""
    x, y, width, height = frame
    return x <= px < x + width and y <= py < y + height


def choose_screen(mouse_x, mouse_y, screens):
    """Pick the screen whose frame contains the mouse pointer.

    Adapted in spirit from OpenSuperWhisper's
    Utils/FocusUtils.swift chooseIndicatorPoint(mouseposition:): the
    current mouse position is the fastest cue for the display the user
    is actively working on, with no AX caret queries and no extra
    permissions. Callers pass the screens main-screen-first, so when no
    frame contains the point the first (main) screen wins as the
    fallback; None means no screens at all.
    """
    ordered = list(screens)
    for frame in ordered:
        if frame_contains(frame, mouse_x, mouse_y):
            return frame
    return ordered[0] if ordered else None


def pill_origin(screen, width, height=HEIGHT, margin=BOTTOM_MARGIN):
    """Center-bottom origin of a pill of `width` inside one screen.

    The x is recentered on every width change so the pill grows and
    shrinks symmetrically. Clamp its origin to the screen edges, including
    displays with a negative origin. The y keeps the pill above the Dock because the
    caller passes a visibleFrame, and it never pushes past the top of a
    short screen.
    """
    if screen is None:
        return 0.0, margin
    x, y, sw, sh = screen
    left = x + (sw - width) / 2.0
    left = max(x, min(left, x + sw - width))
    bottom = y + margin
    if bottom + height > y + sh:
        bottom = y + sh - height
    bottom = max(y, bottom)
    return left, bottom


# The overlay mark view class is registered once per process: PyObjC
# raises on a second class definition with the same name, so the module
# caches the class after the first call.
def _overlay_mark_view_class(NSView):
    cached = globals().get("_OVERLAY_MARK_VIEW")
    if cached is not None:
        return cached

    class _OverlayMarkView(NSView):
        """The shared b mark drawn with the bolo_brand path."""

        def drawRect_(self, rect):
            pal = bolo_brand.palette(dark=True)
            bolo_brand.draw_mark(
                0,
                0,
                min(self.bounds().size.width, self.bounds().size.height),
                ink=pal["text"],
                terminal=pal["accent"],
                flipped=False,
            )

    globals()["_OVERLAY_MARK_VIEW"] = _OverlayMarkView
    return _OverlayMarkView


def run_overlay(preview=None):
    """Build the panel and pump stdin until the runtime stops talking.

    Kept in one function so importing the module never touches AppKit
    windows: the pure helpers above stay testable without a display.
    With `preview={"phase": ..., "text": ...}` the panel renders that
    state and returns before any ordering front or stdin loop: the same
    window and content view objects (and the same render function) the
    live protocol path uses, so an offscreen renderer shows the actual
    pill. The window is never ordered front in preview mode and never
    activates the app.
    """
    import os

    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSDefaultRunLoopMode,
        NSFont,
        NSFontWeightMedium,
        NSMakeRect,
        NSPanel,
        NSRunLoop,
        NSScreen,
        NSTextAlignmentLeft,
        NSTextField,
        NSView,
        NSViewWidthSizable,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
        NSFloatingWindowLevel,
    )
    from Foundation import NSDate
    import AppKit

    NSColor = AppKit.NSColor
    preview_mode = isinstance(preview, dict)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.finishLaunching()

    # Active-display selection (idea borrowed from OpenSuperWhisper's
    # FocusUtils.swift chooseIndicatorPoint(mouseposition:)): the
    # current global mouse position is the cheapest cue for the screen
    # the user is working on, needs no accessibility calls and adds no
    # permissions, and never blocks capture or transcription. The full
    # screen frame is used for pointer containment so a mouse parked
    # over the Dock or menu bar still selects that monitor, then the
    # visibleFrame of the winner places the pill clear of the Dock and
    # menu bar. The display is chosen once per dictation and stays
    # stable for the whole session. PyObjC NSRect values are nested
    # (origin, size) structs, so each is flattened to a plain
    # x, y, width, height tuple before reaching the pure helpers.
    def _flat(rect):
        return (
            float(rect.origin.x),
            float(rect.origin.y),
            float(rect.size.width),
            float(rect.size.height),
        )

    visible = []
    full = []
    for candidate in NSScreen.screens() or []:
        visible.append(_flat(candidate.visibleFrame()))
        full.append(_flat(candidate.frame()))
    if preview_mode:
        # Offscreen rendering must not depend on where the pointer
        # happens to be: the main display is the deterministic preview.
        chosen_index = 0
    else:
        mouse = AppKit.NSEvent.mouseLocation()
        chosen_frame = choose_screen(mouse.x, mouse.y, full)
        chosen_index = full.index(chosen_frame) if chosen_frame is not None else 0
    screen = visible[chosen_index] if visible else (0.0, 0.0, 1440.0, 900.0)

    x, y = pill_origin(screen, MIN_WIDTH)

    window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, MIN_WIDTH, HEIGHT),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        2,
        False,
    )
    window.setLevel_(NSFloatingWindowLevel + 1)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    # The pill must never take focus: it ignores the mouse entirely, so
    # the user's typing and held dictation key are untouched while it is
    # up, and it keeps showing when Bolo is not frontmost.
    window.setIgnoresMouseEvents_(True)
    window.setHasShadow_(True)
    window.setHidesOnDeactivate_(False)
    window.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 6))

    content = NSView.alloc().initWithFrame_(
        NSMakeRect(0, 0, MIN_WIDTH, HEIGHT)
    )
    content.setWantsLayer_(True)
    content.layer().setCornerRadius_(HEIGHT / 2)
    content.layer().setMasksToBounds_(True)
    # Opaque warm ink surface instead of the vibrancy material: the
    # HUDWindow material washes the transcript out in busy-desktop and
    # offscreen rendering. Same pill, same layout, legible ivory text on
    # any background, and it matches the setup window's night surface.
    content.layer().setBackgroundColor_(
        bolo_brand.native_color(bolo_brand.NIGHT).CGColor()
    )
    window.setContentView_(content)

    # The shared custom b mark, drawn by the same bolo_brand path the
    # setup window and the icon use: ivory stem and bowl, clay terminal.
    _OverlayMarkView = _overlay_mark_view_class(AppKit.NSView)

    mark = _OverlayMarkView.alloc().initWithFrame_(
        NSMakeRect(MARK_X, (HEIGHT - MARK_SIZE) / 2.0, MARK_SIZE, MARK_SIZE)
    )
    content.addSubview_(mark)

    dot = NSView.alloc().initWithFrame_(
        NSMakeRect(
            TEXT_X, (HEIGHT - DOT_SIZE) / 2, DOT_SIZE, DOT_SIZE
        )
    )
    dot.setWantsLayer_(True)
    dot.layer().setCornerRadius_(DOT_SIZE / 2)
    dot.layer().setBackgroundColor_(phase_color("dictating").CGColor())

    label = NSTextField.alloc().initWithFrame_(
        NSMakeRect(TEXT_X + 18, 11, MIN_WIDTH - TEXT_X - 24, 20)
    )
    label.setAlignment_(NSTextAlignmentLeft)
    label.setFont_(NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium))
    label.setTextColor_(bolo_brand.native_color(bolo_brand.palette(dark=True)["text"]))
    label.setBackgroundColor_(NSColor.clearColor())
    label.setBezeled_(False)
    label.setBordered_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setAutoresizingMask_(NSViewWidthSizable)

    transcript = NSTextField.alloc().initWithFrame_(
        NSMakeRect(TEXT_X + 18, 3, MIN_WIDTH - TEXT_X - 24, TRANSCRIPT_LINE_H)
    )
    transcript.setAlignment_(NSTextAlignmentLeft)
    transcript.setFont_(NSFont.systemFontOfSize_(11.0))
    transcript.setTextColor_(
        bolo_brand.native_color(bolo_brand.palette(dark=True)["muted"])
    )
    transcript.setBackgroundColor_(NSColor.clearColor())
    transcript.setBezeled_(False)
    transcript.setBordered_(False)
    transcript.setEditable_(False)
    transcript.setSelectable_(False)
    transcript.setAutoresizingMask_(NSViewWidthSizable)
    transcript.setStringValue_("")

    content.addSubview_(dot)
    content.addSubview_(label)
    content.addSubview_(transcript)

    def resize(width):
        """Recenter the pill horizontally on its chosen display as the
        width changes, clamped so it never leaves that display."""
        new_x, _ = pill_origin(screen, width)
        new_frame = NSMakeRect(
            new_x,
            window.frame().origin.y,
            width,
            window.frame().size.height,
        )
        window.setFrame_display_(new_frame, True)

    def render(phase, preview=""):
        """Show one state: the phase label always, plus the live
        transcript tail while dictating.

        With a live preview the label moves to the top line and the
        transcript to the bottom, so the two never overlap inside the
        44pt pill; without one the label stays vertically centered and
        the transcript line is empty.
        """
        text = phase_label(phase)
        accent = phase_color(phase)
        line = preview_text(preview) if phase == "dictating" else ""
        label.setStringValue_(text)
        transcript.setStringValue_(line)
        if line:
            label.setFrame_(
                NSMakeRect(
                    TEXT_X + 18, 22, window.frame().size.width - TEXT_X - 24, 18
                )
            )
            transcript.setHidden_(False)
        else:
            # No transcript: the label centers naturally and the empty
            # transcript line is hidden entirely, so the two frames are
            # genuinely disjoint instead of invisibly overlapping.
            label.setFrame_(
                NSMakeRect(
                    TEXT_X + 18, (HEIGHT - 18) // 2,
                    window.frame().size.width - TEXT_X - 24, 18
                )
            )
            transcript.setHidden_(True)
        transcript.setFrame_(
            NSMakeRect(
                TEXT_X + 18, 4, window.frame().size.width - TEXT_X - 24,
                TRANSCRIPT_LINE_H,
            )
        )
        dot.layer().setBackgroundColor_(accent.CGColor())
        resize(pill_width(preview, phase))

    phase = "dictating"
    preview_text_value = ""
    if preview_mode:
        phase = preview.get("phase") or "dictating"
        preview_text_value = preview.get("text") or ""
    render(phase, preview_text_value)
    if not preview_mode:
        window.orderFrontRegardless()
        last_message_at = time.time()

        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                line = sys.stdin.readline()
                if line == "":
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = {}
                phase = message.get("phase", phase)
                preview = message.get("text", "") if phase == "dictating" else ""
                render(phase, preview)
                last_message_at = time.time()

            if time.time() - last_message_at > STALL_TIMEOUT:
                break

            NSRunLoop.mainRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode,
                NSDate.dateWithTimeIntervalSinceNow_(0.05),
            )

        window.orderOut_(None)
        os._exit(0)
    return {"window": window, "content": content, "render": render}


if __name__ == "__main__":
    try:
        first = sys.stdin.readline()
        if first.strip():
            initial = json.loads(first)
        else:
            initial = {}
    except ValueError:
        initial = {}
    if isinstance(initial, dict) and initial:
        run_overlay({"phase": initial.get("phase", "dictating"), "text": initial.get("text", "")})
    else:
        run_overlay()
