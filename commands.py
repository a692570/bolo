#!/usr/bin/env python3


def parse_command(transcript: str, correction_active: bool = False):
    stripped = (transcript or "").strip()
    lowered = stripped.lower()
    if lowered == "scratch that":
        return {"kind": "scratch", "display": ""}
    if lowered == "new paragraph":
        return {"kind": "insert", "text": "\n\n", "display": "\\n\\n"}
    if lowered == "bullet":
        return {"kind": "insert", "text": "\n• ", "display": "• "}
    if lowered.startswith("bullet "):
        text = stripped[7:].strip()
        return {"kind": "insert", "text": f"\n• {text}", "display": f"• {text}"}
    if lowered.startswith("actually "):
        text = stripped[9:].strip()
        if text and correction_active:
            return {"kind": "replace", "text": text, "display": text}
    return None
