"""
Bolo configuration loading with validation.

Loads from env vars, ~/.bolo/env, ~/.codex/.env, ~/.zshrc
Validates that required values are present, endpoints are reachable-looking URLs,
and raises clear errors with suggestions on what to fix.
"""

import os
import sys
from typing import Optional


# ── Version ───────────────────────────────────────────────────────────────────

__version__ = "1.3.0-dev"


# ── Config keys and their metadata ────────────────────────────────────────────

# Each key: (required, env_name, description, validate_url)
REQUIRED_KEYS = [
    ("TELNYX_API_KEY", True, "Telnyx API key for STT and LLM",
     lambda v: v.startswith("KEY") or len(v) > 20, "should start with 'KEY...' and be at least 20 characters"),
]

OPTIONAL_KEYS = [
    ("LITELLM_BASE", "LiteLLM proxy base URL",
     lambda v: not v or v.startswith("http"), "should be an HTTP URL"),
    ("LITELLM_KEY", "LiteLLM proxy API key",
     lambda v: not v or len(v) > 10, "should be at least 10 characters if set"),
    ("BOLO_LLM_CLEANUP", "LLM cleanup mode (auto, on, off)",
     lambda v: not v or v in ("auto", "on", "off"), "should be 'auto', 'on', or 'off'"),
]


def _parse_env_file(filepath: str) -> dict:
    """Parse a simple KEY=VALUE env file (supports # comments, quoted values)."""
    result = {}
    if not os.path.exists(filepath):
        return result
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw = line.split("=", 1)
                val = raw.strip().strip("\"'")
                if val:
                    result[key.strip()] = val
    except OSError:
        pass
    return result


class BoloConfig:
    """Load and validate Bolo configuration from all known sources."""

    def __init__(self):
        self.values: dict = {}
        self.warnings: list = []
        self.errors: list = []

        # Load from all sources
        self._sources = {}
        self._sources["env"] = dict(os.environ)
        self._sources[os.path.expanduser("~/.bolo/env")] = _parse_env_file(
            os.path.expanduser("~/.bolo/env"))
        self._sources[os.path.expanduser("~/.codex/.env")] = _parse_env_file(
            os.path.expanduser("~/.codex/.env"))

        # Merge with priority
        for key in ["TELNYX_API_KEY", "LITELLM_BASE", "LITELLM_KEY", "BOLO_LLM_CLEANUP"]:
            self.values[key] = self._resolve(key)

            self.values[key] = self._resolve(key)

        # Load from zshrc only for TELNYX_API_KEY as last resort
        if not self.values.get("TELNYX_API_KEY"):
            self.values["TELNYX_API_KEY"] = self._load_from_zshrc("TELNYX_API_KEY")

    def _resolve(self, name: str) -> str:
        """Resolve a value from env, ~/.bolo/env, or ~/.codex/.env in order."""
        # 1. os.environ
        val = os.environ.get(name, "")
        if val:
            return val

        # 2. ~/.bolo/env
        env_path = os.path.expanduser("~/.bolo/env")
        vals = self._sources.get(env_path, {})
        if name in vals and vals[name]:
            return vals[name]

        # 3. ~/.codex/.env
        codex_path = os.path.expanduser("~/.codex/.env")
        vals = self._sources.get(codex_path, {})
        if name in vals and vals[name]:
            return vals[name]

        return ""

    def _load_from_zshrc(self, name: str) -> str:
        shell_file = os.path.expanduser("~/.zshrc")
        if not os.path.exists(shell_file):
            return ""
        try:
            with open(shell_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith(f"export {name}="):
                        continue
                    return line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            return ""

    def _check_required(self, name: str, description: str, is_valid: callable, hint: str):
        value = self.values.get(name, "")
        if not value:
            self.errors.append(
                f"MISSING: {name} is not set.\n"
                f"  What it is: {description}\n"
                f"  How to fix: Add TELNYX_API_KEY=\\\"your-key\\\" to ~/.bolo/env\n"
                f"  Get one at: https://portal.telnyx.com"
            )
            return
        if is_valid and not is_valid(value):
            self.errors.append(
                f"INVALID: {name} looks wrong.\n"
                f"  Value starts with: {value[:12]}...\n"
                f"  Expected: {hint}\n"
                f"  Fix: Edit ~/.bolo/env"
            )

    def _check_optional(self, name: str, description: str, is_valid: callable, hint: str):
        value = self.values.get(name, "")
        if not value:
            return  # optional, missing is fine
        if is_valid and not is_valid(value):
            self.warnings.append(
                f"WARNING: {name} is set but may be invalid.\n"
                f"  Expected: {hint}\n"
                f"  Got: {value[:50]}{'...' if len(value) > 50 else ''}"
            )

    def validate(self) -> bool:
        """Validate all config. Returns True if ready to start, False with errors printed."""
        self.errors = []
        self.warnings = []

        for name, req, desc, validator, hint in REQUIRED_KEYS:
            self._check_required(name, desc, validator, hint)

        for name, desc, validator, hint in OPTIONAL_KEYS:
            self._check_optional(name, desc, validator, hint)

        # Check env file exists with permissions
        env_path = os.path.expanduser("~/.bolo/env")
        if not os.path.exists(env_path) and not os.environ.get("TELNYX_API_KEY"):
            self.errors.append(
                f"MISSING: Configuration file not found.\n"
                f"  Expected: {env_path}\n"
                f"  How to fix: Run 'python3 setup.py' for first-time setup"
            )

        if self.warnings:
            for w in self.warnings:
                print(w, file=sys.stderr)

        if self.errors:
            for e in self.errors:
                print("", file=sys.stderr)
                print(f"  ✗ {e}", file=sys.stderr)
            return False

        return True

    def get(self, name: str, default: str = "") -> str:
        return self.values.get(name, default)


# Module-level instance
config = BoloConfig()


def validate() -> bool:
    """Validate config and exit with code 1 if invalid. Call at startup."""
    if not config.validate():
        print("\n💥 Bolo cannot start until all errors above are fixed.", file=sys.stderr)
        sys.exit(1)
    return True


def get(name: str, default: str = "") -> str:
    return config.get(name, default)
