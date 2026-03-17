#!/usr/bin/env python3

import json
import os


class VocabularyStore:
    def __init__(self, built_in_path: str, user_path: str):
        self.built_in_path = built_in_path
        self.user_path = user_path

    def _load_file(self, path: str):
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        terms = []
        for item in data:
            if isinstance(item, str):
                term = item.strip()
                if term:
                    terms.append(term)
        return terms

    def terms(self):
        merged = []
        seen = set()
        for term in self._load_file(self.built_in_path) + self._load_file(self.user_path):
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(term)
        return merged
