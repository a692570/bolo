#!/usr/bin/env python3
"""Generate the Bolo app icon as a 1024px PNG.

Draws the brand mark (the host-owned path in bolo_brand.draw_mark, the
same geometry as the SVG master) on a warm clay rounded tile: an ivory
"b" silhouette with an ink terminal block, a subtle darker rim, and a
thin top light. No gloss, no gradients, no waveform, no microphone.
Geometry lives in pure functions so tests assert the real mark placement
without importing AppKit. Run under any Python with pyobjc (the DMG build
creates one from the bundled python-build-standalone runtime):

    python3 scripts/make_icon.py --output build/icon/bolo-icon-1024.png
"""

import argparse
import os
import sys

# Shared brand module lives beside the runtime, above these build scripts.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bolo_brand import CLAY, INK, PAPER, draw_mark, native_color

SIZE = 1024
# Geometry constants are defined for the 1024-pixel canvas; render()
# scales them with size so --size 16/32 still produces a valid tile.
CORNER_RADIUS_1024 = 224.0
RECT_INSET_1024 = 16.0
EDGE_WIDTH_1024 = 10.0
TOP_LIGHT_HEIGHT_1024 = 30.0

# The 100-unit mark grid scaled so the visible ink is ~58% of the tile's
# height. draw_mark is called with `size * MARK_GRID_FRACTION`; the ink
# itself spans grid units x 19..83, y 5..93.
MARK_GRID_FRACTION = 0.66
INK_LEFT = 19.0
INK_RIGHT = 83.0
INK_TOP = 5.0
INK_BOTTOM = 93.0

# Warm clay tile (host token). The mark's silhouette and terminal use
# the same shared tokens: ivory paper for the b, ink for the terminal
# block. The terminal is ink, not a third orange, so it reads as the
# stop-cap the host spec froze.
MARK_INK = PAPER
MARK_TERMINAL = INK
# Edge treatments are monochrome-alpha over the clay color: lighter
# than tile toward the top, darker toward the bottom, never gloss.
EDGE_DARKEN = 0.16
EDGE_ALPHA = 0.5
TOP_LIGHT_LIFT = 0.20
TOP_LIGHT_ALPHA = 0.16


def _mark_scale(size):
    return size * MARK_GRID_FRACTION / 100.0


def mark_origin(size=SIZE):
    """Top-left (x, y) in top-down pixels for the mark's grid origin,
    optically centered: the ink box centers at the tile's center."""
    scale = _mark_scale(size)
    center_x = (INK_LEFT + INK_RIGHT) / 2.0
    center_y = (INK_TOP + INK_BOTTOM) / 2.0
    return (size / 2.0 - center_x * scale, size / 2.0 - center_y * scale)


def mark_ink_box(size=SIZE):
    """The mark's visible ink box (x, y, w, h) in top-down pixels."""
    scale = _mark_scale(size)
    x, y = mark_origin(size)
    return (
        x + INK_LEFT * scale,
        y + INK_TOP * scale,
        (INK_RIGHT - INK_LEFT) * scale,
        (INK_BOTTOM - INK_TOP) * scale,
    )


def tile_box(size=SIZE):
    """The tile's (x, y, w, h); `y` is the top in top-down pixels."""
    inset = RECT_INSET_1024 * (size / SIZE)
    side = size - 2 * inset
    return (inset, inset, side, side)


def render(size=SIZE):
    """Render the icon into an AppKit bitmap image rep."""
    from AppKit import (
        NSBezierPath,
        NSBitmapImageRep,
        NSGraphicsContext,
        NSMakeRect,
    )

    def mix(base, amount):
        if amount >= 0:
            target = (1.0, 1.0, 1.0)
        else:
            target = (0.0, 0.0, 0.0)
            amount = -amount
        return tuple(
            min(1.0, max(0.0, channel + (target_c - channel) * amount))
            for channel, target_c in zip(base, target)
        )

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

    scale_geometry = size / SIZE
    inset = RECT_INSET_1024 * scale_geometry
    corner_radius = CORNER_RADIUS_1024 * scale_geometry
    edge_width = EDGE_WIDTH_1024 * scale_geometry
    top_light_height = TOP_LIGHT_HEIGHT_1024 * scale_geometry
    side = size - 2 * inset

    def rounded(x, y, w, h, radius):
        return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x, y, w, h), radius, radius
        )

    # Warm clay tile.
    native_color(CLAY).setFill()
    rounded(inset, inset, side, side, corner_radius).fill()

    # Subtle darker rim: one translucent stroke inset from the tile's
    # edge, weight toward the bottom half via the color mix only.
    rim = rounded(
        inset + edge_width / 2.0,
        inset + edge_width / 2.0,
        side - edge_width,
        side - edge_width,
        corner_radius - edge_width / 2.0,
    )
    rim.setLineWidth_(edge_width)
    native_color(mix(CLAY, -EDGE_DARKEN), EDGE_ALPHA).setStroke()
    rim.stroke()

    # Thin top light: the same inner stroke clipped to a band along the
    # tile's top, drawn lighter. One stroke, two clips: no gradient.
    NSGraphicsContext.saveGraphicsState()
    band = NSMakeRect(inset, inset + side - top_light_height, side, top_light_height)
    NSBezierPath.bezierPathWithRect_(band).addClip()
    rim.setLineWidth_(edge_width)
    native_color(mix(CLAY, TOP_LIGHT_LIFT), TOP_LIGHT_ALPHA).setStroke()
    rim.stroke()
    NSGraphicsContext.restoreGraphicsState()

    # The brand mark: ivory b silhouette plus ink terminal block, drawn
    # from the shared path so the icon matches every other surface.
    origin_x, origin_y = mark_origin(size)
    draw_mark(
        origin_x,
        size - origin_y - size * MARK_GRID_FRACTION,
        size * MARK_GRID_FRACTION,
        ink=MARK_INK,
        terminal=MARK_TERMINAL,
        flipped=False,
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
