#!/usr/bin/env python3


class CorrectionStore:
    def __init__(self, path: str):
        self.path = path

    def load(self):
        return {}

    def save(self, wrong: str, right: str):
        return None

    def apply(self, text: str) -> str:
        return text
