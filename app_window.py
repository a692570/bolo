#!/usr/bin/env python3
"""AppKit window for Bolo setup, status, and learned-words.

The Rust runtime spawns this script and writes one JSON payload line to
stdin; further lines update the onboarding wizard (try-it, trust
replies). Closing the window (button or close box) exits 0, and the
onboarding session writes ~/.bolo/onboarding.json only when the user
actually finishes the wizard from the ready screen after a real
dictation has been inserted. Skipping, closing early, or leaving
before the practice insert lands leaves the marker absent so the next
launch reopens the wizard.

The onboarding Accessibility row carries an Open Privacy & Security
Settings button and the runtime answers the window's trust_check
requests with the real Accessibility reading of the helper that
actually pastes; a toggle that is on while the runtime is still
untrusted (the failure users see today) cannot flip the row green.

Every unfinished wizard step also carries a visible "Finish later"
secondary button (left side of the bottom button row, with the
Escape key equivalent) that dismisses setup through its own controller
callback: the marker stays absent and no permission is granted. The
ready screen keeps only the strict finish path.

The live window runs the canonical AppKit lifecycle
(NSApplication.run() -> launch -> timer activation -> key window),
per
https://developer.apple.com/documentation/appkit/nsapplication/finishlaunching()
and https://developer.apple.com/documentation/appkit/nsapplication/run().
A repeating NSTimer is the only non-native integration point: it
services stdin updates, the Accessibility trust poll, and stops
NSApplication.run() (NSApplication.stop(_:)) when the user closes
the window or the parent closes stdin. A posted application-defined
event lets stop() return from a timer callback without another click.
"""

import json
import os
import select
import subprocess
import sys
import textwrap
import time
import warnings

import bolo_brand
import bolo_env

MARKER_VERSION = 2
MARKER_FILE = os.path.expanduser("~/.bolo/onboarding.json")
WIDTH = 720
MARGIN = 36
TEXT_X = MARGIN + 14 + 16
TOP_PAD = 28
LABEL_LINE_H = 22
DETAIL_LINE_H = 18
ROW_GAP = 18
WELCOME_GAP = 14
BUTTON_AREA_H = 76
DETAIL_WRAP_AT = 78
KEY_FIELD_W = 400
KEY_FIELD_H = 28
VALIDATE_BUTTON_W = 110
KEY_ENTRY_NAME = "ASSEMBLYAI_API_KEY"
PRACTICE_FIELD_W = 480
PRACTICE_FIELD_H = 64

WELCOME_HEADLINE_SIZE = 20.0

# Brand row: the custom b mark plus the lowercase bolo lockup, drawn
# from bolo_brand (same geometry as the SVG master and the icon).
BRAND_ROW_H = 26
BRAND_GAP = 12
BRAND_MARK_SIZE = 22
BRAND_LOCKUP_SIZE = 17.0
BRAND_LOCKUP_PAD = 6

# Hero try-it row: when the payload marks the try-it step as the one thing
# left, its label grows and its instruction line brightens.
HERO_LABEL_SIZE = 16.0

DONE_BUTTON_W = 148
DONE_BUTTON_H = 32

# Onboarding screens: one primary action per screen, one topic per screen.
SCREEN_WELCOME = "welcome"
SCREEN_CONNECT_SPEECH = "connect_speech"
SCREEN_MICROPHONE = "microphone"
SCREEN_ACCESSIBILITY = "accessibility"
SCREEN_PRACTICE = "practice"
SCREEN_READY = "ready"
SCREEN_ORDER = (
    SCREEN_WELCOME,
    SCREEN_CONNECT_SPEECH,
    SCREEN_MICROPHONE,
    SCREEN_ACCESSIBILITY,
    SCREEN_PRACTICE,
    SCREEN_READY,
)

# Copy for each screen. All lines are short enough to wrap at most once
# and every screen keeps exactly one primary action.
SCREEN_WELCOME_LINE = (
    "Bolo listens while you hold a key, then types what you said wherever "
    "your cursor is. This short setup makes sure it works."
)
SCREEN_WELCOME_DETAIL = (
    "You can quit at any point and come back from the Bolo menu bar item."
)
SCREEN_CONNECT_SPEECH_DETAIL = (
    "Bolo sends audio to AssemblyAI to turn speech into text. Paste your "
    "API key, then Validate to connect. You can skip and add it later."
)
SCREEN_MICROPHONE_DETAIL = (
    "Bolo records only while you hold the dictation key; it stops and "
    "pastes the moment you let go. Nothing records in between."
)
SCREEN_MICROPHONE_ACTION_TITLE = "Test the microphone"
SCREEN_ACCESSIBILITY_ACTION_TITLE = "Open Privacy & Security Settings"
SCREEN_ACCESSIBILITY_DETAIL = (
    "Bolo needs permission to insert text for you. Open Privacy & Security, "
    "find Accessibility (this Mac may show Device Control and Data Access), "
    "then enable Bolo in the list."
)
SCREEN_ACCESSIBILITY_GRANT_HINT = (
    "If Bolo is missing from the list, use the + button and add Bolo from "
    "your Applications folder. Return here after the change and Bolo checks "
    "the actual runtime automatically."
)
SCREEN_PRACTICE_DETAIL = (
    "Hold your dictation key, say a sentence, then release. Bolo types it "
    "into the field below, so you can see it work without leaving setup."
)
SCREEN_PRACTICE_PLACEHOLDER = (
    "Hold your dictation key, speak, release. Text appears here."
)
SCREEN_READY_DETAIL = (
    "Dictation is set up. Hold your key, speak, release. Bolo is in the "
    "menu bar any time you need it."
)
SCREEN_PRIMARY_BUTTONS = {
    SCREEN_WELCOME: "Continue",
    SCREEN_CONNECT_SPEECH: "Continue",
    SCREEN_MICROPHONE: "Continue",
    SCREEN_ACCESSIBILITY: "Open Privacy & Security Settings",
    SCREEN_PRACTICE: "Continue",
    SCREEN_READY: "Start using Bolo",
}
SCREEN_BACK_TITLE = "Back"
PRACTICE_ROW_LABEL = "Try it in the field"
PRACTICE_STATE_LABEL = "Insert succeeded."
PRACTICE_PENDING_DETAIL = "Waiting for your first dictation."

# Onboarding Accessibility copy. Trust is checked against the same helper
# pipeline the runtime pastes with, so the row can only turn green when a
# real insert would succeed; a stale or mismatched grant keeps the warn
# state with re-add instructions.
GRANTED_DETAIL = "Granted. Bolo can insert text now."
SOURCE_GRANTED_DETAIL = (
    "Granted. Run ./restart.sh so Bolo picks up the grant."
)
ASSEMBLYAI_LIST_URL = "https://api.assemblyai.com/v2/transcript?limit=1"
KEY_VALIDATION_TIMEOUT_S = 6.0

# Accessibility grant flow: the onboarding warn row carries a button that
# deep-links System Settings to the Accessibility list (Apple never prompts
# for this permission, so the row must send the user there itself), the
# window polls trust while that row is showing, and a granted flip turns
# the row green. Bundle mode then swaps in a Restart Bolo button; source
# mode has no supervised relaunch, so the granted detail keeps the
# ./restart.sh instruction instead of a button.
ACCESSIBILITY_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)
ACCESSIBILITY_LABEL = "Accessibility"
OPEN_SETTINGS_TITLE = SCREEN_ACCESSIBILITY_ACTION_TITLE
RESTART_TITLE = "Restart Bolo"
GRANTED_DETAIL = "Granted."
SOURCE_GRANTED_DETAIL = "Granted. Run ./restart.sh so Bolo picks up the grant."
TRUST_POLL_INTERVAL_S = 2.0
ACTION_KINDS = ("open_settings", "restart")
ACTION_BUTTON_W = 210
ACTION_BUTTON_H = 24
ACTION_BUTTON_GAP = 6

# Learned-corrections file the learning window edits. The Rust runtime owns
# the file too (it reads pairs at startup and re-checks the mtime at each
# recording start), so deletions here take effect without a restart.
LEARNED_FILE = os.path.expanduser("~/.bolo/learned_vocabulary.json")


def marker_payload(now_ms=None):
    """Marker document written when onboarding completes."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return {"version": MARKER_VERSION, "completed_at_ms": now_ms}


def write_marker(path, payload, required_version=None):
    """Persist the completion marker with private permissions.

    `required_version` guards migration: a marker on disk whose version
    is older than the current schema is rewritten only with the current
    version, and read paths (Rust `onboarding_status_at`) treat an old
    marker as needing setup again.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def parse_update(line):
    """Decode one stdin update line; None when it is not an object."""
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def apply_window_background(view, dark=False):
    """Paint the semantic warm brand background on the content view.

    Offscreen cacheDisplay rendering of a bare content view has no
    window chrome to inherit a background from, so the preview bitmap
    composites against black and light/dark appearances flip the text
    contrast. Painting the brand background on the content view keeps
    offscreen PNGs faithful to the onscreen window.
    """
    from AppKit import NSColor

    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(
        bolo_brand.native_color(bolo_brand.palette(dark=dark)["background"]).CGColor()
    )


def wrap_lines(text, width=DETAIL_WRAP_AT):
    """Wrap one detail string for display; empty text stays one empty line."""
    if not text:
        return [""]
    return textwrap.wrap(text, width=width) or [""]


def is_bundle_mode():
    """Whether this window runs under the distributed Bolo.app bundle.

    The bundle launcher exports BOLO_BUNDLE_MODE=1 for the runtime, which
    its helper children (this window included) inherit.
    """
    return os.environ.get("BOLO_BUNDLE_MODE") == "1"


def row_action_kind(row):
    """Validated action kind carried by a row, or None when it has none."""
    action = row.get("action") if isinstance(row, dict) else None
    if not isinstance(action, dict):
        return None
    kind = action.get("kind")
    return kind if kind in ACTION_KINDS else None


def action_button_title(row):
    """Button label for a row action: the payload title when it carries
    one, else the default title for the kind."""
    action = row.get("action")
    if isinstance(action, dict) and isinstance(action.get("title"), str) and action["title"]:
        return action["title"]
    defaults = {
        "open_settings": OPEN_SETTINGS_TITLE,
        "restart": RESTART_TITLE,
    }
    return defaults.get(row_action_kind(row)) or OPEN_SETTINGS_TITLE


def open_settings_command():
    """Shell argv that opens System Settings on the Accessibility list."""
    return ["open", ACCESSIBILITY_SETTINGS_URL]


def targeted_restart_command(bundle, parent_pid=None):
    """Argv that signals exactly the parent process, or None.

    The parent of this helper is whatever process spawned it (the Rust
    runtime in a live launch); a kill -0 probe confirms the PID is alive
    and signalable before any signal is sent, and the callback uses
    only this path. Source mode keeps None. The probe proves the parent
    is alive, not the identity of the binary.
    """
    if not bundle:
        return None
    pid = parent_pid if parent_pid is not None else os.getppid()
    if not is_parent_alive(pid):
        return None
    return ["kill", "-USR1", str(pid)]


def is_parent_alive(pid):
    """Whether the given PID exists and accepts signals from us.

    A kill -0 probe: it proves the parent is alive and signalable right
    now, nothing more. It does not verify which binary owns the PID.
    """
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def accessibility_self_trusted():
    """Whether macOS trusts this helper process for Accessibility.

    A different helper process can carry a different trust reading, and
    the runtime's paste-path check is the authority, so this reading is
    never authoritative. The window always polls the runtime for the
    real reading (see `request_runtime_trust_check`); this helper only
    stays around as a fallback for source-mode previews and tests.
    Returns None when the check itself fails.
    """
    try:
        import ApplicationServices as ax

        return bool(ax.AXIsProcessTrusted())
    except Exception:
        return None


def request_runtime_trust_check(out=sys.stdout):
    """Ask the runtime for its own Accessibility trust reading.

    The runtime spawned this window; macOS attributes the Accessibility
    grant to the app bundle (or the source interpreter) the runtime
    runs, not to this helper. The runtime's reply is the only source of
    truth the onboarding row should trust, and the row flips green only
    on that reply.
    """
    out.write('{"type":"trust_check"}\n')
    out.flush()


def parse_trust_reply(line):
    """Decode one stdin trust reply; None when it is not a trust_reply."""
    parsed = parse_update(line)
    if not isinstance(parsed, dict):
        return None
    if parsed.get("type") != "trust_reply":
        return None
    return bool(parsed.get("trusted")), bool(parsed.get("restart_needed"))


def accessibility_flip(row, trusted_now, bundle):
    """Row update after one Accessibility trust poll; None when unchanged.

    `row` is the row as last displayed, `trusted_now` the fresh reading
    from `accessibility_self_trusted`, and `bundle` whether the window
    runs under Bolo.app. Flipping to granted turns the row green with a
    "Granted." line: bundle mode swaps the settings button for a Restart
    Bolo action, source mode drops the button and keeps the ./restart.sh
    instruction in the detail.
    """
    if trusted_now is not True:
        return None
    if row.get("label") != ACCESSIBILITY_LABEL or row.get("state") == "ok":
        return None
    if bundle:
        return {
            "label": ACCESSIBILITY_LABEL,
            "detail": GRANTED_DETAIL,
            "state": "ok",
            "action": {"kind": "restart", "title": RESTART_TITLE},
        }
    return {
        "label": ACCESSIBILITY_LABEL,
        "detail": SOURCE_GRANTED_DETAIL,
        "state": "ok",
        "action": None,
    }


def screen_detail(screen):
    """Welcome / detail copy for one onboarding screen."""
    return {
        SCREEN_WELCOME: SCREEN_WELCOME_DETAIL,
        SCREEN_CONNECT_SPEECH: SCREEN_CONNECT_SPEECH_DETAIL,
        SCREEN_MICROPHONE: SCREEN_MICROPHONE_DETAIL,
        SCREEN_ACCESSIBILITY: SCREEN_ACCESSIBILITY_DETAIL,
        SCREEN_PRACTICE: SCREEN_PRACTICE_DETAIL,
        SCREEN_READY: SCREEN_READY_DETAIL,
    }.get(screen, "")


def screen_rows(screen, trust_state="ok", bundle=True, hotkey="left_option"):
    """Rows for one onboarding screen; pure so tests and previews share
    the exact payload the live window renders.

    `trust_state` is the real runtime trust reading ("ok", "warn", or
    "unavailable"), not a UI assumption: only an "ok" reading turns the
    Accessibility row green.
    """
    if screen == SCREEN_WELCOME:
        return []
    if screen == SCREEN_CONNECT_SPEECH:
        return [
            {
                "label": "Speech to text",
                "detail": SCREEN_CONNECT_SPEECH_DETAIL,
                "state": "pending",
            }
        ]
    if screen == SCREEN_MICROPHONE:
        return [
            {
                "label": "Microphone",
                "detail": SCREEN_MICROPHONE_DETAIL,
                "state": "pending",
            }
        ]
    if screen == SCREEN_ACCESSIBILITY:
        if trust_state == "ok":
            return [
                {
                    "label": ACCESSIBILITY_LABEL,
                    "detail": GRANTED_DETAIL,
                    "state": "ok",
                    "action": None,
                }
            ]
        if trust_state == "warn":
            row = {
                "label": ACCESSIBILITY_LABEL,
                "detail": " ".join(
                    [SCREEN_ACCESSIBILITY_DETAIL, SCREEN_ACCESSIBILITY_GRANT_HINT]
                ),
                "state": "warn",
                "action": {"kind": "open_settings"},
            }
            return [row]
        return [
            {
                "label": ACCESSIBILITY_LABEL,
                "detail": (
                    "Reinstall Bolo by downloading the latest Bolo DMG again."
                    if bundle
                    else "Helper unavailable. Run ./install.sh, then ./restart.sh."
                ),
                "state": "warn",
            }
        ]
    if screen == SCREEN_PRACTICE:
        return [
            {
                "label": PRACTICE_ROW_LABEL,
                "detail": " ".join([SCREEN_PRACTICE_DETAIL, PRACTICE_PENDING_DETAIL]),
                "state": "pending",
            }
        ]
    if screen == SCREEN_READY:
        return [
            {"label": "Dictation", "detail": SCREEN_READY_DETAIL, "state": "ok"}
        ]
    return []


def screen_welcome_line(screen):
    """Welcome copy for one onboarding screen."""
    if screen == SCREEN_WELCOME:
        return SCREEN_WELCOME_LINE
    return screen_detail(screen)


def screen_primary_button(screen, practice_done=False):
    """Title of the one primary action on a screen."""
    if screen == SCREEN_PRACTICE and not practice_done:
        return "Waiting for dictation"
    return SCREEN_PRIMARY_BUTTONS.get(screen, "Continue")


# ---- Polished wizard rendering path ----
#
# The wizard renders on its own spacious fixed-size window instead of the
# cramped generic row list: a small brand row, a step counter with a
# progress bar, one large headline, short body copy, one primary action,
# and screen-specific controls (key entry, practice field, trust status).
# All screens render in place in the same window, so advancing never
# closes the helper and the runtime keeps one process for the whole
# wizard. The content view paints the native window background itself so
# offscreen cacheDisplay previews match the onscreen window in both light
# and dark appearances.

WIZARD_WIDTH = 680
WIZARD_HEIGHT = 480
WIZARD_MARGIN = 48
WIZARD_BODY_WRAP_AT = 70
WIZARD_SAMPLE_WRAP_AT = 58
WIZARD_HEADLINE_SIZE = 27.0
WIZARD_HEADLINE_H = 36
WIZARD_HEADLINE_Y = 104
WIZARD_BODY_Y = 150
WIZARD_BODY_SIZE = 15.0
WIZARD_BODY_LINE_H = 23
WIZARD_BODY_GAP = 10
WIZARD_STEP_Y = 62
WIZARD_STEP_H = 18
WIZARD_PROGRESS_Y = 88
WIZARD_PRIMARY_W = 200
WIZARD_PRIMARY_H = 38
WIZARD_PRIMARY_Y = WIZARD_HEIGHT - WIZARD_PRIMARY_H - 26
WIZARD_PRIMARY_RADIUS = 8.0
WIZARD_FIELD_W = 430
WIZARD_FIELD_H = 30
WIZARD_PRACTICE_W = 520
WIZARD_PRACTICE_H = 120
WIZARD_STATUS_H = 22
WIZARD_LINK_H = 24
# Welcome focal object geometry: a tactile dictation key with a short
# sample sentence using the actual configured hotkey.
WIZARD_KEY_ART_W = 240
WIZARD_KEY_ART_H = 110
WIZARD_KEY_ART_Y = 202
WIZARD_SAMPLE_Y = 322

WIZARD_HEADLINES = {
    SCREEN_WELCOME: "Welcome to Bolo",
    SCREEN_CONNECT_SPEECH: "Connect speech to text",
    SCREEN_MICROPHONE: "Microphone",
    SCREEN_ACCESSIBILITY: "Let Bolo type for you",
    SCREEN_PRACTICE: "Try your first dictation",
    SCREEN_READY: "You're all set",
}
WIZARD_STEP_NAMES = {
    SCREEN_WELCOME: "Welcome",
    SCREEN_CONNECT_SPEECH: "Speech key",
    SCREEN_MICROPHONE: "Microphone",
    SCREEN_ACCESSIBILITY: "Accessibility",
    SCREEN_PRACTICE: "Practice",
    SCREEN_READY: "Ready",
}
WIZARD_PRIMARY_TITLES = {
    SCREEN_WELCOME: "Continue",
    SCREEN_MICROPHONE: "Continue",
    SCREEN_PRACTICE: "Continue",
    SCREEN_READY: "Start using Bolo",
}
WIZARD_OPEN_SETTINGS_TITLE = "Open Settings"
WIZARD_CONTINUE_TITLE = "Continue"
WIZARD_KEY_LABEL = "AssemblyAI API key"
WIZARD_KEY_STATUS_INITIAL = "Key not validated yet."
WIZARD_ACCESSIBILITY_PENDING_STATUS = "Permission not confirmed yet."
WIZARD_ACCESSIBILITY_GRANTED_STATUS = "Granted. Bolo can insert text now."
WIZARD_PRACTICE_STATUS = "Waiting for your first dictation."
WIZARD_PRACTICE_PLACEHOLDER = "Text you dictate appears here."
WIZARD_LINK_TITLE = "Get an API key"
WIZARD_READY_STATUS = "Setup verified."
WIZARD_FINISH_LATER_TITLE = "Finish later"
# Secondary "Finish later" button geometry: left edge of the same
# bottom button row as the primary, matching the primary's height and
# baseline so the pair reads as one row.
WIZARD_FINISH_LATER_W = 150
WIZARD_FINISH_LATER_H = WIZARD_PRIMARY_H

HOTKEY_DISPLAY_NAMES = {
    "left_option": "Left Option",
    "right_option": "Right Option",
    "right_shift": "Right Shift",
    "fn": "Fn",
    "caps_lock": "Caps Lock",
}

# Primary buttons use the burnt clay brand color with warm ivory text
# (contrast above the accessibility threshold on both light and dark
# backgrounds); the clay light tint stays for progress fill and status
# cues.
def _primary_button_rgb():
    return bolo_brand.CLAY

def _blocked_button_rgb(dark=False):
    return bolo_brand.palette(dark=dark)["disabled"]

def _pending_dot_rgb():
    return (0.46, 0.46, 0.48)

ASSEMBLYAI_DASHBOARD_URL = "https://www.assemblyai.com/app/api-keys"


def _calibrated(rgb, alpha=1.0):
    """One calibrated NSColor from an RGB tuple."""
    from AppKit import NSColor

    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        rgb[0], rgb[1], rgb[2], alpha
    )


def _label_color(color):
    """Coerce a label color argument to a native NSColor.

    bolo_brand.palette returns RGB tuples; AppKit wants NSColor. This is
    the one boundary where both shapes arrive, so plain tuples and lists
    become native colors while real NSColor objects pass through.
    """
    if isinstance(color, (tuple, list)):
        return bolo_brand.native_color(color)
    return color


def heading_font(size, bold=False):
    """Serif headline font: Georgia with a system fallback.

    fontWithName_size_ returns a falsy font when the family is missing,
    so the fallback returns the real system font instead of a None that
    would crash the label path.
    """
    from AppKit import NSFont

    wanted = "Georgia-Bold" if bold else "Georgia"
    font = NSFont.fontWithName_size_(wanted, size)
    if font is not None:
        return font
    if not bold:
        font = NSFont.fontWithName_size_("Georgia", size)
        if font is not None:
            return font
    return NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)


def brand_mark_rect(x, y, size):
    """Frame for the bolo b mark inside one content view."""
    return (x, y, size, size)


def brand_mark_draw(view):
    """Draw the custom b mark inside one brand-mark view.

    Shared by the wizard brand row and the overlay mark so setup and
    dictation show the same silhouette. The terminal block stays clay
    and the stem-bowl stroke stays ink.
    """
    import AppKit

    bounds = view.bounds()
    dark = getattr(view, "_dark", False)
    pal = bolo_brand.palette(dark=dark)
    size = min(bounds.size.width, bounds.size.height)
    bolo_brand.draw_mark(
        bounds.origin.x,
        bounds.origin.y,
        size,
        ink=pal["text"],
        terminal=pal["accent"],
        flipped=view.isFlipped(),
    )
    return AppKit


def hotkey_display_name(hotkey):
    """Human name for the configured dictation key, e.g. Left Option."""
    if not isinstance(hotkey, str) or not hotkey:
        return "your dictation key"
    return HOTKEY_DISPLAY_NAMES.get(hotkey) or hotkey.replace("_", " ").capitalize()


def wizard_step_line(screen, position, count):
    """Progress line: step number, total, and the screen's own name."""
    name = WIZARD_STEP_NAMES.get(screen, "Setup")
    return "Step {0} of {1} · {2}".format(position, count, name)


def wizard_body_paragraphs(screen, facts, practice_done=False):
    """Short body copy for one screen; each entry is one paragraph.

    The Accessibility warn copy names both pane titles this Mac may show,
    explains adding Bolo when it is missing, and covers the stale-grant
    case (toggle already on while the real runtime is still untrusted).
    """
    hotkey = hotkey_display_name((facts or {}).get("hotkey"))
    if screen == SCREEN_WELCOME:
        return [
            "Hold a key to speak. Bolo types where your cursor is.",
            "A quick setup, then a sentence to try it.",
        ]
    if screen == SCREEN_CONNECT_SPEECH:
        return [
            "Bolo sends audio to AssemblyAI to turn speech into text. "
            "Paste your API key below, then click Validate.",
            "Bolo saves your key locally and uses it to connect to AssemblyAI.",
        ]
    if screen == SCREEN_MICROPHONE:
        return [
            "Bolo records only while you hold {0}. When you let go, it "
            "stops and types what you said.".format(hotkey),
            "You'll allow microphone access when you try dictation in "
            "Practice. Click Allow when macOS asks.",
        ]
    if screen == SCREEN_ACCESSIBILITY:
        if (facts or {}).get("trust") == "ok":
            return ["Bolo is trusted and can insert text wherever your cursor is."]
        return [
            "Bolo needs macOS permission to type for you.",
            "1. Open Settings → Privacy & Security.",
            "2. Find Accessibility, or Device Control and Data Access.",
            "3. Turn on Bolo. If it is missing, click + and add Bolo from Applications.",
            "Already enabled? Remove Bolo with −, then add it again. "
            "Bolo checks permission automatically when you return.",
        ]
    if screen == SCREEN_PRACTICE:
        return [
            "Hold {0}, say a sentence, then release. Bolo types it into "
            "the field below, so you can prove it works before finishing "
            "setup.".format(hotkey)
        ]
    if screen == SCREEN_READY:
        return [
            "Hold {0}, speak, release. That is dictation.".format(hotkey),
            "Bolo lives in your menu bar. Status, learned words, and setup "
            "are all in its menu.",
        ]
    return []


def wizard_primary_title(screen, facts, practice_done=False):
    """Primary button title; the Accessibility screen keeps it short.

    The practice screen keeps the future action ("Continue") as its
    label even while gated: the status line below the field already says
    dictation is pending, and the button should name what it will do.
    """
    if screen == SCREEN_ACCESSIBILITY:
        if (facts or {}).get("trust") == "ok":
            return WIZARD_CONTINUE_TITLE
        return WIZARD_OPEN_SETTINGS_TITLE
    return WIZARD_PRIMARY_TITLES.get(screen, WIZARD_CONTINUE_TITLE)


def wizard_primary_action(screen, facts):
    """Target-action for the primary button on one screen."""
    if screen == SCREEN_ACCESSIBILITY and (facts or {}).get("trust") != "ok":
        return "openAccessibility:"
    if screen == SCREEN_READY:
        return "finish:"
    return "advance:"


def wizard_primary_enabled(screen, facts, practice_done=False):
    """One gate per screen; nothing fakes readiness.

    The key screen stays disabled until a real validation succeeds, the
    practice screen until the runtime reports a genuine inserted
    dictation, and the Accessibility screen only while untrusted (where
    the primary opens Settings instead of advancing).
    """
    if screen == SCREEN_CONNECT_SPEECH:
        return False
    if screen == SCREEN_PRACTICE:
        return practice_done is True
    return True


def wizard_trust_primary(trusted, restart_needed):
    """Primary (title, action) after one runtime trust reply.

    Untrusted keeps the settings deep link. Trusted with no restart
    needed turns straight into Continue, so a live grant never traps the
    user in a restart loop. A grant the runtime can only pick up after a
    relaunch offers the targeted Restart Bolo action instead.
    """
    if not trusted:
        return (WIZARD_OPEN_SETTINGS_TITLE, "openAccessibility:")
    if restart_needed:
        return (RESTART_TITLE, "restartBolo:")
    return (WIZARD_CONTINUE_TITLE, "advance:")


def build_wizard_ui(payload, preview=False):
    """Build the polished progressive-setup wizard window.

    A fixed 680x480 window with the native window background painted on
    the content view (so offscreen renders match onscreen light/dark),
    one large headline per screen, one primary action, and generous
    spacing. Screens advance in place: the primary button re-renders the
    next screen into the same window instead of closing the helper, and
    the runtime keeps answering trust checks while the Accessibility
    screen is showing.
    """
    from AppKit import (
        NSAttributedString,
        NSMutableParagraphStyle,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBezelStyleRounded,
        NSButton,
        NSDate,
        NSColor,
        NSDefaultRunLoopMode,
        NSFont,
        NSFontWeightBold,
        NSFontWeightMedium,
        NSFontWeightRegular,
        NSMakeRect,
        NSRunLoop,
        NSObject,
        NSSecureTextField,
        NSTextAlignmentCenter,
        NSTextAlignmentLeft,
        NSTextField,
        NSView,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskTitled,
    )
    from objc import ObjCPointerWarning

    warnings.filterwarnings("ignore", category=ObjCPointerWarning)
    FlippedView, BrandMarkView, DictationKeyView, WindowController = (
        _appkit_classes_cached(NSView, NSObject)
    )

    facts = runtime_facts(raw_wizard_payload(payload))
    queue = list(onboarding_screen_queue(facts))
    start = payload.get("screen")
    if start not in queue:
        start = queue[0]

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    # Preview builders do not launch NSApplication. The live helper
    # starts its native lifecycle with run() in run_event_loop.
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, WIZARD_WIDTH, WIZARD_HEIGHT),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
        2,
        False,
    )
    window.setTitle_(payload.get("title") or "Set up Bolo")
    window.setReleasedWhenClosed_(False)
    controller = WindowController.alloc().init()
    window.setDelegate_(controller)

    refs = {"queue": queue, "screen": start, "facts": dict(facts)}

    def palette_now():
        """Semantic brand colors for the window's current appearance."""
        dark = False
        try:
            effective = window.effectiveAppearance()
            dark = bolo_brand.is_dark(effective)
        except Exception:
            dark = False
        return bolo_brand.palette(dark=dark), dark
    pal, dark_now = palette_now()
    muted_color = bolo_brand.native_color(pal["muted"])
    text_color_now = bolo_brand.native_color(pal["text"])
    colors = {
        "ok": bolo_brand.native_color(bolo_brand.palette(dark_now)["success"]),
        "warn": bolo_brand.native_color(bolo_brand.palette(dark_now)["error"]),
        "pending": bolo_brand.native_color(_pending_dot_rgb()),
    }
    primary_color = bolo_brand.native_color(_primary_button_rgb())
    blocked_color = bolo_brand.native_color(_blocked_button_rgb(dark_now))
    accent = primary_color
    STATE["colors"] = colors

    def make_primary(title, action):
        button = NSButton.buttonWithTitle_target_action_(
            title, controller, action
        )
        button.setBordered_(False)
        button.setWantsLayer_(True)
        button.layer().setCornerRadius_(WIZARD_PRIMARY_RADIUS)
        button.setKeyEquivalent_("\r")
        paragraph = NSMutableParagraphStyle.alloc().init()
        paragraph.setAlignment_(NSTextAlignmentCenter)
        button.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                title,
                {
                    "NSFont": NSFont.systemFontOfSize_weight_(
                        15.0, NSFontWeightMedium
                    ),
                    "NSColor": bolo_brand.native_color(
                        bolo_brand.palette(dark=False)["button_text"]
                    ),
                    "NSParagraphStyle": paragraph,
                },
            )
        )
        button.setFrame_(
            NSMakeRect(
                WIZARD_WIDTH - WIZARD_MARGIN - WIZARD_PRIMARY_W,
                WIZARD_PRIMARY_Y,
                WIZARD_PRIMARY_W,
                WIZARD_PRIMARY_H,
            )
        )
        return button

    def make_finish_later():
        """Exit unfinished setup with a native button or Escape."""
        button = NSButton.buttonWithTitle_target_action_(
            WIZARD_FINISH_LATER_TITLE, controller, "finishLater:"
        )
        button.setBezelStyle_(NSBezelStyleRounded)
        button.setKeyEquivalent_("\x1b")
        button.setFrame_(
            NSMakeRect(
                WIZARD_MARGIN,
                WIZARD_PRIMARY_Y,
                WIZARD_FINISH_LATER_W,
                WIZARD_FINISH_LATER_H,
            )
        )
        return button

    def status_row(content, y, text, state):
        dot = NSView.alloc().initWithFrame_(
            NSMakeRect(WIZARD_MARGIN, y + 5, 10, 10)
        )
        dot.setWantsLayer_(True)
        dot.layer().setCornerRadius_(5)
        dot.layer().setBackgroundColor_(
            colors.get(state, colors["pending"]).CGColor()
        )
        content.addSubview_(dot)
        label = NSTextField.labelWithString_(text)
        label.setFrame_(
            NSMakeRect(
                WIZARD_MARGIN + 20,
                y,
                WIZARD_WIDTH - 2 * WIZARD_MARGIN - 20,
                WIZARD_STATUS_H,
            )
        )
        label.setFont_(NSFont.systemFontOfSize_weight_(13.0, NSFontWeightRegular))
        label.setTextColor_(muted_color)
        content.addSubview_(label)
        return dot, label

    pal, dark_now = palette_now()
    colors = {
        "ok": bolo_brand.native_color(pal["success"]),
        "warn": bolo_brand.native_color(pal["error"]),
        "pending": bolo_brand.native_color(_pending_dot_rgb()),
    }
    primary_color = bolo_brand.native_color(_primary_button_rgb())
    blocked_color = bolo_brand.native_color(_blocked_button_rgb(dark_now))
    STATE["colors"] = colors

    def render(screen):
        """Build one screen's content view and swap it into the window."""
        screen_facts = refs["facts"]
        # The queue is computed once when the window builds and never
        # recomputed, so validating a key or a trust update mid-flow can
        # never renumber the steps: "Step 2 of 6" stays "Step 2 of 6".
        queue_now = refs["queue"]
        position = queue_now.index(screen) + 1
        count = len(queue_now)
        content = FlippedView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIZARD_WIDTH, WIZARD_HEIGHT)
        )
        apply_window_background(content, dark=dark_now)
        pal = palette_now()[0]
        text_color = bolo_brand.native_color(pal["text"])
        muted_color = bolo_brand.native_color(pal["muted"])

        def label(text, y, h, font, color, x=WIZARD_MARGIN, width=None):
            field = NSTextField.labelWithString_(text)
            field.setFrame_(
                NSMakeRect(
                    x, y, width or (WIZARD_WIDTH - WIZARD_MARGIN - x), h
                )
            )
            field.setFont_(font)
            field.setTextColor_(_label_color(color))
            field.setEditable_(False)
            field.setSelectable_(True)
            field.setBezeled_(False)
            field.setDrawsBackground_(False)
            content.addSubview_(field)
            return field

        body_font = NSFont.systemFontOfSize_weight_(
            WIZARD_BODY_SIZE, NSFontWeightRegular
        )
        body_font_small = NSFont.systemFontOfSize_weight_(
            13.0, NSFontWeightRegular
        )
        mono_font = NSFont.monospacedDigitSystemFontOfSize_weight_(
            12.0, NSFontWeightMedium
        )

        # Brand row: the custom b mark plus the lowercase bolo lockup,
        # drawn from bolo_brand so setup, overlay, and the icon agree.
        mark_view = BrandMarkView.alloc().initWithFrame_(
            NSMakeRect(WIZARD_MARGIN, 24, BRAND_MARK_SIZE, BRAND_MARK_SIZE)
        )
        mark_view.configureWithDark_(dark_now)
        content.addSubview_(mark_view)
        wordmark = label(
            (payload.get("brand") or "bolo").lower(),
            24,
            BRAND_MARK_SIZE,
            heading_font(16.0, bold=True),
            text_color_now,
            x=WIZARD_MARGIN + BRAND_MARK_SIZE + BRAND_LOCKUP_PAD,
            width=140,
        )
        wordmark.setSelectable_(False)

        # Step counter plus a thin progress bar: clear, non-text progress.
        label(
            wizard_step_line(screen, position, count),
            WIZARD_STEP_Y,
            WIZARD_STEP_H,
            body_font_small,
            muted_color,
        )
        track = NSView.alloc().initWithFrame_(
            NSMakeRect(
                WIZARD_MARGIN,
                WIZARD_PROGRESS_Y,
                WIZARD_WIDTH - 2 * WIZARD_MARGIN,
                4,
            )
        )
        track.setWantsLayer_(True)
        track.layer().setCornerRadius_(2)
        track.layer().setBackgroundColor_(
            bolo_brand.native_color(pal["border"]).CGColor()
        )
        content.addSubview_(track)
        fill = NSView.alloc().initWithFrame_(
            NSMakeRect(
                WIZARD_MARGIN,
                WIZARD_PROGRESS_Y,
                (WIZARD_WIDTH - 2 * WIZARD_MARGIN) * position / count,
                4,
            )
        )
        fill.setWantsLayer_(True)
        fill.layer().setCornerRadius_(2)
        fill.layer().setBackgroundColor_(
            bolo_brand.native_color(pal["accent"]).CGColor()
        )
        content.addSubview_(fill)

        # Large serif headline, then short body copy once.
        label(
            WIZARD_HEADLINES.get(screen, "Set up Bolo"),
            WIZARD_HEADLINE_Y,
            WIZARD_HEADLINE_H,
            heading_font(WIZARD_HEADLINE_SIZE, bold=True),
            text_color_now,
        )
        practice_done = STATE.get("practice_done") is True
        y = float(WIZARD_BODY_Y)
        for paragraph in wizard_body_paragraphs(screen, screen_facts, practice_done):
            lines = wrap_lines(paragraph, width=WIZARD_BODY_WRAP_AT)
            label(
                "\n".join(lines),
                y,
                len(lines) * WIZARD_BODY_LINE_H,
                body_font,
                text_color_now,
            )
            y += len(lines) * WIZARD_BODY_LINE_H + WIZARD_BODY_GAP

        if screen == SCREEN_WELCOME:
            # Focal object: a tactile dictation key and one sample sentence
            # using the configured hotkey. One quiet crafted object instead
            # of decorative bars; no animation, no marketing ornament.
            key_art = DictationKeyView.alloc().initWithFrame_(
                NSMakeRect(
                    WIZARD_MARGIN,
                    WIZARD_KEY_ART_Y,
                    WIZARD_KEY_ART_W,
                    WIZARD_KEY_ART_H,
                )
            )
            key_art.setKeyArt_(
                (pal, hotkey_display_name(screen_facts.get("hotkey")))
            )
            content.addSubview_(key_art)
            sample = " ".join(
                [
                    "Hold {0} and say:".format(
                        hotkey_display_name(screen_facts.get("hotkey"))
                    ),
                    '“Meeting moved to three. See you there.”',
                ]
            )
            sample_lines = wrap_lines(sample, width=WIZARD_SAMPLE_WRAP_AT)
            label(
                "\n".join(sample_lines),
                WIZARD_SAMPLE_Y,
                len(sample_lines) * WIZARD_BODY_LINE_H,
                body_font,
                muted_color,
            )

        key_refs = None
        practice_refs = None
        trust_refs = None

        if screen == SCREEN_CONNECT_SPEECH:
            label(
                WIZARD_KEY_LABEL,
                y,
                WIZARD_STEP_H,
                body_font_small,
                muted_color,
            )
            y += WIZARD_STEP_H + 8
            field = NSSecureTextField.alloc().initWithFrame_(
                NSMakeRect(
                    WIZARD_MARGIN,
                    y,
                    WIZARD_PRACTICE_W - 110 - 12,
                    WIZARD_FIELD_H,
                )
            )
            key_spec = payload.get("key_entry")
            placeholder = (
                key_spec.get("placeholder")
                if isinstance(key_spec, dict)
                else None
            )
            field.cell().setPlaceholderString_(
                placeholder or "Paste your AssemblyAI API key"
            )
            content.addSubview_(field)
            validate_button = NSButton.buttonWithTitle_target_action_(
                "Validate", controller, "validateKey:"
            )
            validate_button.setBezelStyle_(NSBezelStyleRounded)
            validate_button.setFrame_(
                NSMakeRect(
                    WIZARD_MARGIN + WIZARD_PRACTICE_W - 110,
                    y,
                    110,
                    WIZARD_FIELD_H,
                )
            )
            content.addSubview_(validate_button)
            y += WIZARD_FIELD_H + 12
            key_dot, key_label = status_row(
                content, y, WIZARD_KEY_STATUS_INITIAL, "pending"
            )
            y += WIZARD_STATUS_H + 8
            link = NSButton.buttonWithTitle_target_action_(
                WIZARD_LINK_TITLE, controller, "openDashboard:"
            )
            link.setBordered_(False)
            link.setFont_(NSFont.systemFontOfSize_weight_(
                13.0, NSFontWeightMedium
            ))
            link.setFrame_(
                NSMakeRect(WIZARD_MARGIN, y, 180, WIZARD_LINK_H)
            )
            link_paragraph = NSMutableParagraphStyle.alloc().init()
            link_paragraph.setAlignment_(NSTextAlignmentLeft)
            link.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    WIZARD_LINK_TITLE,
                    {
                        "NSFont": NSFont.systemFontOfSize_weight_(
                            13.0, NSFontWeightMedium
                        ),
                        "NSColor": bolo_brand.native_color(pal["accent"]),
                        "NSParagraphStyle": link_paragraph,
                    },
                )
            )
            content.addSubview_(link)

            def on_valid(key_dot=key_dot):
                key_dot.layer().setBackgroundColor_(colors["ok"].CGColor())
                # A fresh key is read dynamically by every speech
                # request, so the same runtime can transcribe
                # immediately: unlock Continue and clear the missing
                # flag, then the wizard advances in place with the
                # frozen queue intact.
                screen_facts["key_missing"] = False
                primary = refs.get("primary_button")
                if primary is not None:
                    primary.setEnabled_(True)
                    primary.layer().setBackgroundColor_(
                        primary_color.CGColor()
                    )

            key_refs = {
                "field": field,
                "dot": key_dot,
                "detail_label": key_label,
                "on_valid": on_valid,
            }

        if screen == SCREEN_MICROPHONE:
            mic_row = microphone_devices_row(screen_facts.get("microphones"))
            if mic_row is not None:
                status_row(content, y, mic_row["detail"], mic_row["state"])
                y += WIZARD_STATUS_H + 8

        if screen == SCREEN_ACCESSIBILITY:
            state = "ok" if screen_facts.get("trust") == "ok" else "warn"
            text = (
                WIZARD_ACCESSIBILITY_GRANTED_STATUS
                if state == "ok"
                else WIZARD_ACCESSIBILITY_PENDING_STATUS
            )
            trust_dot, trust_label = status_row(content, y, text, state)
            trust_refs = {"dot": trust_dot, "label": trust_label}
            y += WIZARD_STATUS_H + 8

        if screen == SCREEN_PRACTICE:
            practice_field = NSTextField.alloc().initWithFrame_(
                NSMakeRect(
                    WIZARD_MARGIN, y, WIZARD_PRACTICE_W, WIZARD_PRACTICE_H
                )
            )
            practice_spec = payload.get("practice")
            placeholder = (
                practice_spec.get("placeholder")
                if isinstance(practice_spec, dict)
                else None
            )
            practice_field.cell().setPlaceholderString_(
                placeholder or WIZARD_PRACTICE_PLACEHOLDER
            )
            practice_field.setFont_(body_font)
            content.addSubview_(practice_field)
            y += WIZARD_PRACTICE_H + 10
            practice_dot, practice_label = status_row(
                content, y, WIZARD_PRACTICE_STATUS, "pending"
            )
            practice_refs = {
                "dot": practice_dot,
                "detail_label": practice_label,
                "field": practice_field,
            }

        if screen == SCREEN_READY:
            status_row(content, y, WIZARD_READY_STATUS, "ok")

        title = wizard_primary_title(screen, screen_facts, practice_done)
        action = wizard_primary_action(screen, screen_facts)
        enabled = wizard_primary_enabled(screen, screen_facts, practice_done)
        button = make_primary(title, action)
        button.setEnabled_(enabled)
        button.layer().setBackgroundColor_(
            (primary_color if enabled else blocked_color).CGColor()
        )
        content.addSubview_(button)
        finish_later = None
        if screen != SCREEN_READY:
            finish_later = make_finish_later()
            content.addSubview_(finish_later)

        window.setContentView_(content)
        refs.update(
            {
                "screen": screen,
                "primary_button": button,
                "finish_later_button": finish_later,
                "key_refs": key_refs,
                "practice_refs": practice_refs,
                "trust_refs": trust_refs,
            }
        )
        STATE["screen"] = screen
        STATE["key_refs"] = key_refs
        # Trust polling follows the screen: entering the Accessibility
        # step while untrusted (re)starts the poll so run_event_loop
        # requests checks and Open Settings can become Continue; leaving
        # or a granted reply stops it, and a revoked grant mid-practice
        # is caught by the paste-time insert_trusted gate, not by a poll.
        if screen == SCREEN_ACCESSIBILITY and screen_facts.get("trust") != "ok":
            if accessibility is not None:
                accessibility["active"] = True
                accessibility["next_check"] = (
                    time.monotonic() + TRUST_POLL_INTERVAL_S
                )
        elif accessibility is not None:
            accessibility["active"] = False
        # Practice: make the editable field first responder immediately so
        # a hotkey dictation lands in the field the user is proving
        # dictation with, without an extra click.
        if screen == SCREEN_PRACTICE and practice_refs is not None:
            window.makeFirstResponder_(practice_refs["field"])
        return content

    def advance():
        """Render the next screen into the same window; None at the end.

        A granted Accessibility step stays green as the flow moves on;
        the insert_trusted gate at paste time is what catches a grant
        revoked during practice, so no extra poll is needed there.
        """
        queue_now = refs["queue"]
        current = refs.get("screen")
        if current not in queue_now:
            return None
        following = queue_now.index(current) + 1
        if following >= len(queue_now):
            return None
        render(queue_now[following])
        return refs["screen"]

    def apply_trust(trusted, restart_needed):
        """Apply one runtime trust reply to the Accessibility screen.

        Only the runtime's own reading of the helper that actually pastes
        reaches this path, so a Settings toggle that is on while the
        runtime is still untrusted keeps the warn state and the settings
        deep link instead of faking readiness.
        """
        if refs.get("screen") != SCREEN_ACCESSIBILITY:
            return
        screen_facts = refs["facts"]
        trust_refs = refs.get("trust_refs") or {}
        button = refs.get("primary_button")
        if trusted:
            screen_facts["trust"] = "ok"
            if trust_refs.get("dot") is not None:
                trust_refs["dot"].layer().setBackgroundColor_(
                    colors["ok"].CGColor()
                )
            if trust_refs.get("label") is not None:
                trust_refs["label"].setStringValue_(
                    WIZARD_ACCESSIBILITY_GRANTED_STATUS
                )
            title, action = wizard_trust_primary(True, restart_needed)
            if button is not None:
                button.setTitle_(title)
                button.setAction_(action)
                button.setEnabled_(True)
                button.layer().setBackgroundColor_(primary_color.CGColor())
            if accessibility is not None:
                accessibility["active"] = False
        else:
            # Untrusted reply: keep checking so returning from Settings
            # flips the state without the user re-opening this window.
            # A grant granted earlier in this session can also be
            # revoked, and the next reply must recover the warn state.
            screen_facts["trust"] = "warn"
            title, action = wizard_trust_primary(False, False)
            if button is not None:
                button.setTitle_(title)
                button.setAction_(action)
            if accessibility is not None:
                accessibility["active"] = True
                accessibility["next_check"] = (
                    time.monotonic() + TRUST_POLL_INTERVAL_S
                )

    accessibility = {}
    render(start)
    accessibility.setdefault(
        "next_check", time.monotonic() + TRUST_POLL_INTERVAL_S
    )
    ui = {
        "window": window,
        "app": app,
        "run_loop": (NSRunLoop, NSDate, NSDefaultRunLoopMode),
        "accessibility": accessibility,
        "apply_trust": apply_trust,
        "advance": advance,
        "refs": refs,
        "primary_color": primary_color,
        "accent": accent,
        "content": window.contentView(),
        "plan": {"width": WIZARD_WIDTH, "height": WIZARD_HEIGHT},
        # Retain the controller: the delegate and button targets are
        # weak references from AppKit, and a collected controller means
        # dead buttons even with a working event pump.
        "controller": controller,
    }
    if preview:
        return ui
    app.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)
    return ui


def practice_complete_from_update(update):
    """Whether a stdin update line reports a genuine inserted dictation.

    The runtime only sends this after a real insert succeeded, so the
    marker logic can trust it. An explicit `insert_trusted: false` from
    the runtime (the runtime rechecked Accessibility at paste time and
    found it untrusted) refuses the update: the row must not flip green
    when the actual paste path is still failing. Legacy updates without
    the field are rejected too, so older runtime builds cannot fake
    readiness in the new wizard.
    """
    if not isinstance(update, dict):
        return False
    if update.get("try_it_complete") is not True:
        return False
    insert_trusted = update.get("insert_trusted")
    return insert_trusted is True


def onboarding_screen_queue(payload):
    """Ordered wizard screens derived from the runtime payload.

    The key screen appears only when the runtime reports the key missing,
    and Accessibility always appears: its row rechecks the real helper
    trust, so an already-granted install still verifies instead of
    assuming. Reads both flat and nested wizard shapes so tests and the
    Rust payload agree.
    """
    screens = [SCREEN_WELCOME]
    facts = runtime_facts(payload)
    if facts["key_missing"]:
        screens.append(SCREEN_CONNECT_SPEECH)
    screens.append(SCREEN_MICROPHONE)
    screens.append(SCREEN_ACCESSIBILITY)
    screens.append(SCREEN_PRACTICE)
    screens.append(SCREEN_READY)
    return screens


def microphone_devices_row(microphones=None):
    """Devices row for the microphone screen; None hides it."""
    if microphones is None:
        return None
    if microphones == 0:
        return {
            "label": "Devices",
            "detail": "No microphone found. Allow microphone access for Bolo "
            "in System Settings > Privacy & Security > Microphone.",
            "state": "warn",
        }
    word = "microphone" if microphones == 1 else "microphones"
    return {
        "label": "Devices",
        "detail": "{0} {1} found.".format(microphones, word),
        "state": "ok",
    }


def runtime_facts(payload):
    """Facts the runtime reports, with defaults kept test-friendly.

    Accepts both the wizard nested dict (the Rust payload) and a flat
    dict (the host preview script), so tests and previews can build
    either shape without a wrapper. `trust` is the runtime's own
    reading of the helper that actually pastes; the window never
    upgrades it from the settings UI state.
    """
    wizard = payload.get("wizard") if isinstance(payload.get("wizard"), dict) else {}
    key_missing = payload.get("key_missing")
    if key_missing is None:
        key_missing = wizard.get("key_missing")
    if key_missing is None:
        key_missing = payload.get("key_entry") is not None
    trust = payload.get("accessibility_state")
    if trust is None:
        trust = wizard.get("accessibility_state")
    if trust not in ("ok", "warn", "unavailable"):
        trust = "warn"
    microphones = payload.get("microphones")
    if microphones is None:
        microphones = wizard.get("microphones")
    if microphones is None:
        microphones = 1
    hotkey = payload.get("hotkey")
    if not isinstance(hotkey, str) or not hotkey:
        hotkey = wizard.get("hotkey")
    if not isinstance(hotkey, str) or not hotkey:
        hotkey = "left_option"
    return {
        "key_missing": key_missing is True,
        "trust": trust,
        "microphones": microphones,
        "hotkey": hotkey,
    }


def build_screen_payload(facts, screen, position, count, practice_done=False):
    """One generic-renderer payload for a wizard screen.

    Previews and tests use this shape: it names the current screen, the
    queue position, and the practice state, while the wizard renderer
    derives all copy and controls from the facts. `write_marker` is
    always false here so nothing touches the marker.
    """
    rows = screen_rows(
        screen,
        trust_state=facts["trust"],
        bundle=is_bundle_mode(),
        hotkey=facts["hotkey"],
    )
    mic_row = microphone_devices_row(facts["microphones"])
    if screen == SCREEN_MICROPHONE and mic_row is not None:
        rows.append(mic_row)
    payload = {
        "mode": "onboarding",
        "wizard_screen": True,
        "title": "Set up Bolo",
        "welcome": screen_welcome_line(screen),
        "brand": "BOLO",
        "rows": rows,
        "button": screen_primary_button(screen, practice_done=practice_done),
        "screen": screen,
        "screen_position": position,
        "screen_count": count,
        "practice_done": practice_done,
        # Facts ride along so the wizard renderer derives the same
        # queue, trust reading, and hotkey from the payload the preview
        # builds as the runtime's own payload would carry.
        "wizard": {
            "key_missing": facts["key_missing"],
            "accessibility_state": facts["trust"],
            "microphones": facts["microphones"],
            "hotkey": facts["hotkey"],
        },
        "write_marker": False,
    }
    if screen == SCREEN_CONNECT_SPEECH:
        payload["key_entry"] = {
            "index": 0,
            "placeholder": "Paste your AssemblyAI API key",
        }
    if screen == SCREEN_PRACTICE:
        payload["practice"] = {"placeholder": WIZARD_PRACTICE_PLACEHOLDER}
    return payload


def raw_wizard_payload(payload):
    """Fields build_screen_payload needs, kept separate from the renderer
    payload so tests can build either."""
    facts = runtime_facts(payload)
    return {
        "key_missing": facts["key_missing"],
        "accessibility_state": facts["trust"],
        "microphones": facts["microphones"],
        "hotkey": facts["hotkey"],
    }


def should_write_marker(payload, screen, practice_done, user_closed):
    """Pure decision: only finish the wizard on the ready screen, after
    a real inserted dictation, when the user closed the window (not
    stdin dying), and when the runtime still wants the marker written.
    Any other case leaves the marker absent so the next launch reopens
    the wizard instead of pretending Bolo is set up.
    """
    if not user_closed:
        return False
    if not payload.get("write_marker"):
        return False
    if screen != SCREEN_READY:
        return False
    return practice_done


def write_learned_file(path, payload):
    """Atomic learned-vocabulary write mirroring the Rust runtime's writer:
    indent 2 with a trailing newline, private permissions, rename into
    place so a crash never leaves a half-written file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def delete_learned_pair(path, misheard):
    """Remove one learned pair from the corrections file.

    Returns ``(removed, error)`` where error is plain display text for the
    window when something failed; both values are None-free: a successful
    removal has ``error=None``.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return False, "The learned-words file is gone."
    except Exception:
        return False, "Could not read the learned-words file."
    corrections = data.get("corrections") if isinstance(data, dict) else None
    if not isinstance(corrections, dict) or misheard not in corrections:
        return False, "That correction is no longer saved."
    del corrections[misheard]
    try:
        write_learned_file(path, data)
    except Exception:
        return False, "Could not save the learned-words file."
    return True, None


def learning_display_payload(pairs, error, spec):
    """Pure display payload for the learning window: the welcome copy picked
    from the spec (empty state when no pairs remain), one row per pair
    showing ``misheard -> corrected``, and the plain error line last."""
    rows = []
    for pair in pairs:
        misheard = pair.get("misheard") or ""
        corrected = pair.get("corrected") or ""
        rows.append(
            {"label": "{0} -> {1}".format(misheard, corrected), "detail": "", "state": "ok"}
        )
    if error:
        rows.append({"label": error, "detail": "", "state": "warn"})
    welcome = spec.get("hint_welcome") if pairs else spec.get("empty_welcome")
    display = {
        "brand": "BOLO",
        "welcome": welcome or "",
        "rows": rows,
    }
    return display


def key_entry_index(payload):
    """Row index of the API-key entry field, or None when it is not shown."""
    spec = payload.get("key_entry")
    if not isinstance(spec, dict):
        return None
    index = spec.get("index")
    rows = payload.get("rows")
    if (
        isinstance(index, int)
        and isinstance(rows, list)
        and 0 <= index < len(rows)
    ):
        return index
    return None


def classify_key_response(status):
    """Map one AssemblyAI HTTP status to a validation verdict.

    A 200 from the transcript-list endpoint proves the key authenticates
    without spending any audio on transcription; 401 is a definite reject.
    Anything else is treated as a transient error rather than a rejection.
    """
    if status == 200:
        return "valid"
    if status == 401:
        return "invalid"
    return "error"


def fetch_assemblyai_status(key, url=ASSEMBLYAI_LIST_URL, timeout=KEY_VALIDATION_TIMEOUT_S):
    """Return the HTTP status code for a key probe; raise on network failure."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"Authorization": key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode()
    except urllib.error.HTTPError as error:
        return error.code


def validate_and_save_key(
    key,
    env_path=None,
    fetch=fetch_assemblyai_status,
):
    """Validate one API key and persist it when AssemblyAI accepts it.

    Returns ``(verdict, detail)`` where detail is display text for the
    onboarding row. `fetch` is injectable so tests cover the 200/401/other
    branches without network access; it must raise on transport errors.
    """
    key = (key or "").strip()
    if not key:
        return "empty", "Paste your AssemblyAI API key first, then click Validate."
    if env_path is None:
        env_path = bolo_env.default_env_path()
    try:
        status = fetch(key)
    except Exception:
        return (
            "unreachable",
            "Could not reach api.assemblyai.com. Check your internet "
            "connection and try again.",
        )
    verdict = classify_key_response(status)
    if verdict == "valid":
        bolo_env.write_env_value(env_path, KEY_ENTRY_NAME, key)
        print("[app-window] key validation: valid", file=sys.stderr, flush=True)
        return (
            "valid",
            "Key saved. Click Continue to finish setup and try dictation.",
        )
    if verdict == "invalid":
        return (
            "invalid",
            "AssemblyAI rejected that key. Double-check it and try again.",
        )
    return (
        "error",
        "AssemblyAI returned an unexpected response. Try again in a moment.",
    )



def plan_layout(payload):
    """Pure geometry pass: line wrapping and vertical placement.

    Returns a plan with the content height, the button's y position, and
    per-row y positions for the label, optional key-entry field, and
    wrapped detail lines, all in flipped-content coordinates (origin
    top-left). The row named by ``payload["key_entry"]["index"]`` reserves
    extra vertical space for the text field plus Validate button. A truthy
    ``payload["brand"]`` reserves the brand row above the welcome lines.
    A row carrying a valid ``action`` reserves one button line under its
    detail for the row's tappable action.
    """
    rows = payload.get("rows", [])
    brand = bool(payload.get("brand"))
    welcome = payload.get("welcome") or ""
    welcome_lines = wrap_lines(welcome) if welcome else []
    key_index = key_entry_index(payload)
    row_plans = []
    y = float(TOP_PAD)
    brand_y = None
    if brand:
        brand_y = y
        y += BRAND_ROW_H + BRAND_GAP
    if welcome_lines:
        y += len(welcome_lines) * LABEL_LINE_H + WELCOME_GAP
    for index, row in enumerate(rows):
        detail_text = row.get("detail") or ""
        # Rows without detail text (learning-window pair rows) reserve no
        # detail lines, so tappable rows stay a single tight line each.
        detail_lines = wrap_lines(detail_text) if detail_text else []
        if key_index is not None and index == key_index:
            field_y = y + LABEL_LINE_H + 2
            detail_y = field_y + KEY_FIELD_H + 4
        else:
            field_y = None
            detail_y = y + LABEL_LINE_H + 2
        # A row carrying an action reserves a button line under the detail.
        action_y = None
        if row_action_kind(row) is not None:
            action_y = detail_y + len(detail_lines) * DETAIL_LINE_H + ACTION_BUTTON_GAP
        row_plans.append(
            {
                "label_y": y,
                "field_y": field_y,
                "detail_y": detail_y,
                "detail_lines": detail_lines,
                "action_y": action_y,
            }
        )
        y = detail_y + len(detail_lines) * DETAIL_LINE_H + ROW_GAP
        if action_y is not None:
            y = action_y + ACTION_BUTTON_H + ROW_GAP
    button_y = y + 6
    height = button_y + BUTTON_AREA_H
    welcome_y = None
    if welcome_lines:
        welcome_y = TOP_PAD + ((BRAND_ROW_H + BRAND_GAP) if brand else 0)
    return {
        "brand": brand,
        "brand_y": brand_y,
        "welcome_lines": welcome_lines,
        "welcome_y": welcome_y,
        "rows": row_plans,
        "key_index": key_index,
        "button_y": button_y,
        "height": height,
    }


def read_payload():
    """Read and validate the one-line JSON payload from stdin."""
    line = sys.stdin.readline()
    if not line.strip():
        return None, "empty payload on stdin"
    try:
        payload = json.loads(line)
    except ValueError:
        return None, "payload is not valid JSON"
    if not isinstance(payload, dict):
        return None, "payload is not a JSON object"
    return payload, None


def _appkit_classes_cached(NSView, NSObject):
    """Return a (FlippedView, WindowController) pair built from one
    ObjC registration per process. PyObjC raises when a class is
    redefined in the same runtime, so the second build_ui call must
    reuse the cached classes instead of declaring them again.
    """
    cached = globals().get("_APPKIT_CLASSES")
    if cached is not None:
        return cached
    import warnings as _warnings

    from objc import ObjCPointerWarning as _Warn

    _warnings.filterwarnings("ignore", category=_Warn)

    class FlippedView(NSView):
        def isFlipped(self):
            return True

    class BrandMarkView(FlippedView):
        """The custom b mark, drawn with the shared bolo_brand path."""

        def configureWithDark_(self, dark):
            self._dark = bool(dark)
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            brand_mark_draw(self)

    class DictationKeyView(FlippedView):
        """The welcome focal object: a tactile dictation key.

        One quiet crafted object instead of decorative bars: a wide key
        cap with a subtle shadow, a clay underline while held, and the
        configured hotkey name in small caps under the surface. All
        drawing happens inside drawRect_, so repeated draws never add
        subviews to the hierarchy.
        """

        def setKeyArt_(self, art):
            pal, key_name = art
            self._pal = pal
            self._key_name = key_name or ""
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            import AppKit

            pal = getattr(self, "_pal", None) or bolo_brand.palette(dark=False)
            bounds = self.bounds()
            width = bounds.size.width
            key_h = 54.0
            key_w = width
            key_y = bounds.size.height - key_h - 20.0
            surface = bolo_brand.native_color(pal["surface"])
            border = bolo_brand.native_color(pal["border"])
            text = bolo_brand.native_color(pal["text"])
            cap = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                AppKit.NSMakeRect(0, key_y, key_w, key_h), 10.0, 10.0
            )
            surface.setFill()
            cap.fill()
            border.setStroke()
            cap.setLineWidth_(1.0)
            cap.stroke()
            # Draw the hotkey name directly with NSAttributedString: no
            # label subview, so repeated draws never grow the hierarchy.
            attrs = {
                "NSFont": AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                    14.0, AppKit.NSFontWeightMedium
                ),
                "NSColor": _label_color(text),
            }
            text_draw = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                self._key_name, attrs
            )
            text_draw.drawAtPoint_(
                AppKit.NSMakePoint(
                    (key_w - text_draw.size().width) / 2.0, key_y + 22
                )
            )


    class WindowController(NSObject):
        def finish_(self, sender):
            STATE["user_done"] = True
            STATE["finish_button"] = sender is not None

        def advance_(self, sender):
            advance = (STATE.get("wizard") or {}).get("advance")
            if advance is not None and advance() is not None:
                return
            STATE["user_done"] = True
            STATE["advanced"] = True

        def openDashboard_(self, sender):
            print(
                "[app-window] opening API key dashboard",
                file=sys.stderr,
                flush=True,
            )
            subprocess.Popen(["open", ASSEMBLYAI_DASHBOARD_URL])

        def validateKey_(self, sender):
            refs = STATE.get("key_refs")
            if not refs:
                return
            field = refs["field"]
            key = str(field.stringValue())
            verdict, detail = validate_and_save_key(key)
            state_colors = STATE.get("colors") or {}
            dot_color = {
                "valid": state_colors.get("ok"),
                "error": state_colors.get("pending"),
            }.get(verdict, state_colors.get("warn"))
            if dot_color is not None:
                refs["dot"].layer().setBackgroundColor_(dot_color.CGColor())
            refs["detail_label"].setStringValue_("\n".join(wrap_lines(detail)))
            if verdict == "valid":
                field.setEnabled_(False)
                unlock = refs.get("on_valid")
                if unlock is not None:
                    unlock()

        def deleteLearned_(self, sender):
            refs = STATE.get("learning") or {}
            rerender = refs.get("rerender")
            if not rerender:
                return
            try:
                index = int(sender.tag())
            except (TypeError, ValueError):
                return
            pairs = refs.get("pairs") or []
            if not 0 <= index < len(pairs):
                return
            misheard = pairs[index].get("misheard")
            removed, error = delete_learned_pair(
                refs.get("file") or LEARNED_FILE, misheard
            )
            if removed:
                refs["pairs"] = pairs[:index] + pairs[index + 1 :]
                refs["error"] = None
            else:
                refs["error"] = error or "Could not remove that correction."
            rerender()

        def openAccessibility_(self, sender):
            print(
                "[app-window] opening Accessibility settings",
                file=sys.stderr,
                flush=True,
            )
            subprocess.Popen(open_settings_command())

        def restartBolo_(self, sender):
            # Signal exactly the process that spawned this window, and
            # only after the kill -0 probe confirms it is alive. Never a
            # process-name pattern match, which could catch the native
            # launcher too.
            command = targeted_restart_command(is_bundle_mode())
            if command is None:
                return
            print(
                "[app-window] restarting Bolo: {0}".format(" ".join(command)),
                file=sys.stderr,
                flush=True,
            )
            subprocess.Popen(command)

        def finishLater_(self, sender):
            # End the window loop without marking setup complete.
            STATE["user_done"] = True
            STATE["finish_button"] = False

        def windowWillClose_(self, notification):
            STATE["user_done"] = True
            STATE["finish_button"] = False

    class BootstrapPump(NSObject):
        """The native-loop integration timer target.

        NSApplication.run() owns all event fetching, dispatching, and
        window flushing; this timer only crosses the boundary that
        run() cannot know about: stdin lines from the Rust parent, the
        Accessibility trust poll, and the decision to stop run()
        (NSApplication.stop(_:)) when the user closes the window or the
        parent goes away.
        """

        def tick_(self, timer):
            ui = self._ui
            app = self._app
            if not self._activated:
                self._activated = True
                if not (ui.get("preview") or (ui.get("dashboard") and (ui.get("refs") or {}).get("preview"))):
                    app.activateIgnoringOtherApps_(True)
                    ui["window"].makeKeyAndOrderFront_(None)
                    ui["window"].orderFrontRegardless()
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                line = sys.stdin.readline()
                if line == "":
                    # Parent died: stop run() and never let a stale
                    # finish look like a completed session.
                    self._eof = True
                    app.stop_(app)
                    _post_stop_wake(app)
                    return
                update = parse_update(line)
                if ui.get("dashboard"):
                    apply_dashboard_message(ui, update, line)
                if update and update.get("try_it_complete"):
                    apply_try_it_complete(ui, update)
                if update and update.get("type") == "trust_reply":
                    trusted, restart_needed = parse_trust_reply(line) or (
                        None,
                        None,
                    )
                    apply_runtime_trust_reply(
                        ui, update, trusted, restart_needed
                    )
            accessibility = self._accessibility
            if (
                accessibility is not None
                and accessibility["active"]
                and time.monotonic() >= accessibility["next_check"]
            ):
                accessibility["next_check"] = (
                    time.monotonic() + TRUST_POLL_INTERVAL_S
                )
                request_runtime_trust_check()
            if STATE["user_done"]:
                # A click, Escape, or the close box already ran: let
                # run() unwind instead of pumping forever.
                app.stop_(app)
                _post_stop_wake(app)

    cached = (FlippedView, BrandMarkView, DictationKeyView, WindowController)
    globals()["_APPKIT_CLASSES"] = cached
    # Separate registry: existing callers unpack the 4-tuple above, so
    # the pump class is exposed on its own key instead of extending the
    # tuple and breaking every native builder.
    globals()["_APPKIT_PUMP_CLASS"] = BootstrapPump
    return cached


def _post_stop_wake(app):
    """Wake run() after stop() without waiting for another user event.

    https://developer.apple.com/documentation/appkit/nsapplication/stop(_:)
    https://developer.apple.com/documentation/appkit/nsapplication/postevent(_:atstart:)
    """
    from AppKit import NSEvent, NSEventTypeApplicationDefined

    event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        NSEventTypeApplicationDefined, (0, 0), 0, 0.0, 0, None, 0, 0, 0
    )
    app.postEvent_atStart_(event, True)


def build_ui(payload, preview=False):
    """Create the AppKit window; imports stay local so tests import safely.

    Wizard onboarding payloads render through the polished progressive
    wizard path (build_wizard_ui); everything else falls through to the
    original generic renderer (build_ui_generic), whose layout and
    learned-words behavior stay untouched.
    """
    if payload.get("mode") == "dashboard":
        # The dashboard renders through its own module, imported lazily
        # so tests and onboarding windows never pay its import cost.
        import dashboard_window

        return dashboard_window.build_dashboard_ui(
            payload, preview=preview
        )
    is_wizard = (
        not isinstance(payload.get("learning"), dict)
        and payload.get("mode") == "onboarding"
        and (
            payload.get("wizard_screen") is True
            or isinstance(payload.get("wizard"), dict)
        )
    )
    if is_wizard:
        try:
            from AppKit import NSObject, NSView  # noqa: F401

            _appkit_classes_cached(NSView, NSObject)
        except ImportError:
            pass
        ui = build_wizard_ui(payload, preview=preview)
        STATE["wizard"] = {
            "advance": ui["advance"],
            "apply_trust": ui["apply_trust"],
            "refs": ui["refs"],
        }
        return ui
    return build_ui_generic(payload, preview=preview)


def build_ui_generic(payload, preview=False):
    """Create the AppKit window; imports stay local so tests import safely.

    Mode "learning" renders its rows from the pairs the runtime sends and
    makes each pair a tappable button that removes it from the
    learned-words file, re-rendering in place; mode "onboarding" with a
    "screen" key renders one progressive setup screen with a practice
    field and a single primary action; every other mode renders the
    generic label/detail rows from the payload. With `preview` the
    window is never ordered front and no app activation happens, so the
    content view can be rendered offscreen by the preview script.
    """
    from AppKit import (
        NSAttributedString,
        NSMutableParagraphStyle,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBezelStyleRounded,
        NSButton,
        NSDate,
        NSColor,
        NSDefaultRunLoopMode,
        NSFont,
        NSFontWeightMedium,
        NSFontWeightRegular,
        NSMakeRect,
        NSRunLoop,
        NSObject,
        NSSecureTextField,
        NSTextAlignmentCenter,
        NSTextAlignmentLeft,
        NSTextField,
        NSView,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskTitled,
    )
    from objc import ObjCPointerWarning

    warnings.filterwarnings("ignore", category=ObjCPointerWarning)
    from AppKit import NSAppearance

    FlippedView, BrandMarkView, DictationKeyView, WindowController = (
        _appkit_classes_cached(NSView, NSObject)
    )

    dark_now = False
    try:
        dark_now = bolo_brand.is_dark(NSAppearance.currentAppearance())
    except Exception:
        dark_now = False
    pal_now = bolo_brand.palette(dark=dark_now)

    STATE["colors"] = STATE.get("colors") or {
        "ok": bolo_brand.native_color(pal_now["success"]),
        "warn": bolo_brand.native_color(pal_now["error"]),
        "pending": bolo_brand.native_color(_pending_dot_rgb()),
    }
    colors = STATE["colors"]
    accent = bolo_brand.native_color(_primary_button_rgb())
    STATE["accent"] = accent
    blocked_color = bolo_brand.native_color(_blocked_button_rgb(dark_now))
    content_bg = bolo_brand.native_color(pal_now["background"])
    surface_bg = bolo_brand.native_color(pal_now["surface"])
    text_color = bolo_brand.native_color(pal_now["text"])
    muted_color = bolo_brand.native_color(pal_now["muted"])

    learning_spec = payload.get("learning")
    learning_spec = learning_spec if isinstance(learning_spec, dict) else None
    is_learning = learning_spec is not None
    raw_pairs = learning_spec.get("pairs") if learning_spec else None
    learning_pairs = [pair for pair in (raw_pairs or []) if isinstance(pair, dict)]
    learning_error = learning_spec.get("error") if learning_spec else None
    if is_learning:
        display = learning_display_payload(learning_pairs, learning_error, learning_spec)
    else:
        display = payload
    plan = plan_layout(display)
    try_it_index = payload.get("try_it_index")
    try_it_index = try_it_index if isinstance(try_it_index, int) else None
    hero_line = payload.get("try_it_hero")
    hero_line = hero_line if isinstance(hero_line, str) else None
    # Progressive wizard: the runtime payload names the first screen; the
    # window walks forward one screen per primary action and can only
    # finish from the ready screen after a real insert.
    wizard = (
        not is_learning
        and payload.get("mode") == "onboarding"
        and payload.get("wizard_screen") is True
    )
    if wizard and not isinstance(payload.get("screen"), str):
        payload = dict(payload)
        payload["screen"] = SCREEN_WELCOME
    screen = payload.get("screen")
    is_screen = (
        not is_learning
        and isinstance(screen, str)
        and screen in SCREEN_ORDER
        and payload.get("wizard_screen") is True
    )
    if is_screen and isinstance(payload.get("screen_position"), int):
        STATE["screen_position"] = payload["screen_position"]
    if is_screen and payload.get("screen_count"):
        STATE["screen_count"] = payload["screen_count"]
    practice_spec = payload.get("practice")
    practice_spec = practice_spec if isinstance(practice_spec, dict) else None
    practice_index = None
    if (
        is_screen
        and screen == SCREEN_PRACTICE
        and plan["rows"]
        and practice_spec is not None
    ):
        practice_index = len(plan["rows"]) - 1

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    # See build_wizard_ui: run_event_loop starts the live lifecycle.

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, WIDTH, plan["height"]),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
        2,
        False,
    )
    window.setTitle_(payload.get("title") or "Bolo")
    window.setReleasedWhenClosed_(False)
    window.setBackgroundColor_(content_bg)

    label_font = NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium)
    detail_font = NSFont.systemFontOfSize_weight_(12.0, NSFontWeightRegular)
    hero_label_font = NSFont.systemFontOfSize_weight_(
        HERO_LABEL_SIZE, NSFontWeightMedium
    )
    controller = WindowController.alloc().init()

    def build_content(content_display, content_plan):
        """Populate one content view from a display payload; reusable so the
        learning window can re-render after a deletion."""
        content = FlippedView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH, content_plan["height"])
        )
        apply_window_background(content, dark=dark_now)

        def make_label(text, y, h, font, color, x=MARGIN):
            label = NSTextField.labelWithString_(text)
            label.setFrame_(NSMakeRect(x, y, WIDTH - MARGIN - x, h))
            label.setFont_(font)
            label.setTextColor_(_label_color(color))
            label.setEditable_(False)
            label.setSelectable_(True)
            label.setBezeled_(False)
            label.setDrawsBackground_(False)
            content.addSubview_(label)
            return label

        if content_plan["brand_y"] is not None:
            # Brand row: the custom b mark and the lowercase bolo lockup,
            # drawn from bolo_brand so every Bolo surface shows the same
            # silhouette and warm palette.
            mark_view = BrandMarkView.alloc().initWithFrame_(
                NSMakeRect(MARGIN, content_plan["brand_y"], BRAND_MARK_SIZE, BRAND_MARK_SIZE)
            )
            mark_view.configureWithDark_(dark_now)
            content.addSubview_(mark_view)
            wordmark = make_label(
                (content_display.get("brand") or "bolo").lower(),
                content_plan["brand_y"],
                BRAND_ROW_H,
                heading_font(BRAND_LOCKUP_SIZE, bold=True),
                text_color,
                x=MARGIN + BRAND_MARK_SIZE + BRAND_LOCKUP_PAD,
            )
            wordmark.setSelectable_(False)

        if content_plan["welcome_y"] is not None:
            make_label(
                "\n".join(content_plan["welcome_lines"]),
                content_plan["welcome_y"],
                len(content_plan["welcome_lines"]) * LABEL_LINE_H,
                heading_font(WELCOME_HEADLINE_SIZE, bold=True),
                text_color,
            )

        try_it_refs = None
        key_refs = None
        accessibility_refs = None
        practice_refs = None
        key_index = content_plan["key_index"]
        for index, (row, row_plan) in enumerate(
            zip(content_display.get("rows", []), content_plan["rows"])
        ):
            is_hero_row = (
                try_it_index is not None
                and index == try_it_index
                and hero_line is not None
                and row.get("state") == "pending"
            )
            if is_learning and index < len(learning_pairs):
                # Tappable pair row: a borderless button spanning the row
                # width, left-aligned like the plain labels. Tapping it
                # removes that learned pair and re-renders the window.
                pair_button = NSButton.buttonWithTitle_target_action_(
                    row.get("label", ""), controller, "deleteLearned:"
                )
                pair_button.setBordered_(False)
                pair_button.setTag_(index)
                pair_button.setFont_(label_font)
                pair_button.setFrame_(
                    NSMakeRect(
                        MARGIN,
                        row_plan["label_y"] - 3,
                        WIDTH - 2 * MARGIN,
                        LABEL_LINE_H + 6,
                    )
                )
                pair_title = NSMutableParagraphStyle.alloc().init()
                pair_title.setAlignment_(NSTextAlignmentLeft)
                # NSAttributedString attribute keys are stable string
                # constants ("NSFont", "NSColor", "NSParagraphStyle"), used
                # as literals here.
                pair_button.setAttributedTitle_(
                    NSAttributedString.alloc().initWithString_attributes_(
                        row.get("label", ""),
                        {
                            "NSFont": label_font,
                            "NSColor": text_color,
                            "NSParagraphStyle": pair_title,
                        },
                    )
                )
                content.addSubview_(pair_button)
                continue
            dot = NSView.alloc().initWithFrame_(
                NSMakeRect(MARGIN, row_plan["label_y"] + 5, 10, 10)
            )
            dot.setWantsLayer_(True)
            dot.layer().setCornerRadius_(5)
            dot.layer().setBackgroundColor_(
                colors.get(row.get("state"), colors["pending"]).CGColor()
            )
            content.addSubview_(dot)
            make_label(
                row.get("label", ""),
                row_plan["label_y"],
                LABEL_LINE_H,
                hero_label_font if is_hero_row else label_font,
                text_color,
                x=TEXT_X,
            )
            if key_index is not None and index == key_index:
                # Key-entry row: secure text field plus Validate button between
                # the label and the detail line. The validation GET runs inline
                # (bounded by KEY_VALIDATION_TIMEOUT_S), which briefly pauses
                # the run loop on click; acceptable for a one-shot action.
                field = NSSecureTextField.alloc().initWithFrame_(
                    NSMakeRect(TEXT_X, row_plan["field_y"], KEY_FIELD_W, KEY_FIELD_H)
                )
                field.cell().setPlaceholderString_(
                    (payload.get("key_entry") or {}).get("placeholder")
                    or "Paste your AssemblyAI API key"
                )
                content.addSubview_(field)
                validate_button = NSButton.buttonWithTitle_target_action_(
                    "Validate", controller, "validateKey:"
                )
                validate_button.setBezelStyle_(NSBezelStyleRounded)
                validate_button.setFrame_(
                    NSMakeRect(
                        TEXT_X + KEY_FIELD_W + 10,
                        row_plan["field_y"],
                        VALIDATE_BUTTON_W,
                        KEY_FIELD_H,
                    )
                )
                content.addSubview_(validate_button)
            practice_field = None
            if practice_index is not None and index == practice_index:
                # Editable practice field: the user proves dictation works
                # without leaving setup, because the runtime inserts into
                # whatever has keyboard focus, this field included.
                practice_field = NSTextField.alloc().initWithFrame_(
                    NSMakeRect(
                        TEXT_X,
                        row_plan["detail_y"] + len(row_plan["detail_lines"]) * DETAIL_LINE_H + 8,
                        PRACTICE_FIELD_W,
                        PRACTICE_FIELD_H,
                    )
                )
                practice_field.cell().setPlaceholderString_(
                    practice_spec.get("placeholder") or SCREEN_PRACTICE_PLACEHOLDER
                )
                practice_field.setFont_(detail_font)
                content.addSubview_(practice_field)
            detail_label = make_label(
                "\n".join(row_plan["detail_lines"]),
                row_plan["detail_y"],
                len(row_plan["detail_lines"]) * DETAIL_LINE_H,
                detail_font,
                text_color if is_hero_row else muted_color,
                x=TEXT_X,
            )
            action_kind = row_action_kind(row)
            if action_kind is not None:
                # Row action button (the Accessibility warn row's deep
                # link, or the granted row's restart): same target-action
                # mechanism the Validate button and the learning window's
                # tappable rows already use.
                action_button = NSButton.buttonWithTitle_target_action_(
                    action_button_title(row),
                    controller,
                    "openAccessibility:" if action_kind == "open_settings" else "restartBolo:",
                )
                action_button.setBezelStyle_(NSBezelStyleRounded)
                action_button.setFrame_(
                    NSMakeRect(
                        TEXT_X,
                        row_plan["action_y"],
                        ACTION_BUTTON_W,
                        ACTION_BUTTON_H,
                    )
                )
                content.addSubview_(action_button)
                if (
                    not is_learning
                    and action_kind == "open_settings"
                    and row.get("label") == ACCESSIBILITY_LABEL
                ):
                    accessibility_refs = {
                        "row": row,
                        "dot": dot,
                        "detail_label": detail_label,
                        "button": action_button,
                    }
            if try_it_index is not None and index == try_it_index:
                try_it_refs = {"dot": dot, "detail_label": detail_label}
            if key_index is not None and index == key_index:
                key_refs = {"dot": dot, "field": field, "detail_label": detail_label}
            if practice_index is not None and index == practice_index:
                practice_refs = {
                    "dot": dot,
                    "detail_label": detail_label,
                    "field": practice_field,
                    "row": row,
                }

        # Primary affordance: the warm clay brand background with ivory
        # text, so the button reads as enabled against both light and dark
        # windows instead of the washed-out default bezel. Return triggers
        # it too. A screen waiting on a real event (practice before the
        # first insert) renders disabled so it cannot fake readiness.
        primary_title = payload.get("button") or "Close"
        if is_screen:
            primary_title = screen_primary_button(
                screen, practice_done=payload.get("practice_done") is True
            )
        primary_action = "finish:"
        if is_screen and screen != SCREEN_READY:
            primary_action = "advance:"
        button = NSButton.buttonWithTitle_target_action_(
            primary_title, controller, primary_action
        )
        button.setBordered_(False)
        button.setWantsLayer_(True)
        button.layer().setBackgroundColor_(accent.CGColor())
        button.layer().setCornerRadius_(13.0)
        button.setKeyEquivalent_("\r")
        blocked = is_screen and screen == SCREEN_PRACTICE and payload.get("practice_done") is not True
        button.setEnabled_(not blocked)
        if blocked:
            button.layer().setBackgroundColor_(blocked_color.CGColor())
        paragraph = NSMutableParagraphStyle.alloc().init()
        paragraph.setAlignment_(NSTextAlignmentCenter)
        title_attributes = {
            "NSFont": NSFont.systemFontOfSize_weight_(13.0, NSFontWeightMedium),
            "NSColor": bolo_brand.native_color(bolo_brand.palette(dark=False)["button_text"]),
            "NSParagraphStyle": paragraph,
        }
        button.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                primary_title, title_attributes
            )
        )
        button.setFrame_(
            NSMakeRect(
                WIDTH - MARGIN - DONE_BUTTON_W,
                content_plan["button_y"],
                DONE_BUTTON_W,
                DONE_BUTTON_H,
            )
        )
        content.addSubview_(button)
        return content, {
            "try_it_refs": try_it_refs,
            "key_refs": key_refs,
            "accessibility_refs": accessibility_refs,
            "practice_refs": practice_refs,
            "primary_button": button,
        }

    content, row_refs = build_content(display, plan)
    window.setContentView_(content)
    STATE["key_refs"] = row_refs["key_refs"]

    # Wizard navigation: the primary button on every non-ready screen
    # advances; the ready screen's button finishes. Both paths go through
    # the same target-action, so Return drives either.
    if is_screen:
        STATE["screen"] = screen
        STATE["wizard_facts"] = runtime_facts(raw_wizard_payload(payload))

    # Accessibility trust polling: runs while the onboarding window shows
    # the untrusted Accessibility warn row, so the row flips green within
    # one poll interval of the user enabling Bolo.
    accessibility = None
    accessibility_refs = row_refs["accessibility_refs"]
    if accessibility_refs is not None and accessibility_refs["row"].get("state") == "warn":
        bundle = is_bundle_mode()

        def apply_accessibility_granted(refs=accessibility_refs, bundle=bundle):
            """Flip the row green in place and swap the button: Restart
            Bolo in bundle mode, no button from source (the granted
            detail's ./restart.sh line carries the instruction)."""
            new_row = accessibility_flip(refs["row"], True, bundle)
            if new_row is None:
                return
            print("[app-window] accessibility granted", file=sys.stderr, flush=True)
            refs["dot"].layer().setBackgroundColor_(colors["ok"].CGColor())
            refs["detail_label"].setStringValue_("\n".join(wrap_lines(new_row["detail"])))
            if row_action_kind(new_row) == "restart":
                # Keep the button's target; only the action moves.
                refs["button"].setTitle_(RESTART_TITLE)
                refs["button"].setAction_("restartBolo:")
            else:
                refs["button"].removeFromSuperview()

        accessibility = {
            "active": True,
            "next_check": time.monotonic() + TRUST_POLL_INTERVAL_S,
            "apply": apply_accessibility_granted,
        }

    if is_learning:
        # The delete action re-renders in place: same window, fresh content,
        # top-left corner pinned so the height change is not jumpy.
        def learning_rerender():
            learning_refs = STATE.get("learning") or {}
            rerender_display = learning_display_payload(
                learning_refs.get("pairs") or [],
                learning_refs.get("error"),
                learning_spec,
            )
            rerender_plan = plan_layout(rerender_display)
            rerender_content, _ = build_content(rerender_display, rerender_plan)
            frame = window.frame()
            rerender_frame = NSMakeRect(
                frame.origin.x,
                frame.origin.y + frame.size.height - rerender_plan["height"],
                WIDTH,
                rerender_plan["height"],
            )
            window.setContentView_(rerender_content)
            window.setFrame_display_(rerender_frame, True)

        STATE["learning"] = {
            "pairs": learning_pairs,
            "error": learning_error,
            "file": learning_spec.get("file") or LEARNED_FILE,
            "rerender": learning_rerender,
        }

    try_it_refs = row_refs["try_it_refs"]

    window.setDelegate_(controller)
    window.center()
    if preview:
        # Offscreen rendering path: no activation, no ordering front, so
        # cacheDisplay draws the genuine view hierarchy without stealing
        # focus or showing anything on the user's desktop.
        return {
            "window": window,
            "app": app,
            "try_it_refs": try_it_refs,
            "accessibility": accessibility,
            "run_loop": (NSRunLoop, NSDate, NSDefaultRunLoopMode),
            "primary_button": row_refs.get("primary_button"),
            "primary_color": accent,
            "accent": accent,
            "content": content,
            "plan": plan,
            # AppKit holds the window delegate and button targets weakly,
            # so the UI dict retains the controller. Without this, GC
            # can collect it after build and every control goes inert
            # before the pump ever dispatches a click.
            "controller": controller,
        }
    app.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)

    return {
        "window": window,
        "app": app,
        "try_it_refs": try_it_refs,
        "accessibility": accessibility,
        "run_loop": (NSRunLoop, NSDate, NSDefaultRunLoopMode),
        "primary_button": row_refs.get("primary_button"),
        "primary_color": accent,
        "accent": accent,
        "content": content,
        "plan": plan,
        "controller": controller,
    }


def apply_try_it_complete(ui, update):
    """Turn the try-it row green and show the capture line.

    A wizard practice update also unblocks the primary button, because
    the runtime only sends it after a real insert succeeded. An update
    that explicitly reports the runtime as untrusted at insert time is
    ignored: it must not fake readiness while pasting still fails.
    """
    if not practice_complete_from_update(update):
        return
    practice_refs = ui.get("refs", {}).get("practice_refs") if "refs" in ui else ui.get("practice_refs")
    if practice_refs is None:
        practice_refs = ui.get("practice_refs")
    if practice_refs is None:
        return
    from AppKit import NSColor  # noqa: F401

    practice_refs["dot"].layer().setBackgroundColor_(
        bolo_brand.native_color(
            bolo_brand.palette(dark=False)["success"]
        ).CGColor()
    )
    detail = update.get("detail") or PRACTICE_STATE_LABEL
    if detail:
        practice_refs["detail_label"].setStringValue_("\n".join(wrap_lines(detail)))
    button = None
    if isinstance(ui.get("refs"), dict):
        button = ui["refs"].get("primary_button")
    if button is None:
        button = ui.get("primary_button")
    primary_color = ui.get("primary_color")
    if button is not None and primary_color is not None:
        button.setEnabled_(True)
        button.layer().setBackgroundColor_(primary_color.CGColor())
        button.setTitle_(WIZARD_CONTINUE_TITLE)
    STATE["practice_done"] = True


def run_event_loop(ui):
    """Let AppKit run its native loop; poll parent updates on a timer.

    Activation happens on the first timer tick, after run() has launched
    the application. Only a user dismissal returns True; parent EOF
    returns False and cannot complete onboarding.
    https://developer.apple.com/documentation/appkit/nsapplication/run()
    https://developer.apple.com/documentation/foundation/timer
    """
    from AppKit import NSObject, NSView
    from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

    _appkit_classes_cached(NSView, NSObject)
    app = ui["app"]
    pump = globals()["_APPKIT_PUMP_CLASS"].alloc().init()
    pump._ui = ui
    pump._app = app
    pump._accessibility = ui.get("accessibility")
    pump._eof = False
    pump._activated = False
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        0.05, pump, "tick:", None, True
    )
    ui["pump"] = pump
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    try:
        app.run()
    finally:
        timer.invalidate()
        ui.pop("pump", None)
    if pump._eof:
        STATE["finish_button"] = False
        return False
    return STATE["user_done"] is True


def apply_dashboard_message(ui, update, raw_line):
    """Dispatch one stdin line to the dashboard UI, only on that UI.

    dashboard_update refreshes facts and history; dashboard_activate
    brings the window front on menu reopen; any other object is treated
    as a dashboard_action_reply. Returns True when something applied.
    """
    if not ui.get("dashboard") or not isinstance(update, dict):
        return False
    import dashboard_window

    refs = ui.get("refs") or {}
    kind = update.get("type")
    if kind == "dashboard_update":
        return dashboard_window.apply_dashboard_update(
            refs, update.get("dashboard")
        )
    if kind == "dashboard_activate":
        return dashboard_window.apply_dashboard_activate(refs, live=True)
    if kind == "dashboard_update" and not isinstance(update.get("dashboard"), dict):
        # A dashboard_update without a payload must not blank the window.
        return False
    return dashboard_window.apply_dashboard_action_reply(refs, update)


def apply_runtime_trust_reply(ui, update, trusted=None, restart_needed=None):
    """Apply one trust reply to the Accessibility step.

    The reply carries `trusted: bool` from the runtime's own reading of
    the helper that pastes. An explicit False keeps the warn state and
    the settings deep link: a Settings toggle that is on while the runtime
    is still untrusted (the failure users see today) cannot fake
    readiness. The wizard turns the primary straight into Continue when
    no restart is needed, so a live grant never traps the user in a
    restart loop.
    """
    if not isinstance(update, dict) or update.get("type") != "trust_reply":
        return
    apply_trust = (
        ui.get("apply_trust")
        or (STATE.get("wizard") or {}).get("apply_trust")
    )
    if apply_trust is None:
        return
    if trusted is None:
        trusted = bool(update.get("trusted"))
    if restart_needed is None:
        restart_needed = bool(update.get("restart_needed"))
    apply_trust(trusted, restart_needed)


def main(marker_file=None, marker_writer=None):
    """Entry point: run the window, then apply the marker decision.

    Only an explicit finish (the primary button on the ready screen,
    never the close box, never a skip, never stdin dying) after a real
    inserted dictation, on a payload whose runtime still wants the
    marker, writes ~/.bolo/onboarding.json. Everything else leaves the
    marker absent so the next launch reopens setup instead of pretending
    Bolo is configured. `marker_file`/`marker_writer` are injectable so
    tests prove the wiring without touching the real marker.
    """
    if marker_file is None:
        marker_file = MARKER_FILE
    payload, failure = read_payload()
    if failure is not None:
        print("[app-window] {0}".format(failure), file=sys.stderr)
        return 1
    reset_state()
    try:
        ui = build_ui(payload)
    except ImportError as error:
        print("[app-window] AppKit unavailable: {0}".format(error), file=sys.stderr)
        return 1
    user_closed = run_event_loop(ui)
    ui["window"].orderOut_(None)
    STATE["wizard"] = None
    finish = payload.copy()
    finish["write_marker"] = payload.get("write_marker") is True
    complete = should_write_marker(
        finish,
        screen=STATE.get("screen"),
        practice_done=STATE.get("practice_done") is True,
        user_closed=user_closed,
    )
    # The ready screen's primary button sets finish_button; the close box
    # clears it. Reaching ready is not enough: only the explicit finish
    # click after a genuine insert marks onboarding done.
    if complete and STATE.get("finish_button") is not True:
        complete = False
    if not complete:
        return 0
    written = marker_payload()
    if marker_writer is not None:
        marker_writer(marker_file, written)
    else:
        write_marker(marker_file, written)
    return 0


STATE = {
    "user_done": False,
    "finish_button": False,
    "advanced": False,
    "practice_done": False,
    "screen": None,
}


def reset_state():
    """Fresh per-session wizard state (tests reset between windows)."""
    STATE.update(
        {
            "user_done": False,
            "finish_button": False,
            "advanced": False,
            "practice_done": False,
            "screen": None,
        }
    )

if __name__ == "__main__":
    # The dashboard imports app_window for the shared AppKit classes.
    # Keep one module identity when the runtime launches this as a script.
    sys.modules["app_window"] = sys.modules[__name__]
    sys.exit(main())
