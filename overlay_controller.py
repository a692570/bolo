#!/usr/bin/env python3

import json
import os
import subprocess


class RecordingOverlay:
    """Launches overlay.py as a subprocess and streams UI updates over stdin."""

    def __init__(self, base_dir: str):
        self.script = os.path.join(base_dir, "overlay.py")
        self._proc = None

    def show(self):
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            ["/usr/bin/python3", self.script],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        print("[overlay] shown", flush=True)

    def update(self, phase, text=""):
        if not self._proc or self._proc.poll() is not None or self._proc.stdin is None:
            return
        try:
            payload = json.dumps({"phase": phase, "text": text or ""})
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    def hide(self):
        if self._proc and self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except Exception:
                pass
            self._proc.terminate()
            self._proc = None
        print("[overlay] hidden", flush=True)
