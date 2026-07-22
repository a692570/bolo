"""Pure tests for safe Accessibility target matching."""

from accessibility_context import target_range_before_caret


def test_matches_exact_text_immediately_before_caret():
    assert target_range_before_caret("hello dictated text", 19, 0, "dictated text") == (6, 13)


def test_allows_one_trailing_space_after_dictation():
    assert target_range_before_caret("hello dictated text ", 20, 0, "dictated text") == (6, 14)


def test_refuses_moved_caret_or_changed_text():
    assert target_range_before_caret("dictated text then moved", 24, 0, "dictated text") is None
    assert target_range_before_caret("hello edited text", 17, 0, "dictated text") is None


def test_refuses_existing_selection():
    assert target_range_before_caret("dictated text", 0, 14, "dictated text") is None
