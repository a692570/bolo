#!/usr/bin/env python3
"""Report whether macOS has granted Bolo Accessibility trust.

Prints ``true`` on stdout when the calling process is trusted, ``false``
otherwise. With ``--prompt`` the first untrusted call also opens the macOS
System Settings prompt so the user knows where to grant the permission.

Used by the Rust runtime to detect the silent-paste-failure state caused by
missing or stale Accessibility permission.
"""

import sys

import ApplicationServices as AX


def main() -> int:
    prompt = "--prompt" in sys.argv
    if prompt:
        trusted = bool(
            AX.AXIsProcessTrustedWithOptions(
                {AX.kAXTrustedCheckOptionPrompt: True}
            )
        )
    else:
        trusted = bool(AX.AXIsProcessTrusted())
    print("true" if trusted else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
