"""
Preferences window for Bolo using native AppKit.

Opens from the menubar: Bolo > Settings...
Shows current config, lets user change key settings without touching files.
"""

import json
import os
import threading
import urllib.request

import AppKit
from AppKit import (
    NSApp,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSButton,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSMakeRect,
    NSPanel,
    NSSecureTextField,
    NSTextField,
    NSTextView,
    NSScrollView,
    NSBezelStyleRounded,
    NSButtonTypeSwitch,
    NSTitledWindowMask,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
)

NSColor = AppKit.NSColor
NSFont = AppKit.NSFont


PREFS_FILE = os.path.expanduser("~/.bolo/prefs.json")
CORRECTIONS_FILE = os.path.expanduser("~/.bolo/corrections.json")


def _load_prefs():
    try:
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_prefs(prefs):
    os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


class SettingsWindow:
    """Native macOS preferences panel."""

    def __init__(self, on_close_callback=None):
        self.on_close = on_close_callback
        self._window = None
        self._built = False

    def show(self):
        if not self._built:
            self._build()
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def _build(self):
        prefs = _load_prefs()

        rect = NSMakeRect(0, 0, 480, 420)
        win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            NSTitledWindowMask | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable,
            NSBackingStoreBuffered,
            False,
        )
        win.setTitle_("Bolo Settings")
        win.center()

        content = win.contentView()

        y = 370

        title = _make_label("Bolo Preferences", 20, y, 200, 24, bold=True)
        content.addSubview_(title)
        y -= 36

        auto = prefs.get("auto_silence_enabled", True)
        self._auto_toggle = NSButton.alloc().initWithFrame_(NSMakeRect(20, y, 440, 22))
        self._auto_toggle.setButtonType_(NSButtonTypeSwitch)
        self._auto_toggle.setTitle_("Auto-stop on silence")
        self._auto_toggle.setState_(NSControlStateValueOn if auto else NSControlStateValueOff)
        self._auto_toggle.setTarget_(self)
        self._auto_toggle.setAction_("toggleAutoSilence:")
        content.addSubview_(self._auto_toggle)
        y -= 28

        lbl1 = _make_label(
            "Stop recording automatically after 5 seconds of silence.", 38, y, 420, 14)
        lbl1.setTextColor_(NSColor.grayColor())
        content.addSubview_(lbl1)
        y -= 32

        clip = prefs.get("clipboard_mode_enabled", False)
        self._clip_toggle = NSButton.alloc().initWithFrame_(NSMakeRect(20, y, 440, 22))
        self._clip_toggle.setButtonType_(NSButtonTypeSwitch)
        self._clip_toggle.setTitle_("Clipboard paste mode")
        self._clip_toggle.setState_(NSControlStateValueOn if clip else NSControlStateValueOff)
        self._clip_toggle.setTarget_(self)
        self._clip_toggle.setAction_("toggleClipboard:")
        content.addSubview_(self._clip_toggle)
        y -= 28

        lbl2 = _make_label(
            "Use clipboard paste instead of keystroke simulation.", 38, y, 420, 14)
        lbl2.setTextColor_(NSColor.grayColor())
        content.addSubview_(lbl2)
        y -= 32

        lbl3 = _make_label("LLM Cleanup:", 20, y, 100, 22)
        content.addSubview_(lbl3)

        modes = ["auto", "on", "off"]
        current_mode = os.environ.get("BOLO_LLM_CLEANUP", "").strip().lower() or "auto"
        self._mode_buttons = []
        for i, mode in enumerate(modes):
            x = 130 + i * 110
            rb = NSButton.alloc().initWithFrame_(NSMakeRect(x, y - 2, 100, 22))
            rb.setButtonType_(NSButtonTypeSwitch)  # actually radio, but NSButtonTypeRadio requires group
            rb.setTitle_(mode.title())
            rb.setState_(1 if mode == current_mode else 0)
            rb.setTag_(i)
            rb.setTarget_(self)
            rb.setAction_("radioLLM:")
            self._mode_buttons.append(rb)
            content.addSubview_(rb)
        y -= 36

        btn_cor = NSButton.alloc().initWithFrame_(NSMakeRect(20, y, 200, 28))
        btn_cor.setTitle_("Manage Corrections...")
        btn_cor.setBezelStyle_(NSBezelStyleRounded)
        btn_cor.setTarget_(self)
        btn_cor.setAction_("showCorrections:")
        content.addSubview_(btn_cor)

        corr_count = _count_corrections()
        lbl_corr = _make_label(
            f"{corr_count} learned corrections", 228, y + 6, 230, 14)
        lbl_corr.setTextColor_(NSColor.grayColor())
        content.addSubview_(lbl_corr)
        lbl_corr.setTag_(999)
        self._corr_label = lbl_corr
        y -= 40

        lbl_key = _make_label("Telnyx API Key:", 20, y, 100, 22)
        content.addSubview_(lbl_key)
        y -= 24

        try:
            from config import get as cfg_get
            key = cfg_get("TELNYX_API_KEY")
        except ImportError:
            key = ""
        self._key_field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(20, y, 440, 24))
        self._key_field.setStringValue_(key[:8] + "..." + key[-4:] if len(key) > 12 else key)
        self._key_field.setEditable_(False)
        self._key_field.setToolTip_(
            "API key is loaded from ~/.bolo/env. Edit that file directly to change it.")
        content.addSubview_(self._key_field)
        y -= 20

        lbl_key_help = _make_label(
            "Edit ~/.bolo/env to change. Restart after editing.", 20, y, 440, 14)
        lbl_key_help.setTextColor_(NSColor.grayColor())
        content.addSubview_(lbl_key_help)
        y -= 24

        try:
            from config import __version__
        except ImportError:
            __version__ = "dev"
        lbl_ver = _make_label(f"Bolo {__version__}", 20, y, 200, 14)
        lbl_ver.setTextColor_(NSColor.grayColor())
        content.addSubview_(lbl_ver)

        btn_update = NSButton.alloc().initWithFrame_(NSMakeRect(200, y - 4, 160, 22))
        btn_update.setTitle_("Check for Updates...")
        btn_update.setBezelStyle_(10)
        btn_update.setTarget_(self)
        btn_update.setAction_("checkUpdates:")
        content.addSubview_(btn_update)

        self._window = win
        self._built = True

    def toggleAutoSilence_(self, sender):
        prefs = _load_prefs()
        prefs["auto_silence_enabled"] = (sender.state() == NSControlStateValueOn)
        _save_prefs(prefs)

    def toggleClipboard_(self, sender):
        prefs = _load_prefs()
        prefs["clipboard_mode_enabled"] = (sender.state() == NSControlStateValueOn)
        _save_prefs(prefs)

    def radioLLM_(self, sender):
        modes = ["auto", "on", "off"]
        mode = modes[sender.tag()]
        env_path = os.path.expanduser("~/.bolo/env")
        lines = []
        found = False
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.strip().startswith("BOLO_LLM_CLEANUP="):
                        lines.append(f'BOLO_LLM_CLEANUP="{mode}"\n')
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f'BOLO_LLM_CLEANUP="{mode}"\n')
        with open(env_path, "w") as f:
            f.writelines(lines)

    def showCorrections_(self, sender):
        _show_corrections_sheet(self._window)
        count = _count_corrections()
        self._corr_label.setStringValue_(f"{count} learned corrections")

    def checkUpdates_(self, sender):
        threading.Thread(target=_check_updates_thread, daemon=True).start()


def _make_label(text, x, y, w, h, bold=False, size=12):
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    font = NSFont.systemFontOfSize_(size)
    if bold:
        font = NSFont.boldSystemFontOfSize_(size)
    field.setFont_(font)
    return field


def _count_corrections() -> int:
    try:
        with open(CORRECTIONS_FILE) as f:
            return len(json.load(f))
    except (OSError, json.JSONDecodeError):
        return 0


def _show_corrections_sheet(parent_window):
    corr = {}
    try:
        with open(CORRECTIONS_FILE) as f:
            corr = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    if not corr:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("No Corrections")
        alert.setInformativeText_(
            "You haven't taught Bolo any corrections yet. "
            "Say 'actually <word>' after a dictation to add one.")
        alert.addButtonWithTitle_("OK")
        alert.beginSheetModalForWindow_completionHandler_(parent_window, None)
        return

    lines = [f"{k}  →  {v}" for k, v in corr.items()]
    text = "\n".join(lines)

    scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 250))
    scroll.setHasVerticalScroller_(True)

    tv = AppKit.NSTextView.alloc().initWithFrame_(scroll.contentView().frame())
    tv.setString_(text)
    tv.setEditable_(False)
    tv.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(11, 0))

    scroll.setDocumentView_(tv)

    panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 420, 320),
        NSTitledWindowMask | NSWindowStyleMaskClosable,
        NSBackingStoreBuffered,
        False,
    )
    panel.setTitle_("Learned Corrections")
    panel.center()
    panel.contentView().addSubview_(scroll)

    btn = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(140, 10, 140, 28))
    btn.setTitle_("Delete All")
    btn.setBezelStyle_(NSBezelStyleRounded)

    def delete_all(_):
        try:
            os.remove(CORRECTIONS_FILE)
        except OSError:
            pass
        panel.close()

    btn.setTarget_(panel)
    btn.setAction_(delete_all)
    panel.contentView().addSubview_(btn)

    panel.makeKeyAndOrderFront_(None)


def _check_updates_thread():
    try:
        url = "https://api.github.com/repos/a692570/bolo/releases/latest"
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            latest = data.get("tag_name", "").lstrip("v")
            from config import __version__
            current = __version__.lstrip("v")
            if latest and latest != current:
                _show_update_available(latest, data.get("html_url", ""))
            else:
                _show_no_update()
    except Exception as e:
        _show_update_error(str(e))


def _show_update_available(latest, url):
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Update Available")
    alert.setInformativeText_(
        f"Bolo {latest} is available (you have {__version__}).\nDownload: {url}")
    alert.addButtonWithTitle_("OK")
    alert.runModal()


def _show_no_update():
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Up to Date")
    alert.setInformativeText_(f"Bolo {__version__} is the latest version.")
    alert.addButtonWithTitle_("OK")
    alert.runModal()


def _show_update_error(error):
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Update Check Failed")
    alert.setInformativeText_(f"Could not check for updates: {error}")
    alert.addButtonWithTitle_("OK")
    alert.runModal()
