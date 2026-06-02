"""
Transcript review popup for Bolo.

Opens when user clicks 'Show Last Transcript' from the menubar.
Shows the most recent dictation in a scrollable window with metadata.
"""

import AppKit
from AppKit import (
    NSApp,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSMakeRect,
    NSPanel,
    NSTextView,
    NSScrollView,
    NSTitledWindowMask,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
)

NSFont = AppKit.NSFont
NSColor = AppKit.NSColor


class TranscriptReviewWindow:
    """Floating panel showing the last dictation."""

    def __init__(self):
        self._window = None

    def show(self, text: str, raw_text: str = "", metadata: dict = None):
        """Show the transcript in a scrollable window."""
        if self._window:
            self._window.close()
            self._window = None

        rect = NSMakeRect(0, 0, 520, 380)
        win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            NSTitledWindowMask | NSWindowStyleMaskClosable |
            NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            False,
        )
        win.setTitle_("Bolo - Last Transcript")
        win.center()
        win.setFloatingPanel_(True)

        content = win.contentView()

        # Metadata header
        if metadata:
            header_items = []
            if metadata.get("source_app"):
                header_items.append(f"App: {metadata['source_app']}")
            if metadata.get("latency_ms"):
                header_items.append(f"Latency: {metadata['latency_ms']}ms")
            if metadata.get("word_count"):
                header_items.append(f"Words: {metadata['word_count']}")
            if metadata.get("stt_provider"):
                header_items.append(f"STT: {metadata['stt_provider']}")
            header = "  |  ".join(header_items)
        else:
            header = ""

        # Text view
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(12, 12, 496, 356))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)

        tv = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, 480, 340))
        tv.setEditable_(False)
        tv.setSelectable_(True)
        tv.setFont_(NSFont.systemFontOfSize_(14))

        # Build display text
        display = ""
        if header:
            display += header + "\n\n"
        display += text
        if raw_text and raw_text != text:
            display += "\n\n── Raw STT Output ──\n\n"
            display += raw_text

        tv.setString_(display)

        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)

        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        NSApp.activateIgnoringOtherApps_(True)

        self._window = win
        win.makeKeyAndOrderFront_(None)
