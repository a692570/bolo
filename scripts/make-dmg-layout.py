#!/usr/bin/env python3
"""Write the Finder layout (.DS_Store) for the Bolo DMG programmatically.

No Finder AppleScript and no UI automation: the .DS_Store is written with the
pure-Python ``ds_store`` package (dmgbuild's library), and the background
image is referenced through a binary alias created with ``mac_alias`` for a
file that must already exist on the target volume. This is the same approach
``dmgbuild`` uses; see its core.py:

    alias = Alias.for_file(path_in_image)
    icvp["backgroundType"] = 2
    icvp["backgroundImageAlias"] = alias.to_bytes()

Run under the build-time venv (build/icon-venv) with the pinned build-only
deps from scripts/dmg-requirements.txt:

    python3 scripts/make-dmg-layout.py --volume /Volumes/Bolo \\
        --background .background/background.png \\
        --bolo-position 150,232 --applications-position 462,232 \\
        --window 0,0,660,400
"""

import argparse
import os
import sys

from ds_store import DSStore
import mac_alias

WINDOW_WIDTH = 660
WINDOW_HEIGHT = 400
DEFAULT_WINDOW = (0, 0, WINDOW_WIDTH, WINDOW_HEIGHT)

ICVP = {
    # Mirror of the icvp plist dmgbuild writes for an icon-view volume.
    "viewOptionsVersion": 1,
    "backgroundType": 2,  # 2 = background image (1 = color, 0 = none)
    "backgroundColorRed": 1.0,
    "backgroundColorGreen": 1.0,
    "backgroundColorBlue": 1.0,
    "gridOffsetX": 0.0,
    "gridOffsetY": 0.0,
    "gridSpacing": 100.0,
    "arrangeBy": "none",  # keep pinned icon positions
    "showIconPreview": True,
    "showItemInfo": False,
    "labelOnBottom": True,
    "textSize": 12.0,
    "iconSize": 80.0,
    "scrollPositionX": 0.0,
    "scrollPositionY": 0.0,
}


def _parse_point(value):
    parts = [int(p) for p in value.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected X,Y, got {0}".format(value))
    return (parts[0], parts[1])


def _parse_rect(value):
    parts = [int(p) for p in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("expected X,Y,W,H, got {0}".format(value))
    return tuple(parts)


def window_bounds(rect):
    """Finder-style WindowBounds string for a {x,y},{w,h} rect."""
    # Built by concatenation: str.format would need every literal brace
    # doubled, which is exactly the kind of escaping this function exists
    # to hide.
    return (
        "{{" + str(rect[0]) + ", " + str(rect[1])
        + "}, {" + str(rect[2]) + ", " + str(rect[3]) + "}}"
    )


def bwsp(rect):
    """Window settings plist: no chrome, fixed bounds (dmgbuild layout)."""
    return {
        "ShowStatusBar": False,
        "WindowBounds": window_bounds(rect),
        "ContainerShowSidebar": False,
        "PreviewPaneVisibility": False,
        "SidebarWidth": 0,
        "ShowTabView": False,
        "ShowToolbar": False,
        "ShowPathbar": False,
        "ShowSidebar": False,
    }


def write_layout(volume, background, positions, rect=DEFAULT_WINDOW):
    """Write <volume>/.DS_Store and return its path.

    ``background`` is a POSIX path; it may be absolute on the target volume or
    relative to the volume root. ``positions`` maps volume-root item names
    (e.g. "Bolo.app", "Applications") to (x, y) icon slots.
    """
    background_abs = background
    if not os.path.isabs(background_abs):
        background_abs = os.path.join(volume, background_abs)
    if not os.path.isfile(background_abs):
        raise FileNotFoundError(
            "the background image must exist on the target volume first: "
            "{0}".format(background_abs)
        )

    alias = mac_alias.Alias.for_file(background_abs)
    icvp = dict(ICVP)
    icvp["backgroundImageAlias"] = alias.to_bytes()

    ds_path = os.path.join(volume, ".DS_Store")
    with DSStore.open(ds_path, "w+") as store:
        store["."]["vSrn"] = ("long", 1)
        store["."]["bwsp"] = bwsp(rect)
        store["."]["icvp"] = icvp
        store["."]["icvl"] = (b"type", b"icnv")
        for name, position in positions.items():
            store[name]["Iloc"] = position
    return ds_path


def read_layout(ds_path):
    """Read back a written .DS_Store: window rect, icvp fields, icon slots,
    and the background alias resolved to its target path."""
    result = {"positions": {}, "background": None}
    with DSStore.open(ds_path, "r") as store:
        result["window_bounds"] = store["."]["bwsp"]["WindowBounds"]
        icvp = store["."]["icvp"]
        result["icon_size"] = icvp["iconSize"]
        result["background_type"] = icvp["backgroundType"]
        result["arrange_by"] = icvp["arrangeBy"]
        result["view"] = store["."]["icvl"][1]
        for filename, code in _iter_entries(store):
            if code == b"Iloc":
                value = store[filename]["Iloc"]
                result["positions"][filename] = (value[0], value[1])
        if icvp["backgroundType"] == 2:
            alias = mac_alias.Alias.from_bytes(icvp["backgroundImageAlias"])
            posix = alias.target.posix_path
            if isinstance(posix, bytes):
                posix = posix.decode("utf-8")
            volume = alias.volume.posix_path
            if isinstance(volume, bytes):
                volume = volume.decode("utf-8")
            carbon = alias.target.carbon_path
            if isinstance(carbon, bytes):
                carbon = carbon.decode("utf-8")
            # mac_alias resolves an alias by several anchors, and the
            # stored volume posix path is the mount point the alias was
            # created on, which can be stale after the image is converted
            # and remounted. Finder resolves by volume name plus the
            # volume-relative path; do the same, and only trust a
            # candidate that exists as a file.
            volume_name = (
                alias.volume.name.decode("utf-8")
                if isinstance(alias.volume.name, bytes)
                else alias.volume.name
            )
            relative = None
            if ":" in carbon and carbon.split(":", 1)[0] == volume_name:
                # Volume-relative carbon path after the volume-name prefix.
                relative = carbon.split(":", 1)[1]
                relative = relative.rstrip("\x00").replace("\x00", "")
                relative = relative.replace(":", os.sep)
            candidates = []
            if relative:
                candidates.append(
                    os.path.normpath(os.path.join(ds_path, "..", relative))
                )
            if posix:
                candidates.append(os.path.normpath(posix))
            resolved = None
            for candidate in candidates:
                if os.path.isfile(candidate):
                    resolved = candidate
                    break
            result["background"] = resolved
            result["background_exists"] = (
                resolved is not None and os.path.isfile(resolved)
            )
            result["background_volume_name"] = volume_name
            result["background_relative"] = posix
    return result


def _iter_entries(store):
    # Iterating a DSStore yields filenames (a Partial is created lazily by
    # indexing); deduplicate, then collect each name's entries with find().
    for filename in _names(store):
        for entry in store.find(filename):
            yield entry.filename, entry.code


def _names(store):
    names = []
    for item in store:
        name = getattr(item, "filename", item)
        if isinstance(name, bytes):
            name = name.decode("utf-8")
        if name not in names:
            names.append(name)
    return names


def parse_window(rect):
    """Parse '{{x, y}, {w, h}}' back into (x, y, w, h)."""
    import re

    numbers = [int(n) for n in re.findall(r"\d+", rect)]
    if len(numbers) != 4:
        raise ValueError("not a Finder WindowBounds string: {0}".format(rect))
    return tuple(numbers)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--volume",
        required=True,
        help="target volume root (the mounted scratch volume)",
    )
    parser.add_argument(
        "--background",
        default=".background/background.png",
        help="background image path on the volume (default: %(default)s)",
    )
    parser.add_argument(
        "--bolo-position",
        type=_parse_point,
        default=(150, 232),
        help="Bolo.app icon slot X,Y (default: %(default)s)",
    )
    parser.add_argument(
        "--applications-position",
        type=_parse_point,
        default=(462, 232),
        help="Applications symlink icon slot X,Y (default: %(default)s)",
    )
    parser.add_argument(
        "--window",
        type=_parse_rect,
        default=DEFAULT_WINDOW,
        help="container window rect X,Y,W,H (default: %(default)s)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="re-open the .DS_Store and verify the exact layout",
    )
    parser.add_argument(
        "--verify-existing",
        action="store_true",
        help="do not write; read the volume's existing .DS_Store and verify "
        "the background alias resolves to a file inside that volume",
    )
    args = parser.parse_args(argv)

    if args.verify_existing:
        ds_path = os.path.join(args.volume, ".DS_Store")
        if not os.path.isfile(ds_path):
            print(
                "[make-dmg-layout] ERROR: no .DS_Store at {0}".format(ds_path),
                file=sys.stderr,
            )
            return 1
        state = read_layout(ds_path)
        expected = {
            "Bolo.app": tuple(args.bolo_position),
            "Applications": tuple(args.applications_position),
        }
        for name, position in expected.items():
            if state["positions"].get(name) != position:
                print(
                    "[make-dmg-layout] ERROR: {0} at {1}, expected {2}".format(
                        name, state["positions"].get(name), position
                    ),
                    file=sys.stderr,
                )
                return 1
        if state["background_type"] != 2 or not state["background_exists"]:
            print(
                "[make-dmg-layout] ERROR: the background alias does not "
                "resolve to an existing file in {0} (got: {1})".format(
                    args.volume, state["background"]
                ),
                file=sys.stderr,
            )
            return 1
        # The resolved file must live inside the given volume.
        resolved = state["background"] or ""
        volume_abs = os.path.realpath(args.volume)
        if not os.path.realpath(resolved).startswith(volume_abs + os.sep):
            print(
                "[make-dmg-layout] ERROR: background {0} is outside the "
                "volume {1}".format(resolved, volume_abs),
                file=sys.stderr,
            )
            return 1
        print(
            "[make-dmg-layout] existing layout verified: window {0}, "
            "background {1}".format(
                parse_window(state["window_bounds"]), resolved
            )
        )
        return 0

    positions = {
        "Bolo.app": tuple(args.bolo_position),
        "Applications": tuple(args.applications_position),
    }
    try:
        ds_path = write_layout(
            args.volume, args.background, positions, tuple(args.window)
        )
    except (FileNotFoundError, OSError) as error:
        print("[make-dmg-layout] ERROR: {0}".format(error), file=sys.stderr)
        return 1

    print("[make-dmg-layout] wrote {0}".format(ds_path))

    if args.verify:
        state = read_layout(ds_path)
        rect = parse_window(state["window_bounds"])
        if rect != tuple(args.window):
            print(
                "[make-dmg-layout] ERROR: window {0} != {1}".format(
                    rect, tuple(args.window)
                ),
                file=sys.stderr,
            )
            return 1
        for name, expected in positions.items():
            if state["positions"].get(name) != expected:
                print(
                    "[make-dmg-layout] ERROR: {0} at {1}, expected {2}".format(
                        name, state["positions"].get(name), expected
                    ),
                    file=sys.stderr,
                )
                return 1
        expected_bg = os.path.normpath(
            os.path.join(args.volume, args.background)
        ) if not os.path.isabs(args.background) else os.path.normpath(args.background)
        if state["background"] != expected_bg:
            print(
                "[make-dmg-layout] ERROR: background alias resolves to "
                "{0}, expected {1}".format(state["background"], expected_bg),
                file=sys.stderr,
            )
            return 1
        if not state["background_exists"]:
            print(
                "[make-dmg-layout] ERROR: the aliased background file does "
                "not exist at {0}".format(state["background"]),
                file=sys.stderr,
            )
            return 1
        print(
            "[make-dmg-layout] verified window {0}, icons {1}, background {2}".format(
                rect,
                ", ".join(
                    "{0} at {1}".format(n, state["positions"][n])
                    for n in ("Bolo.app", "Applications")
                ),
                state["background"],
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
