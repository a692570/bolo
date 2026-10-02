#!/usr/bin/env python3
"""Generate the Bolo app icon as a 1024px PNG.

Draws a dark rounded square, a green centered waveform, and a bold "B"
wordmark using AppKit, so the DMG build gets a reproducible icon with no
binary asset checked into git. Run under any Python with pyobjc (the DMG
build creates one from the bundled python-build-standalone runtime):

    python3 scripts/make_icon.py --output build/icon/bolo-icon-1024.png
"""

import argparse
import os
import sys

SIZE = 1024
CORNER_RADIUS = 224
RECT_INSET = 16
WAVE_BAR_W = 36
WAVE_GAP = 26
WAVE_MAX_H = 190
WAVE_CENTER_Y = 380
WAVE_HEIGHTS = (0.90, 0.55, 0.72, 1.00, 0.62, 0.86, 0.48, 0.74, 0.52)
WORDMARK_FONT_SIZE = 300
WORDMARK_CENTER_Y = 700
BG_COLOR = (0.055, 0.075, 0.070, 1.0)
WAVE_COLOR = (0.30, 0.875, 0.49, 1.0)
WORD_COLOR = (0.95, 0.96, 0.95, 1.0)


def wave_bar_rects(size=SIZE):
    """Pure layout: each waveform bar's (x, y, w, h) in top-down pixels."""
    count = len(WAVE_HEIGHTS)
    total_w = count * WAVE_BAR_W + (count - 1) * WAVE_GAP
    x = (size - total_w) / 2.0
    rects = []
    for scale in WAVE_HEIGHTS:
        height = WAVE_MAX_H * scale
        top = WAVE_CENTER_Y - height / 2.0
        rects.append((x, top, float(WAVE_BAR_W), height))
        x += WAVE_BAR_W + WAVE_GAP
    return rects


def rounded_rect_path(x, y, w, h, radius, flipped=True):
    """Build an NSBezierPath rounded rect; `y` is top-down like the rest."""
    from AppKit import NSMakeRect, NSBezierPath

    if flipped:
        y = SIZE - y - h
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(x, y, w, h), radius, radius
    )
    return path


def render(size=SIZE):
    """Render the icon into an AppKit bitmap image rep."""
    from AppKit import (
        NSBitmapImageRep,
        NSColor,
        NSGraphicsContext,
    )

    def rgba(components):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(*components)

    rep = (
        NSBitmapImageRep.alloc()
        .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None,
            size,
            size,
            8,
            4,
            True,
            False,
            "NSCalibratedRGBColorSpace",
            0,
            32,
        )
    )
    rep.setSize_((size, size))
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    inset = RECT_INSET
    body = rounded_rect_path(inset, inset, size - 2 * inset, size - 2 * inset, CORNER_RADIUS)
    rgba(BG_COLOR).setFill()
    body.fill()

    for bar_x, bar_y, bar_w, bar_h in wave_bar_rects(size):
        bar = rounded_rect_path(bar_x, bar_y, bar_w, bar_h, bar_w / 2.0)
        rgba(WAVE_COLOR).setFill()
        bar.fill()

    draw_wordmark(size)

    NSGraphicsContext.restoreGraphicsState()
    return rep


def draw_wordmark(size=SIZE):
    """Center the bold `B` wordmark at WORDMARK_CENTER_Y (top-down)."""
    from AppKit import (
        NSAttributedString,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSMutableParagraphStyle,
        NSParagraphStyleAttributeName,
    )

    font = NSFont.boldSystemFontOfSize_(WORDMARK_FONT_SIZE)
    paragraph = NSMutableParagraphStyle.new()
    paragraph.setAlignment_(1)  # NSCenterTextAlignment
    attributes = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: _word_color(),
        NSParagraphStyleAttributeName: paragraph,
    }
    text = NSAttributedString.alloc().initWithString_attributes_("B", attributes)
    width, height = text.size()
    # Bitmap contexts are not flipped: convert the top-down baseline box.
    center_y = size - WORDMARK_CENTER_Y
    text.drawAtPoint_(((size - width) / 2.0, center_y - height / 2.0))


def _word_color():
    from AppKit import NSColor

    return NSColor.colorWithCalibratedRed_green_blue_alpha_(*WORD_COLOR)


def write_png(rep, output_path):
    """Save the rep as PNG; parent dirs are created as needed."""
    from AppKit import NSBitmapImageFileTypePNG

    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "wb") as handle:
        handle.write(bytes(data))
    return output_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="bolo-icon-1024.png",
        help="output PNG path (default: %(default)s)",
    )
    parser.add_argument("--size", type=int, default=SIZE, help="canvas size")
    args = parser.parse_args(argv)
    try:
        rep = render(args.size)
    except ImportError as error:
        print(
            "[make-icon] AppKit is unavailable: {0}. Use a python with pyobjc.".format(error),
            file=sys.stderr,
        )
        return 1
    write_png(rep, args.output)
    print("[make-icon] wrote {0}".format(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
