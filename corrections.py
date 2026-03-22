#!/usr/bin/env python3

import json
import os
import re
import tempfile

MAX_ENTRIES = 200


class CorrectionStore:
    def __init__(self, path: str):
        self.path = path
        self._cache = None  # loaded lazily

    def _get(self):
        if self._cache is None:
            self._cache = self.load()
        return self._cache

    def load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def save(self, wrong: str, right: str):
        wrong = (wrong or "").strip()
        right = (right or "").strip()
        if not wrong or not right:
            return
        # Only store if strings differ beyond case
        if wrong.lower() == right.lower():
            return
        # Skip very short strings that are too likely to cause false replacements
        if len(wrong.split()) < 2 and len(wrong) < 6:
            return
        key = wrong.lower()
        store = self._get()
        # If already stored with same mapping, nothing to do
        if store.get(key) == right:
            return
        store[key] = right
        # Evict oldest entries beyond limit
        if len(store) > MAX_ENTRIES:
            excess = len(store) - MAX_ENTRIES
            for old_key in list(store.keys())[:excess]:
                del store[old_key]
        self._write(store)

    def apply(self, text: str) -> str:
        if not text:
            return text
        store = self._get()
        if not store:
            return text
        # Sort by key length descending so longer phrases match first
        for key in sorted(store.keys(), key=len, reverse=True):
            replacement = store[key]
            # Case-insensitive exact word/phrase boundary match
            try:
                pattern = re.compile(r"(?<!\w)" + re.escape(key) + r"(?!\w)", re.IGNORECASE)
                text = pattern.sub(replacement, text)
            except re.error:
                continue
        return text

    def _write(self, store):
        dir_path = os.path.dirname(self.path) or "."
        try:
            fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass
