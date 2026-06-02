"""
Audio device enumeration and selection for Bolo.

Lists available microphones, lets the user pick one, saves choice to prefs.
Uses sounddevice (already a dependency) to query devices.
"""

import json
import os
import sounddevice as sd


PREFS_FILE = os.path.expanduser("~/.bolo/prefs.json")


def list_microphones() -> list:
    """Return list of (index, name, channels, default) for input devices."""
    devices = []
    try:
        default_input = sd.default.device[0]
    except Exception:
        default_input = None

    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            devices.append({
                "index": idx,
                "name": dev["name"],
                "channels": dev["max_input_channels"],
                "sample_rate": int(dev["default_samplerate"]),
                "is_default": idx == default_input,
            })
    return devices


def get_preferred_device() -> dict:
    """Get the user's preferred microphone, or system default."""
    prefs = _load_prefs()
    device_id = prefs.get("audio_device_id")

    if device_id is not None:
        try:
            dev = sd.query_devices(device_id)
            if dev["max_input_channels"] > 0:
                return {
                    "index": device_id,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "sample_rate": int(dev["default_samplerate"]),
                }
        except Exception:
            pass

    # Fall back to system default
    devices = list_microphones()
    for d in devices:
        if d["is_default"]:
            return d
    return devices[0] if devices else None


def set_preferred_device(device_id: int):
    """Save the user's microphone choice."""
    prefs = _load_prefs()
    prefs["audio_device_id"] = device_id
    _save_prefs(prefs)


def cli():
    """List microphones for the user to pick from."""
    import sys
    devices = list_microphones()
    if not devices:
        print("No microphones found.")
        sys.exit(1)

    current = get_preferred_device()

    print("Available microphones:\n")
    for d in devices:
        marker = " ← current" if (current and d["index"] == current["index"]) else ""
        default = " (system default)" if d["is_default"] else ""
        print(f"  [{d['index']}] {d['name']} — {d['channels']}ch, {d['sample_rate']}Hz{default}{marker}")

    print("\nRun: python3 audio_devices.py select <index>")
    print("  Or set in ~/.bolo/prefs.json: {\"audio_device_id\": <index>}")


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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "select":
        try:
            idx = int(sys.argv[2])
            set_preferred_device(idx)
            dev = sd.query_devices(idx)
            print(f"Selected: [{idx}] {dev['name']}")
        except (IndexError, ValueError):
            print("Usage: python3 audio_devices.py select <index>")
            sys.exit(1)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        cli()
