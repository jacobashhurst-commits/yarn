"""Model emphasis must show up as bold, not as asterisks."""

from meeting_archive.markup import markup_inline, markup_to_html
from meeting_archive.models import ROOM, Turn
from meeting_archive.records import Transcript, render_transcript_html


def test_single_and_double_asterisks_become_bold():
    html = markup_to_html("Please *send* the **note**.")
    assert "<strong>send</strong>" in html
    assert "<strong>note</strong>" in html
    assert "*" not in html


def test_a_heading_and_bullets_are_not_left_as_markup():
    html = markup_to_html("**Key points**\n- Ship Friday\n- *Ask* Sam")
    assert "<h3>Key points</h3>" in html
    assert "<li>Ship Friday</li>" in html
    assert "<strong>Ask</strong>" in html
    assert "*" not in html


def test_a_star_inside_code_does_not_break_the_page():
    html = markup_inline("use `a * b` and `c * d`")
    assert html.count("<code>") == 2
    assert "</code>" in html
    assert "<strong>" not in html


def test_markup_escapes_html_before_it_adds_bold():
    html = markup_inline("Use <script> and *ok*")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>ok</strong>" in html


def test_transcript_page_shows_bold_not_asterisks():
    transcript = Transcript(
        meeting_id="2026-09-25T11-00-00",
        duration_s=65,
        title="Planning",
        summary="**Key points**\n- Jacob will *send* the note.",
        turns=[Turn(ROOM, 1, 4, "Hello there.", speaker="room-1")],
    )
    html = render_transcript_html(transcript, lambda turn: "Jacob", {})
    assert "<h3>Key points</h3>" in html
    assert "<strong>send</strong>" in html
    assert "*send*" not in html
    assert "**Key" not in html
