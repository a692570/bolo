"""Exercise modifier polling and event callbacks without starting the event loop."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_modifier_checks(hotkey, flags):
    tree = ast.parse((Path(__file__).resolve().parents[1] / "hotkey.py").read_text())
    names = {"KEYCODE_MAP", "TARGET_KEYCODE", "USE_FLAGS_CHANGED"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and (target.id.startswith("NX_") or target.id in names)
            for target in node.targets
        ):
            nodes.append(node)
    nodes.extend(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"is_hotkey_down", "flags_changed"}
    )
    states = []
    namespace = {
        "HOTKEY": hotkey,
        "state": False,
        "CGEventSourceFlagsState": lambda _source: flags,
        # Model a source that reports the general modifier down for its key code.
        "CGEventSourceKeyState": lambda _source, _key: bool(flags),
        "kCGEventSourceStateCombinedSessionState": 0,
        "set_state": states.append,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "hotkey.py", "exec"), namespace)
    return namespace, states


# Values checked against Apple's SDK IOKit/hidsystem/IOLLEvent.h.
@pytest.mark.parametrize("hotkey, own_mask, opposite_mask", [
    ("left_option", 0x20, 0x40),
    ("right_option", 0x40, 0x20),
    ("right_shift", 0x04, 0x02),
    ("right_control", 0x2000, 0x01),
])
def test_modifier_polling_distinguishes_keyboard_sides(hotkey, own_mask, opposite_mask):
    for flags, expected in [(0, False), (opposite_mask, False),
                            (own_mask, True), (own_mask | opposite_mask, True)]:
        namespace, _states = load_modifier_checks(hotkey, flags)
        assert namespace["is_hotkey_down"]() is expected


def test_other_modifier_event_does_not_release_held_left_option():
    namespace, states = load_modifier_checks("left_option", 0x20 | 0x40)
    event = SimpleNamespace(keyCode=lambda: 61, modifierFlags=lambda: 0x20 | 0x40)
    namespace["flags_changed"](event)
    assert states == [True]


def test_right_option_event_does_not_activate_left_option():
    namespace, states = load_modifier_checks("left_option", 0x40)
    event = SimpleNamespace(keyCode=lambda: 61, modifierFlags=lambda: 0x40)
    namespace["flags_changed"](event)
    assert states == [False]
