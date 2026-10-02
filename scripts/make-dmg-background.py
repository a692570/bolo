#!/usr/bin/env python3
"""Generate the Bolo DMG background image as a 660x400 PNG.

Draws the dark brand surface with green accents (matching the app icon
family: near-black surface, green waveform), the "Drag Bolo to
Applications" instruction line, and a green arrow pointing from the
Bolo.app slot to the Applications slot the Finder layout pins. Run under
the same build-time venv that renders the app icon:

    python3 scripts/make-dmg-background.py --output build/dmg/background.png
"""

import argparse
import os
import sys

WIDTH = 660
HEIGHT = 400

# Icon slots the Finder layout (build-dmg.sh) pins: the Applications
# symlink on the left, Bolo.app on the right.
ICON_SIZE = 80
APPLICATIONS_POS = (180, 220)
BOLO_POS = (480, 220)

WORDMARK = "BOLO"
WORDMARK_FONT_SIZE = 44
WORDMARK_CENTER_Y = 64
WORDMARK_GAP = 16.0
BAR_W = 4.5
BAR_GAP = 3.0
BAR_HEIGHTS = (13.0, 22.0, 28.0, 18.0, 9.0)

SUBTITLE = "Drag Bolo to Applications"
SUBTITLE_FONT_SIZE = 20
SUBTITLE_CENTER_Y = 116

ARROW_SHAFT_H = 8.0
ARROW_SHAFT_LEN = 110.0
ARROW_HEAD_W = 30.0
ARROW_HEAD_H = 28.0

BG_COLOR = (0.055, 0.075, 0.070, 1.0)
ACCENT_COLOR = (0.30, 0.875, 0.49, 1.0)
ACCENT_DIM_ALPHA = 0.45
WORD_COLOR = (0.95, 0.96, 0.95, 1.0)
SUBTITLE_COLOR = (0.80, 0.86, 0.82, 1.0)


def icon_center(slot):
    """Center of one icon slot's cell: position is the cell's top-left."""
    return (slot[0] + ICON_SIZE / 2.0, slot[1] + ICON_SIZE / 2.0)


def arrow_center():
    """Midpoint between the two icon slots, at the icon band's height."""
    left = icon_center(APPLICATIONS_POS)
    right = icon_center(BOLO_POS)
    return ((left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0)


def arrow_pieces():
    """The drag arrow as (shaft_rect, head_points), pointing from Bolo's
    slot toward the Applications slot (right to left)."""
    center_x, center_y = arrow_center()
    shaft_left = center_x - ARROW_SHAFT_LEN / 2.0 + ARROW_HEAD_W / 2.0
    shaft = (
        shaft_left,
        center_y - ARROW_SHAFT_H / 2.0,
        ARROW_SHAFT_LEN,
        ARROW_SHAFT_H,
    )
    head = (
        (shaft[0], center_y - ARROW_HEAD_H / 2.0),
        (shaft[0], center_y + ARROW_HEAD_H / 2.0),
        (shaft[0] - ARROW_HEAD_W, center_y),
    )
    return shaft, head


def wave_bar_rects(unit_left):
    """(x, y, w, h) for each waveform bar, ordered left to right, vertically
    centered on the wordmark line starting at ``unit_left``."""
    rects = []
    x = unit_left
    for height in BAR_HEIGHTS:
        rects.append((x, WORDMARK_CENTER_Y - height / 2.0, BAR_W, height))
        x += BAR_W + BAR_GAP
    return rects


def wave_bars_width():
    return len(BAR_HEIGHTS) * BAR_W + (len(BAR_HEIGHTS) - 1) * BAR_GAP


def render(width=WIDTH, height=HEIGHT):
    """Render the background into an AppKit bitmap image rep."""
    from AppKit import (
        NSAttributedString,
        NSBezierPath,
        NSBitmapImageRep,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSGraphicsContext,
        NSMakeRect,
        NSMutableParagraphStyle,
        NSParagraphStyleAttributeName,
    )

    def rgba(components, alpha=None):
        red, green, blue, base_alpha = components
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(
            red, green, blue, base_alpha if alpha is None else alpha
        )

    rep = (
        NSBitmapImageRep.alloc()
        .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None,
            width,
            height,
            8,
            4,
            True,
            False,
            "NSCalibratedRGBColorSpace",
            0,
            32,
        )
    )
    rep.setSize_((width, height))
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    rgba(BG_COLOR).setFill()
    NSBezierPath.fillRect_(NSMakeRect(0, 0, width, height))

    def attributed(text, font, color, alignment):
        paragraph = NSMutableParagraphStyle.alloc().init()
        paragraph.setAlignment_(alignment)
        return NSAttributedString.alloc().initWithString_attributes_(
            text,
            {
                NSFontAttributeName: font,
                NSForegroundColorAttributeName: color,
                NSParagraphStyleAttributeName: paragraph,
            },
        )

    # Wordmark plus waveform glyph, centered as one unit. Bitmap contexts
    # are not flipped, so top-down center lines convert at draw time.
    word_font = NSFont.boldSystemFontOfSize_(WORDMARK_FONT_SIZE)
    word_attributed = attributed(WORDMARK, word_font, rgba(WORD_COLOR), 0)
    word_w, word_h = word_attributed.size()
    unit_width = wave_bars_width() + WORDMARK_GAP + word_w
    unit_left = (width - unit_width) / 2.0
    accent = rgba(ACCENT_COLOR)
    accent.setFill()
    for bar_x, bar_y, bar_w, bar_h in wave_bar_rects(unit_left):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bar_x, height - bar_y - bar_h, bar_w, bar_h),
            bar_w / 2.0,
            bar_w / 2.0,
        )
        path.fill()
    word_attributed.drawAtPoint_(
        (
            unit_left + wave_bars_width() + WORDMARK_GAP,
            height - WORDMARK_CENTER_Y - word_h / 2.0,
        )
    )

    subtitle_attributed = attributed(
        SUBTITLE, NSFont.systemFontOfSize_(SUBTITLE_FONT_SIZE), rgba(SUBTITLE_COLOR), 1
    )
    subtitle_w, subtitle_h = subtitle_attributed.size()
    subtitle_attributed.drawAtPoint_(
        ((width - subtitle_w) / 2.0, height - SUBTITLE_CENTER_Y - subtitle_h / 2.0)
    )

    # Drag arrow: shaft plus a leftward head, in the dimmed accent green.
    shaft, head = arrow_pieces()
    rgba(ACCENT_COLOR, ACCENT_DIM_ALPHA).setFill()
    shaft_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(
            shaft[0],
            height - shaft[1] - shaft[3],
            shaft[2],
            shaft[3],
        ),
        shaft[3] / 2.0,
        shaft[3] / 2.0,
    )
    shaft_path.fill()
    head_path = NSBezierPath.bezierPath()
    head_path.moveToPoint_((head[0][0], height - head[0][1]))
    head_path.lineToPoint_((head[1][0], height - head[1][1]))
    head_path.lineToPoint_((head[2][0], height - head[2][1]))
    head_path.closePath()
    head_path.fill()

    NSGraphicsContext.restoreGraphicsState()
    return rep


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
        default="dmg-background.png",
        help="output PNG path (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        rep = render()
    except ImportError as error:
        print(
            "[make-dmg-background] AppKit is unavailable: {0}. Use a python with pyobjc.".format(
                error
            ),
            file=sys.stderr,
        )
        return 1
    write_png(rep, args.output)
    print("[make-dmg-background] wrote {0}".format(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
