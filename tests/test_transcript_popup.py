"""Pure tests for transcript_popup.py, the manual-copy fallback popup.

The AppKit window itself is exercised live by the runtime; these tests
pin the protocol helpers and the layout math that must not regress:
payload parsing tolerates damage, the Copy reply carries the popup's
current (possibly edited) body, the preview panel can take key status
for its editable body without becoming activating, and the height math
keeps the body at its ~6 visible lines.
"""

import json

import pytest

import transcript_popup


def test_parse_payload_reads_transcript_text():
    line = json.dumps({"text": "ship it"})
    assert transcript_popup.parse_payload(line) == "ship it"


def test_parse_payload_tolerates_damage():
    assert transcript_popup.parse_payload("not json") == ""
    assert transcript_popup.parse_payload(json.dumps([])) == ""
    assert transcript_popup.parse_payload(json.dumps({"other": 1})) == ""


def test_copy_reply_carries_the_current_text():
    reply = transcript_popup.copy_reply("please call Sinead about the meeting")
    assert reply == (
        '{"type": "copy_used", "text": "please call Sinead about the meeting"}'
    )
    assert "\n" not in reply


def test_height_matches_the_layout_constants():
    assert transcript_popup.HEIGHT == transcript_popup.popup_height()
    # A shorter body cap shrinks the panel by whole line heights.
    assert (
        transcript_popup.popup_height() - transcript_popup.popup_height(lines=3)
        == transcript_popup.BODY_LINE_H * (transcript_popup.BODY_LINES - 3)
    )


def test_title_is_fixed():
    assert transcript_popup.TITLE_TEXT == "Transcript ready to paste"


def run_preview():
    """Build the offscreen panel the live protocol path uses.

    Skips when PyObjC/AppKit are unavailable, so the suite never depends
    on a window server beyond this machine's helpers.
    """
    return transcript_popup.run_popup(
        "please call Shunade about the meeting", preview=True
    )


def test_preview_body_is_an_editable_plain_text_view():
    pytest.importorskip("AppKit")
    preview = run_preview()
    body = preview["body"]
    # The body accepts edits (the misheard-word fixup path), stays
    # selectable, and stays plain verbatim text: rich text off and every
    # automatic substitution the original flags turned off, now honored
    # for the editing path too.
    assert bool(body.isEditable())
    assert bool(body.isSelectable())
    assert not bool(body.isRichText())
    assert not bool(body.isAutomaticQuoteSubstitutionEnabled())
    assert not bool(body.isAutomaticDashSubstitutionEnabled())
    assert not bool(body.isAutomaticTextReplacementEnabled())
    assert not bool(body.isAutomaticSpellingCorrectionEnabled())


def test_preview_body_holds_edits_the_copy_reply_would_ship():
    pytest.importorskip("AppKit")
    preview = run_preview()
    body = preview["body"]
    # The view reflects edits, and the reply composed from the live body
    # at Copy time would carry the edited text, not the show-time
    # transcript.
    fixed = "please call Sinead about the meeting"
    body.setString_(fixed)
    assert str(body.string()) == fixed
    assert json.loads(transcript_popup.copy_reply(str(body.string()))) == {
        "type": "copy_used",
        "text": fixed,
    }


def test_preview_panel_takes_key_only_by_explicit_policy():
    pytest.importorskip("AppKit")
    from AppKit import NSWindowStyleMaskNonactivatingPanel

    preview = run_preview()
    window = preview["window"]
    # A borderless panel refuses key status by default; the override is
    # what lets the editable body receive keyboard focus on click, while
    # the nonactivating style keeps the panel from ever activating the
    # app or stealing focus at show time.
    assert bool(window.canBecomeKeyWindow())
    assert int(window.styleMask()) & int(NSWindowStyleMaskNonactivatingPanel)
    assert not bool(window.isVisible())
