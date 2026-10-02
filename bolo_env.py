#!/usr/bin/env python3
"""Shared writer for Bolo's environment file (~/.bolo/env).

Both install.sh (bash) and the onboarding window (app_window.py) need to
persist a shell variable with the same rules: replace any existing line for
the name or append a new one, double-quote the value with backslash and
double-quote escaping, keep the file private (0600, 0700 parent directory),
and swap the result in atomically. This module is the single Python
implementation of that contract so the onboarding key-entry path cannot
drift from the installer.
"""

import os
import tempfile


def shell_double_quote(value):
    """Escape a value for placement inside double quotes.

    Mirrors install.sh's ``${key//\\/\\\\}`` + ``${key//\"/\\\"}`` and the
    Rust ``shell_double_quote``: backslashes first, then double quotes.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def read_env_value(env_path, name):
    """Return the unquoted value for ``name`` from ``env_path``, or None.

    Accepts both the ``NAME=value`` (hotkey picker) and ``NAME="value"``
    (installer, this module) line formats.
    """
    prefix = name + "="
    try:
        with open(env_path, "r") as handle:
            for line in handle:
                if line.startswith(prefix):
                    raw = line[len(prefix):].strip()
                    return raw.strip('"')
    except FileNotFoundError:
        return None
    return None


def write_env_value(env_path, name, value):
    """Replace-or-append ``name`` with a double-quoted ``value``.

    Writes a sibling temp file (0600) and atomically renames it over
    ``env_path``, exactly like install.sh's mktemp + chmod + mv flow.
    """
    parent = os.path.dirname(env_path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    existing = []
    if os.path.exists(env_path):
        with open(env_path, "r") as handle:
            existing = handle.readlines()
    prefix = name + "="
    replacement = '{0}="{1}"\n'.format(name, shell_double_quote(value))
    lines = []
    written = False
    for line in existing:
        if line.startswith(prefix):
            lines.append(replacement)
            written = True
        else:
            lines.append(line)
    if not written:
        lines.append(replacement)
    fd, tmp_path = tempfile.mkstemp(
        dir=parent, prefix=os.path.basename(env_path) + ".tmp."
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.writelines(lines)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, env_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def default_env_path():
    """The standard env file location under the user's home directory."""
    return os.path.expanduser("~/.bolo/env")
