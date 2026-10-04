"""Bolo visual identity: warm paper, ink, clay, and a custom b mark.

Shared native drawing and semantic colors for setup, overlay, and installer.
The mark joins a writing stem to an open curved bowl with a separate terminal.
AppKit imports stay local so runtime-independent tests can import the module.
Coordinates use a 100-unit top-down grid and scale to each native surface.
"""

PAPER = (0.965, 0.949, 0.914)
INK = (0.153, 0.141, 0.129)
CLAY = (0.714, 0.267, 0.157)
CLAY_LIGHT = (0.898, 0.451, 0.302)
NIGHT = (0.133, 0.125, 0.114)
NIGHT_SURFACE = (0.188, 0.176, 0.161)
LIGHT_MUTED = (0.384, 0.357, 0.322)
DARK_MUTED = (0.733, 0.698, 0.635)
MOSS = (0.294, 0.424, 0.306)
MOSS_LIGHT = (0.584, 0.733, 0.537)

# The voice waveform accent: nine bars of varying heights, drawn in the
# brand moss green. Levels are fractions of the drawing height.
WAVEFORM_BARS = (0.24, 0.5, 0.82, 1.0, 0.58, 0.86, 0.42, 0.64, 0.28)


def palette(dark=False):
    return {
        "background": NIGHT if dark else PAPER,
        "surface": NIGHT_SURFACE if dark else (0.992, 0.980, 0.953),
        "text": PAPER if dark else INK,
        "muted": DARK_MUTED if dark else LIGHT_MUTED,
        "accent": CLAY_LIGHT if dark else CLAY,
        "button": CLAY,
        "button_text": (1.0, 0.988, 0.961),
        "border": (0.353, 0.329, 0.294) if dark else (0.784, 0.757, 0.710),
        "success": MOSS_LIGHT if dark else MOSS,
        "error": (0.965, 0.584, 0.443) if dark else (0.639, 0.188, 0.118),
        "disabled": (0.263, 0.247, 0.224) if dark else (0.784, 0.757, 0.710),
    }


def is_dark(appearance):
    from AppKit import NSAppearanceNameAqua, NSAppearanceNameDarkAqua
    return appearance.bestMatchFromAppearancesWithNames_(
        [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
    ) == NSAppearanceNameDarkAqua


def native_color(rgb, alpha=1.0):
    from AppKit import NSColor
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, alpha)


def draw_waveform(x, y, w, h, color):
    """Draw the voice waveform accent: rounded bars in one color.

    Nine pill bars of varying heights, vertically centered, sharing one
    rhythm; the dictation voice made visible. Coordinates follow the
    caller's view (top-down when the view is flipped), like draw_mark.
    """
    from AppKit import NSBezierPath, NSMakeRect

    count = len(WAVEFORM_BARS)
    unit = w / float(2 * count - 1)
    for index, level in enumerate(WAVEFORM_BARS):
        bar_h = max(1.5, h * level)
        top = y + (h - bar_h) / 2.0
        bar = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x + index * 2 * unit, top, unit, bar_h),
            unit / 2.0, unit / 2.0
        )
        native_color(color).setFill()
        bar.fill()


def draw_mark(x, y, size, ink=INK, terminal=CLAY, flipped=True):
    """Draw the custom b silhouette, using the same path as the SVG master."""
    from AppKit import NSBezierPath, NSMakeRect
    scale = size / 100.0
    def point(px, py):
        return (x + px * scale, y + (py if flipped else 100 - py) * scale)
    path = NSBezierPath.bezierPath()
    path.moveToPoint_(point(26, 12))
    path.lineToPoint_(point(26, 61))
    path.curveToPoint_controlPoint1_controlPoint2_(
        point(49, 86), point(26, 77), point(35, 86))
    path.curveToPoint_controlPoint1_controlPoint2_(
        point(76, 60), point(64, 86), point(76, 75))
    path.curveToPoint_controlPoint1_controlPoint2_(
        point(50, 35), point(76, 45), point(65, 35))
    path.curveToPoint_controlPoint1_controlPoint2_(
        point(26, 48), point(39, 35), point(29, 41))
    path.setLineWidth_(14 * scale)
    path.setLineCapStyle_(1)
    path.setLineJoinStyle_(1)
    native_color(ink).setStroke()
    path.stroke()
    top = y + (13 if flipped else 100 - 13 - 14) * scale
    block = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(x + 68 * scale, top, 14 * scale, 14 * scale),
        3 * scale, 3 * scale)
    native_color(terminal).setFill()
    block.fill()
