#!/usr/bin/env python3
"""Generate the Bolo DMG background image as a 660x400 PNG.

A warm paper installer surface with the brand mark (lowercase "bolo"
wordmark using the same bolo_brand.draw_mark path the app icon uses),
finder-readable dark ink labels, and a restrained clay drag arrow from
the Bolo.app slot on the left to the Applications slot on the right.
Light surface so Finder's black icon labels under Bolo.app and
Applications stay readable without relying on any undocumented Finder
text color property. Run under the same build-time venv that renders
the app icon:

    python3 scripts/make-dmg-background.py --output build/dmg/background.png
"""

import argparse
import os
import sys

# Shared brand module lives beside the runtime, above these build scripts.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WIDTH = 660
HEIGHT = 400

# Icon slots the Finder layout (scripts/make-dmg-layout.py) pins:
# Bolo.app on the LEFT, the Applications symlink on the RIGHT, so the
# drag arrow points rightward in the direction of the drag.
ICON_SIZE = 80
BOLO_POS = (150, 232)
APPLICATIONS_POS = (462, 232)

# The mark sits beside a lowercase "bolo" wordmark, both drawn from the
# shared bolo_brand primitives. The serif wordmark matches setup headings.
MARK_GRID_SIZE = 38.0
WORDMARK = "bolo"
WORDMARK_FONT_SIZE = 46
WORDMARK_CENTER_Y = 64
WORDMARK_GAP = 14.0

SUBTITLE = "Drag Bolo to Applications"
SUBTITLE_FONT_SIZE = 18
SUBTITLE_CENTER_Y = 116
# Second instruction line: the next step after the drag.
SUBTITLE2 = "then open it from Applications"
SUBTITLE2_FONT_SIZE = 12
SUBTITLE2_CENTER_Y = 142
SUBTITLE2_COLOR_ALPHA = 0.85

# The arrow rides the icon band's centerline (y 232), its tail starting
# just right of the Bolo icon and its tip stopping just left of the
# Applications icon, so it sits between them rather than under either.
ARROW_SHAFT_H = 7.0
ARROW_TAIL_X = 244.0
ARROW_TIP_X = 388.0
ARROW_HEAD_W = 24.0
ARROW_HEAD_H = 24.0
ARROW_ALPHA = 0.65


def icon_center(slot):
    """Center of one icon slot's cell: position is the cell's top-left."""
    return (slot[0] + ICON_SIZE / 2.0, slot[1] + ICON_SIZE / 2.0)


def arrow_center():
    """Center of the arrow at the icon band's height (y 232 centerline)."""
    left = icon_center(BOLO_POS)
    right = icon_center(APPLICATIONS_POS)
    return ((left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0)


def arrow_pieces():
    """The drag arrow as (shaft_rect, head_points), pointing from Bolo's
    slot toward the Applications slot (left to right). The shaft spans
    from the tail (just right of the Bolo icon) to just before the head,
    on the same horizontal band as both icons."""
    center_y = arrow_center()[1]
    shaft_w = (ARROW_TIP_X - ARROW_HEAD_W) - ARROW_TAIL_X
    shaft = (
        ARROW_TAIL_X,
        center_y - ARROW_SHAFT_H / 2.0,
        shaft_w,
        ARROW_SHAFT_H,
    )
    head = (
        (ARROW_TIP_X - ARROW_HEAD_W, center_y - ARROW_HEAD_H / 2.0),
        (ARROW_TIP_X - ARROW_HEAD_W, center_y + ARROW_HEAD_H / 2.0),
        (ARROW_TIP_X, center_y),
    )
    return shaft, head


def render(width=WIDTH, height=HEIGHT):
    """Render the background into an AppKit bitmap image rep."""
    from AppKit import (
        NSAttributedString,
        NSBezierPath,
        NSBitmapImageRep,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSGraphicsContext,
        NSMakeRect,
        NSMutableParagraphStyle,
        NSParagraphStyleAttributeName,
    )
    from bolo_brand import (
        CLAY,
        INK,
        PAPER,
        draw_mark,
        native_color,
    )

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

    # Warm paper surface (Finder labels stay black-on-light, readable).
    native_color(PAPER).setFill()
    NSBezierPath.fillRect_(NSMakeRect(0, 0, width, height))

    # Brand mark + lowercase wordmark centered as one unit. Bitmap
    # contexts are not flipped, so top-down center lines convert at draw.
    word_font = (NSFont.fontWithName_size_("Georgia", WORDMARK_FONT_SIZE)
                 or NSFont.systemFontOfSize_(WORDMARK_FONT_SIZE))
    word_attributed = attributed(
        WORDMARK, word_font, native_color(INK), 0
    )
    word_w, word_h = word_attributed.size()
    mark_width = MARK_GRID_SIZE  # on the grid; draw_mark multiplies by /100
    unit_width = mark_width + WORDMARK_GAP + word_w
    unit_left = (width - unit_width) / 2.0
    draw_mark(
        unit_left,
        height - WORDMARK_CENTER_Y - MARK_GRID_SIZE / 2.0,
        MARK_GRID_SIZE,
        ink=INK,
        terminal=CLAY,
        flipped=False,
    )
    word_attributed.drawAtPoint_(
        (
            unit_left + mark_width + WORDMARK_GAP,
            height - WORDMARK_CENTER_Y - word_h / 2.0,
        )
    )

    # Drag arrow: shaft plus a rightward head, in clay at restrained
    # strength so it does not overpower the mark or the Finder labels.
    shaft, head = arrow_pieces()
    native_color(CLAY, ARROW_ALPHA).setFill()
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

    subtitle_attributed = attributed(
        SUBTITLE,
        NSFont.systemFontOfSize_(SUBTITLE_FONT_SIZE),
        native_color(INK),
        1,
    )
    subtitle_w, subtitle_h = subtitle_attributed.size()
    subtitle_attributed.drawAtPoint_(
        ((width - subtitle_w) / 2.0, height - SUBTITLE_CENTER_Y - subtitle_h / 2.0)
    )

    # Second line: the next step, dimmer so the drag line stays primary.
    subtitle2_attributed = attributed(
        SUBTITLE2,
        NSFont.systemFontOfSize_(SUBTITLE2_FONT_SIZE),
        native_color(INK, SUBTITLE2_COLOR_ALPHA),
        1,
    )
    subtitle2_w, subtitle2_h = subtitle2_attributed.size()
    subtitle2_attributed.drawAtPoint_(
        ((width - subtitle2_w) / 2.0, height - SUBTITLE2_CENTER_Y - subtitle2_h / 2.0)
    )

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
        default="background.png",
        help="output PNG path (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        rep = render()
    except ImportError as error:
        print(
            "[dmg-background] AppKit is unavailable: {0}. Use a python with pyobjc.".format(error),
            file=sys.stderr,
        )
        return 1
    write_png(rep, args.output)
    print("[dmg-background] wrote {0}".format(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
