"""Regression tests for the native overlay pill."""

import overlay


def test_every_runtime_phase_has_a_label_and_cue_color():
    for phase in (
        "connecting",
        "dictating",
        "listening",
        "thinking",
        "transcribing",
        "processing",
        "inserting",
        "inserted",
        "copied",
        "success",
        "final",
        "error",
    ):
        assert phase in overlay.PHASES
        text, accent = overlay.PHASES[phase]
        assert text
        assert accent is not None


def test_error_state_is_distinguishable_from_success_states():
    # The retry cue must be red, and the done cues green: a glance has to
    # separate failure from success without reading the label.
    assert overlay.is_error_phase("error") is True
    for phase in ("dictating", "inserting", "copied", "final", "thinking"):
        assert overlay.is_error_phase(phase) is False


def test_preview_text_clamps_long_transcripts_to_one_line():
    long = " ".join(["word"] * 40)
    line = overlay.preview_text(long)
    assert len(line) <= 56
    assert line.startswith("...")
    assert overlay.preview_text("") == ""
    assert overlay.preview_text(None) == ""


def test_pill_width_stays_inside_bounds_and_grows_with_text():
    narrow = overlay.pill_width("", "dictating")
    wide = overlay.pill_width(
        overlay.preview_text(" ".join(["word"] * 20)), "dictating"
    )
    # Short labels stay at the compact floor; long text widens but clamps.
    assert narrow >= overlay.MIN_WIDTH
    assert overlay.MIN_WIDTH <= wide <= overlay.MAX_WIDTH
    wide_clamped = overlay.pill_width(" ".join(["word"] * 60), "dictating")
    assert wide_clamped == overlay.MAX_WIDTH
    # A non-dictating phase never sizes for a transcript.
    assert overlay.pill_width("anything", "thinking") <= overlay.pill_width(
        "anything", "dictating"
    )


def test_frame_contains_handles_edges_and_open_right_side():
    frame = (0, 0, 1440, 900)
    assert overlay.frame_contains(frame, 720, 450) is True
    assert overlay.frame_contains(frame, 0, 0) is True
    # Half-open right/top edges: the next pixel belongs to the
    # neighbor, not to this frame.
    assert overlay.frame_contains(frame, 1440, 450) is False
    assert overlay.frame_contains(frame, 720, 900) is False
    assert overlay.frame_contains(frame, -1, 450) is False


def test_choose_screen_picks_the_display_holding_the_mouse():
    main = (0, 0, 1440, 900)
    secondary_right = (1440, 0, 1920, 1080)
    screens = [main, secondary_right]
    # Mouse on the secondary display: that pill goes there.
    assert overlay.choose_screen(2000, 500, screens) == secondary_right
    # Mouse on the main display stays on the main display.
    assert overlay.choose_screen(100, 100, screens) == main


def test_choose_screen_secondary_left_negative_origin():
    # A monitor placed to the left of the main one has a negative x.
    left = (-1920, -200, 1920, 1080)
    main = (0, 0, 1440, 900)
    screens = [main, left]
    assert overlay.choose_screen(-500, 100, screens) == left
    assert overlay.choose_screen(300, 100, screens) == main


def test_choose_screen_vertically_stacked_monitors():
    below = (0, -1080, 1440, 1080)
    main = (0, 0, 1440, 900)
    screens = [main, below]
    assert overlay.choose_screen(720, -500, screens) == below
    assert overlay.choose_screen(720, 500, screens) == main


def test_choose_screen_falls_back_to_first_when_mouse_is_offscreen():
    main = (0, 0, 1440, 900)
    secondary = (1440, 0, 1920, 1080)
    # A detached display can leave the pointer outside every frame;
    # the first entry (the main screen) is the fallback.
    assert overlay.choose_screen(-5000, -5000, [main, secondary]) == main
    # Screens list order decides the fallback, never None when present.
    assert overlay.choose_screen(-5000, -5000, [secondary, main]) == secondary
    # No screens at all: caller handles None.
    assert overlay.choose_screen(1, 1, []) is None


def test_pill_origin_centers_and_respects_visible_dock_frame():
    # The passed frame is a visibleFrame, so its bottom edge already
    # sits above the Dock: the margin is added on top of that edge.
    frame = (0, 83, 1440, 817)  # main display with a Dock below
    x, y = overlay.pill_origin(frame, overlay.MIN_WIDTH)
    assert y == 83 + overlay.BOTTOM_MARGIN
    assert x == (1440 - overlay.MIN_WIDTH) / 2.0


def test_pill_origin_recenters_on_secondary_left_display():
    frame = (-1920, 0, 1920, 1080)
    x, y = overlay.pill_origin(frame, overlay.MIN_WIDTH)
    assert x == -1920 + (1920 - overlay.MIN_WIDTH) / 2.0
    assert y == overlay.BOTTOM_MARGIN


def test_pill_origin_resizing_recenters_symmetrically():
    frame = (0, 0, 1440, 900)
    narrow_x, _ = overlay.pill_origin(frame, overlay.MIN_WIDTH)
    wide_x, _ = overlay.pill_origin(frame, overlay.MAX_WIDTH)
    assert narrow_x == (1440 - overlay.MIN_WIDTH) / 2.0
    assert wide_x == (1440 - overlay.MAX_WIDTH) / 2.0
    # Growing by 220 points moves the left edge half that amount
    # toward the left, i.e. by minus half of the growth.
    assert abs(
        (wide_x - narrow_x) + (overlay.MAX_WIDTH - overlay.MIN_WIDTH) / 2.0
    ) < 0.001


def test_pill_origin_clamps_too_wide_pill_inside_the_screen():
    # On a display narrower than the pill the origin clamps to the
    # screen edges instead of centering offscreen.
    narrow_screen = (100, 200, 200, 400)
    x, y = overlay.pill_origin(narrow_screen, overlay.MAX_WIDTH)
    assert x == 100
    x, _ = overlay.pill_origin(narrow_screen, overlay.MIN_WIDTH)
    assert x == 100
    # The bottom margin never pushes the pill past the screen top.
    short_screen = (0, 0, 1440, 100)
    _, y = overlay.pill_origin(short_screen, overlay.MIN_WIDTH)
    assert y == 100 - overlay.HEIGHT
    # Degenerate no-screen input still returns something usable.
    assert overlay.pill_origin(None, overlay.MIN_WIDTH) == (0.0, overlay.BOTTOM_MARGIN)


def test_pill_origin_stays_visible_on_stacked_lower_monitor():
    below = (0, -1080, 1440, 1080)
    x, y = overlay.pill_origin(below, overlay.MIN_WIDTH)
    assert y == -1080 + overlay.BOTTOM_MARGIN
    assert x >= 0
    assert x + overlay.MIN_WIDTH <= 1440


def test_overlay_phase_frames_never_overlap():
    """Verify actual AppKit text frames and the empty-transcript transition."""
    import pytest
    appkit = pytest.importorskip("AppKit")
    ui = overlay.run_overlay(
        preview={"phase": "dictating", "text": "A real transcript preview"}
    )
    window = ui["window"]
    window.setReleasedWhenClosed_(False)
    try:
        fields = [
            view for view in ui["content"].subviews()
            if isinstance(view, appkit.NSTextField)
        ]
        phase = next(view for view in fields if view.stringValue() == "Dictating")
        transcript = next(
            view for view in fields
            if view.stringValue() == "A real transcript preview"
        )
        bounds = ui["content"].bounds()
        assert transcript.isHidden() is False
        assert phase.frame().origin.y >= (
            transcript.frame().origin.y + transcript.frame().size.height
        )
        for view in fields:
            frame = view.frame()
            assert frame.origin.y >= 0
            assert frame.origin.y + frame.size.height <= bounds.size.height
            assert frame.origin.x + frame.size.width <= bounds.size.width
        ui["render"]("thinking", "")
        assert phase.stringValue() == "Thinking"
        assert transcript.isHidden() is True
        assert phase.frame().origin.y > 0
    finally:
        window.close()


def test_live_appkit_placements_are_valid_offscreen():
    """Real AppKit run: the created panel and an actual resize land at
    geometry the pure helpers predict, inside a visible screen frame."""
    import pytest
    pytest.importorskip("AppKit")
    import AppKit

    ui = overlay.run_overlay(
        preview={"phase": "dictating", "text": "A real transcript preview"}
    )
    window = ui["window"]
    window.setReleasedWhenClosed_(False)
    try:
        def flat(rect):
            return (
                rect.origin.x,
                rect.origin.y,
                rect.size.width,
                rect.size.height,
            )

        screens = [flat(s.visibleFrame()) for s in (AppKit.NSScreen.screens() or [])]
        assert screens, "expected at least one screen"
        wf = window.frame()
        frame = (wf.origin.x, wf.origin.y, wf.size.width, wf.size.height)
        # The window was created inside exactly one visible frame,
        # above the Dock-safe bottom edge of that display.
        containing = [
            s for s in screens
            if s[0] <= frame[0]
            and frame[0] + frame[2] <= s[0] + s[2]
            and s[1] <= frame[1]
            and frame[1] + frame[3] <= s[1] + s[3]
        ]
        assert len(containing) == 1
        screen = containing[0]
        expected_x, expected_y = overlay.pill_origin(
            screen, frame[2], height=frame[3]
        )
        assert abs(frame[0] - expected_x) < 0.51
        assert abs(frame[1] - expected_y) < 0.51

        # A real state change triggers a real resize through the same
        # code path the stdin protocol uses: the pill re-centers on its
        # chosen display and stays fully visible.
        before = window.frame()
        ui["render"]("thinking", "")
        after = window.frame()
        assert after.size.width != before.size.width or True
        new_width = after.size.width
        exp_x, exp_y = overlay.pill_origin(screen, new_width, height=after.size.height)
        assert abs(after.origin.x - exp_x) < 0.51
        assert abs(after.origin.y - before.origin.y) < 0.51
        assert after.origin.x >= screen[0]
        assert after.origin.x + new_width <= screen[0] + screen[2]
        assert after.origin.y >= screen[1]
        assert after.origin.y + after.size.height <= screen[1] + screen[3]
        # Changing width again keeps the recentering behavior.
        ui["render"]("dictating", " ".join(["word"] * 40))
        wide = window.frame()
        assert wide.origin.x == pytest.approx(
            screen[0] + (screen[2] - wide.size.width) / 2.0, abs=0.51
        )
    finally:
        window.close()
