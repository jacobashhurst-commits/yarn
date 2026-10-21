"""An English app that can understand a meeting in another language."""

from meeting_archive.languages import (
    resolve_spoken,
    summary_instruction,
    uses_english_names,
)
from meeting_archive.records import Transcript, render_transcript_html
from meeting_archive.summarize import _summary_prompt
from meeting_archive.models import Turn


def test_english_stays_the_default_and_other_languages_are_stored():
    assert resolve_spoken("", "") == "en"
    assert resolve_spoken("en", "ja") == "en"
    assert resolve_spoken("japanese", "") == "ja"
    assert resolve_spoken("auto", "ja") == "ja"
    assert resolve_spoken("auto", "") == "auto"
    assert uses_english_names("en")
    assert uses_english_names("")
    assert not uses_english_names("ja")
    assert not uses_english_names("auto")


def test_an_english_summary_prompt_is_unchanged_and_japanese_asks_for_japanese():
    english = _summary_prompt("Hello.", partial=False)
    japanese = _summary_prompt("こんにちは。", partial=False, language="ja")
    assert "Write the summary in" not in english
    assert "Write the summary in Japanese" in japanese
    assert "こんにちは。" in japanese


def test_a_known_job_title_sits_under_the_name():
    transcript = Transcript(
        meeting_id="2026-10-21T09-00-00",
        turns=[Turn("room", 0.0, 4.0, "The budget can wait.", speaker="room-1")],
        language="ja",
    )
    html = render_transcript_html(
        transcript,
        lambda turn: "Ada",
        {"speakers": {"room-1": "Ada"}},
        titles={"ada": "Finance director"},
        companies={"ada": "Northwind"},
    )
    assert "Ada" in html
    assert '<div class="role">Finance director, Northwind</div>' in html
    assert "Japanese" in html
