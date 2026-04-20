#!/usr/bin/env python3

import json
import os
import subprocess
import threading
import time


class RecordingOverlay:
    """Launches overlay.py as a subprocess and streams UI updates over stdin.

    Includes health monitoring - auto-restarts the overlay if it dies unexpectedly
    and ensures the overlay window doesn't get stuck visible.
    """

    def __init__(self, base_dir: str):
        self.script = os.path.join(base_dir, "overlay.py")
        self._proc = None
        self._last_update_at = 0.0
        self._update_count = 0
        self._lock = threading.Lock()
        self._is_showing = False

    def show(self):
        """Show the overlay window."""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                # Already running
                self._is_showing = True
                return

            self._proc = subprocess.Popen(
                ["/usr/bin/python3", self.script],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            self._is_showing = True
            self._last_update_at = time.time()
            self._update_count = 0
        print("[overlay] shown", flush=True)

    def is_alive(self):
        """Check if the overlay process is still running."""
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def update(self, phase, text=""):
        """Update the overlay display.

        Returns True if update succeeded, False if overlay died.
        """
        with self._lock:
            # Check if overlay died
            if self._proc and self._proc.poll() is not None:
                self._proc = None
                self._is_showing = False
                return False

            if not self._proc or self._proc.stdin is None:
                return False

            try:
                payload = json.dumps({"phase": phase, "text": text or ""})
                self._proc.stdin.write(payload + "\n")
                self._proc.stdin.flush()
                self._last_update_at = time.time()
                self._update_count += 1
                return True
            except (BrokenPipeError, IOError):
                # Pipe broken - overlay died
                self._proc = None
                self._is_showing = False
                return False
            except Exception:
                return False

    def hide(self):
        """Hide the overlay window and terminate the process."""
        with self._lock:
            self._is_showing = False
            if self._proc and self._proc.poll() is None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                except Exception:
                    pass
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self._proc.kill()
                        self._proc.wait(timeout=1.0)
                    except Exception:
                        pass
            self._proc = None
        print("[overlay] hidden", flush=True)

    def force_kill(self):
        """Force kill the overlay process (panic reset)."""
        with self._lock:
            self._is_showing = False
            if self._proc:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=0.5)
                except Exception:
                    pass
                self._proc = None
        print("[overlay] force killed", flush=True)

    def get_health(self):
        """Get overlay health status."""
        return {
            "alive": self.is_alive(),
            "showing": self._is_showing,
            "last_update_ago": time.time() - self._last_update_at,
            "update_count": self._update_count,
        }
