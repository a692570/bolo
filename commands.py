#!/usr/bin/env python3


def parse_command(transcript: str, correction_active: bool = False):
    stripped = (transcript or "").strip()
    lowered = stripped.lower()

    if lowered == "scratch that":
        return {"kind": "scratch", "display": ""}
    if lowered == "new paragraph":
        return {"kind": "insert", "text": "\n\n", "display": "\\n\\n"}
    if lowered == "new line":
        return {"kind": "insert", "text": "\n", "display": "\\n"}
    if lowered == "bullet":
        return {"kind": "insert", "text": "\n- ", "display": "- "}
    if lowered.startswith("bullet "):
        text = stripped[7:].strip()
        return {"kind": "insert", "text": f"\n- {text}", "display": f"- {text}"}
    if lowered.startswith("actually "):
        text = stripped[9:].strip()
        if text and correction_active:
            return {"kind": "replace", "text": text, "display": text}

    # Punctuation commands
    if lowered == "comma":
        return {"kind": "insert", "text": ", ", "display": ","}
    if lowered in ("period", "full stop"):
        return {"kind": "insert", "text": ". ", "display": "."}
    if lowered == "question mark":
        return {"kind": "insert", "text": "? ", "display": "?"}
    if lowered in ("exclamation mark", "exclamation point"):
        return {"kind": "insert", "text": "! ", "display": "!"}
    if lowered == "open quote":
        return {"kind": "insert", "text": "\u201c", "display": "\u201c"}
    if lowered == "close quote":
        return {"kind": "insert", "text": "\u201d", "display": "\u201d"}
    if lowered == "dash":
        return {"kind": "insert", "text": " -- ", "display": "--"}
    if lowered == "colon":
        return {"kind": "insert", "text": ": ", "display": ":"}
    if lowered == "semicolon":
        return {"kind": "insert", "text": "; ", "display": ";"}

    return None
