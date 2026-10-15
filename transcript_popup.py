#!/usr/bin/env python3
"""Manual-copy fallback popup for Bolo: transcript ready to paste.

Shown when a dictation has nowhere safe to paste: the press-time
accessibility read found no editable focus, or the paste attempt failed
outright. The transcript is already on the clipboard at popup time; the
popup shows the text (word-wrapped, at most ~6 visible lines, scrollable)
in an EDITABLE body, with a Copy button, so the user can fix a misheard
word right in the popup and re-copy the corrected text before a manual
Cmd+V.

Panel construction follows overlay.py: borderless nonactivating NSPanel
near the bottom of the active display, warm ink palette from bolo_brand,
the shared b mark. It never steals keyboard focus at show time
(``orderFrontRegardless`` activates nothing), but after an explicit click
into the body the panel may become key (the ``canBecomeKeyWindow``
override on a borderless panel is what allows that) without activating
the app, so the user's typing still belongs to whatever they were doing
the moment before they clicked. The AX caret context sees the popup's
text view while focused, so Bolo's edit-learning poll watcher can learn
corrections typed here like post-paste edits. It closes on Dismiss, on
an outside click, or when the runtime closes the pipe (a dead or replaced
runtime never leaves a stale popup), and caps its own lifetime so no
interaction is needed for it to disappear.

Protocol: one JSON object on stdin (``{"text": ...}``); one JSON object
on stdout when Copy is clicked (``{"type": "copy_used", "text": ...}``
carrying the popup's CURRENT body text, edited or untouched), then the
popup closes itself; the runtime owns the actual clipboard write, so the
edited text is what Copy puts on the clipboard. A reply without ``text``
(an older popup) makes the runtime fall back to the show-time
transcript. An empty transcript short-circuits with
``{"type": "empty_dismissed"}`` and no window at all.
"""

import json
import select
import sys
import time
import warnings

import bolo_brand
from overlay import frame_contains, pill_origin

from objc import ObjCPointerWarning


warnings.filterwarnings("ignore", category=ObjCPointerWarning)


PADDING = 18
TITLE_H = 24
BODY_LINE_H = 16
BODY_LINES = 6
BODY_H = BODY_LINE_H * BODY_LINES
BODY_TOP_GAP = 10
BUTTONS_H = 30
BUTTONS_TOP_GAP = 14
WIDTH = 440
HEIGHT = PADDING + TITLE_H + BODY_TOP_GAP + BODY_H + BUTTONS_TOP_GAP + BUTTONS_H + PADDING
BOTTOM_MARGIN = 160
COPY_W = 120
DISMISS_W = 96
MARK_SIZE = 20
LIFETIME_SECONDS = 90.0
TITLE_TEXT = "Transcript ready to paste"

# The one line the popup writes back when Copy is clicked, carrying the
# popup's current (possibly edited) body text.
def copy_reply(text):
    return json.dumps({"type": "copy_used", "text": text})


# The left-mouse-down MASK for event monitors: `addGlobalMonitorForEventsMatchingMask_`
# takes a mask (1 << 1), not the NSEventType constant (1). Passing the type
# constant registered the monitor for the wrong bits, which is why the
# outside-click monitors never fired (issue #33).
LEFT_MOUSE_DOWN_MASK = 1 << 1
KEY_ESCAPE = 53


def popup_height(lines=BODY_LINES):
    """Panel height for a body capped at `lines` visible transcript lines."""
    body = lines * BODY_LINE_H
    return (
        PADDING + TITLE_H + BODY_TOP_GAP + body + BUTTONS_TOP_GAP + BUTTONS_H + PADDING
    )


def parse_payload(line):
    """Turn the first stdin line into display text; damage means empty."""
    try:
        payload = json.loads(line)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("text") or "")


def _mark_view_class(NSView):
    """Cache the b-mark view class: PyObjC rejects re-registration."""
    cached = globals().get("_TRANSCRIPT_MARK_VIEW")
    if cached is not None:
        return cached

    class _TranscriptMarkView(NSView):
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

    globals()["_TRANSCRIPT_MARK_VIEW"] = _TranscriptMarkView
    return _TranscriptMarkView


def _panel_class(NSPanel):
    """Cache the popup panel class: PyObjC rejects re-registration.

    Borderless windows refuse key status by default, so the editable body
    could never receive keyboard input after a click. The override lets
    the panel become key ONLY when the user explicitly clicks into it;
    show time stays nonactivating and never steals focus (nothing calls
    makeKey before a click).
    """
    cached = globals().get("_TRANSCRIPT_PANEL")
    if cached is not None:
        return cached

    class _TranscriptPanel(NSPanel):
        def canBecomeKeyWindow(self):
            return True

    globals()["_TRANSCRIPT_PANEL"] = _TranscriptPanel
    return _TranscriptPanel


def _controller_class(NSObject):
    """Button target for the popup; `close` is bound per popup instance."""
    cached = globals().get("_TRANSCRIPT_CONTROLLER")
    if cached is not None:
        return cached

    class _TranscriptPopupController(NSObject):
        def copyTranscript_(self, sender):
            self.close_callback(used_copy=True)

        def dismiss_(self, sender):
            self.close_callback(used_copy=False)

    globals()["_TRANSCRIPT_CONTROLLER"] = _TranscriptPopupController
    return _TranscriptPopupController


def run_popup(transcript, preview=False):
    """Build the panel and pump stdin until the runtime closes the pipe.

    Live mode watches stdin for EOF (runtime gone, or replaced this popup),
    with a hard lifetime cap, and never reads more than the payload already
    consumed. `preview=True` (offscreen rendering and tests) returns the
    same window objects without ordering anything front (overlay.py's
    preview pattern).
    """
    import AppKit
    from AppKit import (
        NSAttributedString,
        NSButton,
        NSDefaultRunLoopMode,
        NSFont,
        NSFontWeightMedium,
        NSObject,
        NSMakeRect,
        NSMutableParagraphStyle,
        NSNoBorder,
        NSPanel,
        NSRunLoop,
        NSScrollView,
        NSScreen,
        NSTextAlignmentCenter,
        NSTextAlignmentLeft,
        NSTextField,
        NSTextView,
        NSView,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
        NSFloatingWindowLevel,
    )
    from Foundation import NSDate, NSSize

    NSColor = AppKit.NSColor
    NSSize_cls = NSSize

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    app.finishLaunching()

    pal = bolo_brand.palette(dark=True)
    height = popup_height()

    def _flat(rect):
        return (
            float(rect.origin.x),
            float(rect.origin.y),
            float(rect.size.width),
            float(rect.size.height),
        )

    # Same active-display selection as the overlay: the screen the pointer
    # is on, main display first as fallback. Offscreen rendering must not
    # depend on the pointer, so preview always uses the main display.
    visible = [_flat(candidate.visibleFrame()) for candidate in NSScreen.screens() or []]
    full = [_flat(candidate.frame()) for candidate in NSScreen.screens() or []]
    if preview or not visible:
        screen = visible[0] if visible else (0.0, 0.0, 1440.0, 900.0)
    else:
        mouse = AppKit.NSEvent.mouseLocation()
        mouse_frame = next(
            (frame for frame in full if frame_contains(frame, mouse.x, mouse.y)),
            None,
        )
        screen = visible[full.index(mouse_frame)] if mouse_frame is not None else visible[0]
    x, y = pill_origin(screen, WIDTH, height, margin=BOTTOM_MARGIN)

    window = _panel_class(NSPanel).alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, WIDTH, height),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        2,
        False,
    )
    window.setLevel_(NSFloatingWindowLevel + 1)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    # Clicks must reach the buttons and the editable body, but the panel
    # only becomes key on an explicit click (never at show time): keyboard
    # focus stays in whatever the user was doing, then belongs to the
    # body the moment they click into it.
    window.setIgnoresMouseEvents_(False)
    window.setHasShadow_(True)
    window.setHidesOnDeactivate_(False)
    window.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 6))

    content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, height))
    content.setWantsLayer_(True)
    content.layer().setCornerRadius_(16)
    content.layer().setMasksToBounds_(True)
    content.layer().setBackgroundColor_(bolo_brand.native_color(bolo_brand.NIGHT).CGColor())
    window.setContentView_(content)

    mark = _mark_view_class(NSView).alloc().initWithFrame_(
        NSMakeRect(PADDING, height - PADDING - MARK_SIZE, MARK_SIZE, MARK_SIZE)
    )
    content.addSubview_(mark)

    title = NSTextField.alloc().initWithFrame_(
        NSMakeRect(
            PADDING + MARK_SIZE + 10,
            height - PADDING - TITLE_H,
            WIDTH - PADDING * 2 - MARK_SIZE - 10,
            TITLE_H,
        )
    )
    title.setAlignment_(NSTextAlignmentLeft)
    title.setFont_(NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium))
    title.setTextColor_(bolo_brand.native_color(pal["text"]))
    title.setBackgroundColor_(NSColor.clearColor())
    title.setBezeled_(False)
    title.setBordered_(False)
    title.setEditable_(False)
    title.setSelectable_(False)
    title.setStringValue_(TITLE_TEXT)
    content.addSubview_(title)

    body_frame = NSMakeRect(
        PADDING,
        PADDING + BUTTONS_H + BUTTONS_TOP_GAP,
        WIDTH - PADDING * 2,
        BODY_H,
    )
    scroll = NSScrollView.alloc().initWithFrame_(body_frame)
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(NSNoBorder)
    scroll.setDrawsBackground_(True)
    surface = bolo_brand.native_color(bolo_brand.NIGHT_SURFACE)
    scroll.setBackgroundColor_(surface)
    scroll.contentView().setDrawsBackground_(True)
    scroll.contentView().setBackgroundColor_(surface)

    body = NSTextView.alloc().initWithFrame_(
        NSMakeRect(0, 0, body_frame.size.width, body_frame.size.height)
    )
    # Editable: a misheard word can be fixed right here. The text view is
    # key-capable only after an explicit click (see _panel_class), keeps
    # rich text and automatic substitutions off so the transcript is
    # edited as plain verbatim text, and its focused state is visible to
    # the AX caret context so the poll watcher learns the fix.
    body.setEditable_(True)
    body.setSelectable_(True)
    body.setRichText_(False)
    body.setUsesFontPanel_(False)
    body.setAutomaticQuoteSubstitutionEnabled_(False)
    body.setAutomaticDashSubstitutionEnabled_(False)
    body.setAutomaticTextReplacementEnabled_(False)
    body.setAutomaticSpellingCorrectionEnabled_(False)
    body.setAllowsUndo_(True)
    body.setDrawsBackground_(False)
    body.setFont_(NSFont.systemFontOfSize_(12.0))
    body.setTextColor_(bolo_brand.native_color(pal["muted"]))
    body.setString_(transcript)
    body.setTextContainerInset_(NSSize_cls(8.0, 8.0))
    container = body.textContainer()
    # Fix the container width so long transcripts wrap instead of
    # stretching the popup; height is unbounded and the scroll view clips.
    container.setContainerSize_(NSSize_cls(body_frame.size.width - 16.0, 1_000_000.0))
    container.setWidthTracksTextView_(False)
    container.setHeightTracksTextView_(False)
    body.setVerticallyResizable_(True)
    body.setHorizontallyResizable_(False)
    scroll.setDocumentView_(body)
    content.addSubview_(scroll)

    controller = _controller_class(NSObject).alloc().init()

    def make_button(title, action, background, text_color, frame_x, width):
        button = NSButton.buttonWithTitle_target_action_(
            title, controller, action
        )
        button.setBordered_(False)
        button.setWantsLayer_(True)
        button.layer().setCornerRadius_(8)
        button.layer().setBackgroundColor_(background.CGColor())
        paragraph = NSMutableParagraphStyle.alloc().init()
        paragraph.setAlignment_(NSTextAlignmentCenter)
        button.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                title,
                {
                    "NSFont": NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium),
                    "NSColor": text_color,
                    "NSParagraphStyle": paragraph,
                },
            )
        )
        button.setFrame_(NSMakeRect(frame_x, PADDING, width, BUTTONS_H))
        content.addSubview_(button)
        return button

    make_button(
        "Copy",
        "copyTranscript:",
        bolo_brand.native_color(pal["button"]),
        bolo_brand.native_color(pal["button_text"]),
        WIDTH - PADDING - COPY_W,
        COPY_W,
    )
    make_button(
        "Dismiss",
        "dismiss:",
        bolo_brand.native_color(bolo_brand.NIGHT_SURFACE),
        bolo_brand.native_color(pal["muted"]),
        WIDTH - PADDING - COPY_W - 10 - DISMISS_W,
        DISMISS_W,
    )

    if preview:
        # The same window and content objects the live protocol path uses;
        # never ordered front, never bound to a close action.
        return {
            "window": window,
            "content": content,
            "body": body,
            "height": height,
        }

    state = {"open": True, "outside_monitor": None, "local_monitor": None}

    def current_text():
        """The body's current contents: the transcript plus any fixes."""
        return str(body.string())

    def close(used_copy=False):
        if not state["open"]:
            return
        state["open"] = False
        if used_copy:
            # The reply carries the CURRENT body text, so an edited
            # transcript is what the runtime puts on the clipboard.
            sys.stdout.write(copy_reply(current_text()) + "\n")
            sys.stdout.flush()
        window.orderOut_(None)
        import os

        os._exit(0)

    # Plain Python attribute (not an ObjC selector: a bind_close_ method
    # name would resolve to the two-argument bind:close: selector).
    controller.close_callback = close

    def outside_click(event):
        # mousedown means the pointer is at the click point; anything
        # outside this panel's frame dismisses it.
        location = AppKit.NSEvent.mouseLocation()
        if not frame_contains(_flat(window.frame()), location.x, location.y):
            close()

    def local_click(event):
        outside_click(event)
        return event

    # Left-mouse-down monitors: the global one sees other apps' clicks,
    # the local one clicks inside this process (e.g. the popup's own body).
    state["outside_monitor"] = (
        AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            LEFT_MOUSE_DOWN_MASK, outside_click
        )
    )
    state["local_monitor"] = (
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            LEFT_MOUSE_DOWN_MASK, local_click
        )
    )

    def escape_pressed(event):
        # Local monitor only: Escape dismisses when the popup itself is
        # key (the keyDown is destined for this process). Escape typed in
        # other apps never reaches a local monitor.
        if event.keyCode() == KEY_ESCAPE:
            close()
            return None
        return event

    state["escape_monitor"] = (
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, escape_pressed
        )
    )

    window.orderFrontRegardless()
    deadline = time.time() + LIFETIME_SECONDS
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if ready:
            line = sys.stdin.readline()
            if line == "":
                # The runtime closed the pipe: it is gone or replacing this
                # popup with a newer one. Either way, dismiss quietly.
                close()

        if time.time() > deadline:
            close()

        # Pump and dispatch events. runMode alone never dequeues application
        # events, so buttons, the text view, and the outside-click monitors
        # never saw input (issue #33): pull one event per tick and hand it
        # to the app.
        pending = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
            AppKit.NSEventMaskAny,
            NSDate.dateWithTimeIntervalSinceNow_(0.02),
            NSDefaultRunLoopMode,
            True,
        )
        if pending is not None:
            app.sendEvent_(pending)

        NSRunLoop.mainRunLoop().runMode_beforeDate_(
            NSDefaultRunLoopMode,
            NSDate.dateWithTimeIntervalSinceNow_(0.05),
        )


def main():
    first = sys.stdin.readline()
    transcript = parse_payload(first)
    if not transcript.strip():
        # Nothing to show: a blank popup would only confuse, and the
        # clipboard/history already carry whatever the runtime produced.
        sys.stdout.write(json.dumps({"type": "empty_dismissed"}) + "\n")
        sys.stdout.flush()
        return
    run_popup(transcript)


if __name__ == "__main__":
    main()
