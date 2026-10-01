#!/usr/bin/env python3
"""Report whether macOS trusts the Python helper for Accessibility.

Prints ``true`` on stdout when the calling process is trusted, ``false``
otherwise. With ``--prompt`` the first untrusted call also opens the macOS
System Settings prompt so the user knows where to grant the permission.

Used by the Rust runtime to detect the silent-paste-failure state caused by
missing or stale Accessibility permission. ``is_trusted`` is also imported by
accessibility_daemon.py so the persistent daemon reports the same state
without paying interpreter startup per check.
"""

import sys

import ApplicationServices as AX


def is_trusted(prompt: bool = False) -> bool:
    """Return whether this process is trusted for Accessibility events.

    With ``prompt`` an untrusted call also asks macOS to open the System
    Settings Accessibility pane.
    """
    if prompt:
        return bool(
            AX.AXIsProcessTrustedWithOptions(
                {AX.kAXTrustedCheckOptionPrompt: True}
            )
        )
    return bool(AX.AXIsProcessTrusted())


def main() -> int:
    print("true" if is_trusted("--prompt" in sys.argv) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
