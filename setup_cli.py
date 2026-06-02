"""
First-run setup wizard for Bolo.

Walks through:
1. Permissions check (Accessibility, Microphone)
2. API key configuration
3. Audio device selection
4. Hotkey verification
5. Creating ~/.bolo/ directory structure
6. Launch on startup
"""

import json
import os
import subprocess
import sys


BOLO_DIR = os.path.expanduser("~/.bolo")
ENV_FILE = os.path.join(BOLO_DIR, "env")
PREFS_FILE = os.path.join(BOLO_DIR, "prefs.json")


def main():
    print("")
    print("  ╔══════════════════════════════════╗")
    print("  ║       Bolo Setup Wizard           ║")
    print("  ║   Voice dictation for macOS       ║")
    print("  ╚══════════════════════════════════╝")
    print("")

    # 1. Permissions
    print("── Step 1: Permissions ──")
    print("")
    print("  Bolo needs two permissions:")
    print("")
    print("  ✓ Accessibility (to type text into apps)")
    print("  ✓ Microphone (to hear you)")
    print("")
    print("  These will be requested on first launch.")
    print("  If they fail, enable them in System Settings > Privacy & Security.")
    print("")

    # 2. API Key
    print("── Step 2: Telnyx API Key ──")
    print("")
    existing = _find_existing_key()
    if existing:
        masked = existing[:8] + "..." + existing[-4:] if len(existing) > 12 else existing
        print(f"  Found existing key: {masked}")
        use = input("  Use this key? [Y/n] ").strip().lower()
        if use in ("n", "no"):
            key = _prompt_key()
        else:
            key = existing
    else:
        print("  You need a Telnyx API key to use Bolo.")
        print("  Get one at: https://portal.telnyx.com")
        print("")
        key = _prompt_key()

    # 3. Save env
    os.makedirs(BOLO_DIR, exist_ok=True)
    _write_env(key)
    print("")
    print(f"  ✓ API key saved to {ENV_FILE}")
    print("")

    # 4. Audio device
    print("── Step 3: Microphone ──")
    print("")
    try:
        from audio_devices import list_microphones, get_preferred_device, set_preferred_device
        devices = list_microphones()
        if devices:
            current = get_preferred_device()
            print("  Available microphones:")
            for i, d in enumerate(devices):
                is_current = current and d["index"] == current["index"]
                print(f"  [{i}] {d['name']} {'← current' if is_current else ''}")
            print("")
            pick = input(f"  Pick a microphone [0-{len(devices)-1}, Enter for default]: ").strip()
            if pick.isdigit() and 0 <= int(pick) < len(devices):
                set_preferred_device(devices[int(pick)]["index"])
                print(f"  ✓ Selected: {devices[int(pick)]['name']}")
            else:
                print("  ✓ Using system default microphone")
        else:
            print("  ⚠ No microphones detected. Check your audio hardware.")
    except ImportError:
        print("  ⚠ Could not list audio devices (sounddevice not installed?)")
        print("    Run: pip install sounddevice")
    print("")

    # 5. Default preferences
    print("── Step 4: Defaults ──")
    print("")
    _write_default_prefs()
    print("  ✓ Preferences saved")
    print("")

    # 6. Launch on startup
    print("── Step 5: Launch on Startup ──")
    print("")
    want_login = input("  Start Bolo automatically when you log in? [Y/n] ").strip().lower()
    if want_login not in ("n", "no"):
        _add_login_item()
        print("  ✓ Added to Login Items")
    else:
        print("  Skipped. Run 'setup_cli.py --login' later to add.")
    print("")

    # 7. Build Rust binary
    print("── Step 6: Build ──")
    print("")
    if os.path.exists("Cargo.toml"):
        print("  Building Rust runtime...")
        try:
            subprocess.run(["cargo", "build", "--release"], check=True, timeout=120)
            print("  ✓ Built target/release/bolo")
        except Exception as e:
            print(f"  ⚠ Build failed: {e}")
            print("    You can still use: python3 bolo.py")
    else:
        print("  No Cargo.toml found. Assuming Python-only mode.")
    print("")

    # Done
    print("── Setup Complete ──")
    print("")
    print("  Start Bolo:")
    print("    python3 bolo.py")
    print("")
    print("  Logs: tail -f /tmp/bolo.log")
    print("  History: python3 session_store.py recent")
    print("  Settings: Open from menubar > Bolo > Settings")
    print("")


def _find_existing_key():
    """Search for existing TELNYX_API_KEY in common locations."""
    for path in [ENV_FILE, os.path.expanduser("~/.codex/.env")]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    for line in f:
                        if line.strip().startswith("TELNYX_API_KEY="):
                            return line.split("=", 1)[1].strip().strip("\"'")
            except OSError:
                pass
    return None


def _prompt_key():
    while True:
        key = input("  Paste your TELNYX_API_KEY: ").strip()
        if key:
            return key
        print("  Key cannot be empty.")


def _write_env(key):
    existing_lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                if not line.strip().startswith("TELNYX_API_KEY="):
                    existing_lines.append(line.rstrip("\n"))
    existing_lines.append(f'TELNYX_API_KEY="{key}"')
    with open(ENV_FILE, "w") as f:
        f.write("\n".join(existing_lines) + "\n")


def _write_default_prefs():
    prefs = {}
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE) as f:
                prefs = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    prefs.setdefault("auto_silence_enabled", True)
    prefs.setdefault("clipboard_mode_enabled", False)
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def _add_login_item():
    """Add Bolo to macOS Login Items via osascript."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cmd = os.path.join(script_dir, "start-bolo.command")
    if not os.path.exists(cmd):
        return
    applescript = (
        'tell application "System Events" to '
        'delete (every login item whose name contains "bolo")\n'
        'tell application "System Events" to '
        f'make new login item at end of login items with properties {{path:"{cmd}", hidden:false}}'
    )
    subprocess.run(["osascript", "-e", applescript], capture_output=True)


if __name__ == "__main__":
    if "--login" in sys.argv:
        _add_login_item()
        print("✓ Added to Login Items")
    else:
        main()
