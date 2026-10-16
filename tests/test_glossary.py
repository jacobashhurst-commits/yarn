from meeting_archive.glossary import apply_glossary, apply_glossary_to_transcript
from meeting_archive.models import Segment, Turn
from meeting_archive.records import Transcript


def test_zora_becomes_zuora_whole_word_only():
    assert apply_glossary("we use zora for billing", {"Zora": "Zuora"}) == (
        "we use Zuora for billing"
    )
    assert apply_glossary("explore options with them", {"Zora": "Zuora"}) == (
        "explore options with them"
    )


def test_apply_glossary_to_transcript_updates_turns_and_summary():
    transcript = Transcript(
        meeting_id="2026-10-16T11-00-32",
        segments=[Segment("room", 0.0, 1.0, "zora marketing", speaker="room-1")],
        turns=[Turn("room", 0.0, 1.0, "zora marketing", speaker="room-1")],
        summary="Speaker 1 from Zora joined the call.",
    )
    changed = apply_glossary_to_transcript(transcript, {"Zora": "Zuora"})
    assert changed
    assert transcript.turns[0].text == "Zuora marketing"
    assert "Zuora" in transcript.summary
