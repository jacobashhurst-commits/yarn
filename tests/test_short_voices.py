"""Who counts as a participant, written from how meetings actually go.

Nothing here is tuned to a recording. Each case is a shape every meeting
has: a "yep", a laugh over someone's sentence, a listener saying "mm" while
another person explains, someone who goes quiet for forty minutes and
comes back. Voiceprint scores are given to the functions directly, so the
rule is tested and not the embedding model.
"""

from __future__ import annotations

import pytest

from meeting_archive import pipeline
from meeting_archive.config import Config
from meeting_archive.diarize import (
    absorb_short_voices,
    assign_speakers,
    group_same_voices,
    settle_brief_voices,
)
from meeting_archive.merge import is_backchannel, merge_segments
from meeting_archive.models import MIC, ROOM, SYS, Segment, SpeakerSpan, Turn
from meeting_archive.quips import profile_speakers
from meeting_archive.records import Transcript, read_notes, write_json_atomic
from meeting_archive.summarize import (
    _summary_prompt,
    summary_turns,
    unattributed_labels,
)


class FakeStore:
    def replace_meeting(self, meeting_id, documents, metadatas):
        return len(documents)

    def delete_meeting(self, meeting_id):
        pass


def speakers(spans):
    return [span.speaker for span in spans]


# --- blips ---------------------------------------------------------------


def test_a_yep_is_not_a_person():
    """1.2s that matches nobody: no Speaker 3, and nobody is asked about it."""
    spans = [
        SpeakerSpan(ROOM, 0.0, 40.0, "room-1"),
        SpeakerSpan(ROOM, 40.5, 41.7, "room-2"),
        SpeakerSpan(ROOM, 42.0, 80.0, "room-3"),
    ]
    cleaned = settle_brief_voices(
        spans,
        voice_scores={("room-2", "room-1"): 0.2, ("room-2", "room-3"): 0.3},
    )
    assert speakers(cleaned) == ["room-1", "room-2"]
    assert [span.start for span in cleaned] == [0.0, 42.0]


def test_a_mismatch_from_two_seconds_does_not_make_a_new_person():
    """The old rule: a measured mismatch stays, however short.

    Two seconds of audio scores low against the same person as often as
    not, so that rule turned every short remark into a speaker.
    """
    spans = [
        SpeakerSpan(SYS, 0.0, 60.0, "sys-1"),
        SpeakerSpan(SYS, 61.0, 63.0, "sys-2"),
    ]
    cleaned = settle_brief_voices(spans, voice_scores={("sys-2", "sys-1"): 0.21})
    assert speakers(cleaned) == ["sys-1"]


def test_a_blip_that_sounds_like_a_participant_is_that_participant():
    spans = [
        SpeakerSpan(ROOM, 0.0, 30.0, "room-1"),
        SpeakerSpan(ROOM, 31.0, 60.0, "room-2"),
        SpeakerSpan(ROOM, 60.5, 62.0, "room-3"),
    ]
    cleaned = settle_brief_voices(
        spans,
        voice_scores={("room-3", "room-1"): 0.71, ("room-3", "room-2"): 0.2},
    )
    assert speakers(cleaned) == ["room-1", "room-2", "room-1"]


def test_a_blip_is_never_given_to_whoever_was_talking_nearby():
    """No fingerprint at all. The neighbour is the one person it was not."""
    spans = [
        SpeakerSpan(ROOM, 0.0, 30.0, "room-1"),
        SpeakerSpan(ROOM, 30.0, 30.6, "room-2"),
        SpeakerSpan(ROOM, 30.6, 60.0, "room-1"),
    ]
    cleaned = settle_brief_voices(spans)
    assert speakers(cleaned) == ["room-1", "room-1"]
    assert all(span.end - span.start > 1 for span in cleaned)


def test_six_seconds_that_do_not_match_is_still_a_second_person():
    """Above the floor a mismatch means something. MHTB Room 1 stays two people."""
    spans = [
        SpeakerSpan(ROOM, 2.3, 8.6, "room-1"),
        SpeakerSpan(ROOM, 17.8, 29.3, "room-2"),
    ]
    scores = {("room-1", "room-2"): 0.21}
    cleaned = settle_brief_voices(spans, voice_scores=scores)
    cleaned = absorb_short_voices(cleaned, voice_scores=scores)
    assert speakers(cleaned) == ["room-1", "room-2"]


def test_someone_who_joined_late_and_said_one_real_thing_is_a_participant():
    spans = [
        SpeakerSpan(SYS, 0.0, 600.0, "sys-1"),
        SpeakerSpan(SYS, 1500.0, 1504.5, "sys-2"),
    ]
    scores = {("sys-2", "sys-1"): 0.2}
    cleaned = settle_brief_voices(spans, voice_scores=scores)
    cleaned = absorb_short_voices(cleaned, voice_scores=scores)
    assert speakers(cleaned) == ["sys-1", "sys-2"]


def test_the_floor_comes_from_config():
    spans = [
        SpeakerSpan(ROOM, 0.0, 30.0, "room-1"),
        SpeakerSpan(ROOM, 31.0, 35.0, "room-2"),
    ]
    scores = {("room-2", "room-1"): 0.1}
    assert speakers(settle_brief_voices(spans, voice_scores=scores)) == [
        "room-1",
        "room-2",
    ]
    assert speakers(
        settle_brief_voices(spans, voice_scores=scores, min_participant_s=5.0)
    ) == ["room-1"]
    assert Config().diarize.min_participant_s == 3.0


def test_a_voice_already_named_is_left_alone_however_short():
    spans = [
        SpeakerSpan(ROOM, 0.0, 30.0, "room-1"),
        SpeakerSpan(ROOM, 31.0, 32.0, "room-2"),
    ]
    cleaned = settle_brief_voices(spans, keep={"room-2"}, renumber=False)
    assert speakers(cleaned) == ["room-1", "room-2"]


def test_keys_are_not_renumbered_when_names_are_stored_against_them():
    spans = [
        SpeakerSpan(ROOM, 0.0, 0.5, "room-1"),
        SpeakerSpan(ROOM, 1.0, 30.0, "room-2"),
    ]
    cleaned = settle_brief_voices(spans, renumber=False)
    assert speakers(cleaned) == ["room-2"]


# --- what the transcript ends up attributing -------------------------------


def test_a_voice_that_only_won_one_word_is_not_a_speaker():
    """Four seconds of spans, but the sentences went to the person talking."""
    spans = [
        SpeakerSpan(ROOM, 0.0, 20.0, "room-1"),
        SpeakerSpan(ROOM, 20.0, 21.0, "room-2"),
        SpeakerSpan(ROOM, 21.0, 40.0, "room-1"),
        # Overlapped by room-1, so these never win a sentence.
        SpeakerSpan(ROOM, 5.0, 6.5, "room-2"),
        SpeakerSpan(ROOM, 25.0, 26.5, "room-2"),
    ]
    segments = [
        Segment(ROOM, 0.0, 20.0, "Here is the long explanation."),
        Segment(ROOM, 20.0, 21.0, "Right."),
        Segment(ROOM, 21.0, 40.0, "And the rest of it."),
    ]
    assign_speakers(segments, spans, min_participant_s=3.0)
    assert [s.speaker for s in segments] == ["room-1", None, "room-1"]
    profiles = profile_speakers(merge_segments(segments))
    assert [p.speaker for p in profiles] == ["room-1"]


def test_without_the_floor_assignment_is_unchanged():
    spans = [
        SpeakerSpan(ROOM, 0.0, 20.0, "room-1"),
        SpeakerSpan(ROOM, 20.0, 21.0, "room-2"),
    ]
    segments = [
        Segment(ROOM, 0.0, 20.0, "Long."),
        Segment(ROOM, 20.0, 21.0, "Right."),
    ]
    assert assign_speakers(segments, spans) == 2
    assert [s.speaker for s in segments] == ["room-1", "room-2"]


# --- crosstalk and overlap --------------------------------------------------


def test_an_interruption_keeps_its_own_line():
    """Both marked at once: the shorter span is the reply, and it is real."""
    spans = [
        SpeakerSpan(ROOM, 0.0, 30.0, "room-1"),
        SpeakerSpan(ROOM, 10.0, 14.0, "room-2"),
        SpeakerSpan(ROOM, 40.0, 60.0, "room-2"),
    ]
    segments = [
        Segment(ROOM, 0.0, 10.0, "So the plan is to ship on Friday."),
        Segment(ROOM, 10.0, 14.0, "Hang on, Friday is the freeze."),
        Segment(ROOM, 14.0, 30.0, "Then Thursday."),
        Segment(ROOM, 40.0, 60.0, "Thursday works for the release team."),
    ]
    assign_speakers(segments, spans, min_participant_s=3.0)
    assert [s.speaker for s in segments] == ["room-1", "room-2", "room-1", "room-2"]


def test_a_listeners_mm_does_not_cut_the_speakers_turn_in_two():
    segments = [
        Segment(SYS, 0.0, 8.0, "The migration has three stages.", "sys-1"),
        Segment(MIC, 8.1, 8.6, "Mm."),
        Segment(SYS, 8.8, 16.0, "The second stage is the risky one.", "sys-1"),
    ]
    turns = merge_segments(segments, backchannel_max_s=1.5)
    assert [(t.voice, t.text) for t in turns] == [
        ("sys-1", "The migration has three stages. The second stage is the risky one."),
        ("mic", "Mm."),
    ]
    assert [t.start for t in turns] == sorted(t.start for t in turns)


def test_several_back_channels_in_one_explanation():
    segments = [
        Segment(ROOM, 0.0, 6.0, "First we export.", "room-1"),
        Segment(ROOM, 6.1, 6.5, "Yeah.", "room-2"),
        Segment(ROOM, 6.6, 12.0, "Then we transform.", "room-1"),
        Segment(ROOM, 12.1, 12.6, "Okay.", "room-3"),
        Segment(ROOM, 12.7, 18.0, "Then we load.", "room-1"),
    ]
    turns = merge_segments(segments, backchannel_max_s=1.5)
    assert turns[0].text == "First we export. Then we transform. Then we load."
    assert [t.voice for t in turns] == ["room-1", "room-2", "room-3"]


def test_a_real_reply_still_ends_the_turn():
    segments = [
        Segment(ROOM, 0.0, 6.0, "Can we ship Friday?", "room-1"),
        Segment(ROOM, 6.2, 7.0, "No, Thursday.", "room-2"),
        Segment(ROOM, 7.2, 9.0, "Fine.", "room-1"),
    ]
    turns = merge_segments(segments, backchannel_max_s=1.5)
    assert [t.voice for t in turns] == ["room-1", "room-2", "room-1"]


def test_a_long_yeah_is_an_answer_not_a_back_channel():
    assert is_backchannel("Yeah.", 0.5, 1.5)
    assert not is_backchannel("Yeah.", 2.5, 1.5)
    assert not is_backchannel("Yeah I think so too", 1.0, 1.5)
    assert not is_backchannel("Thursday.", 0.5, 1.5)


def test_the_speaker_must_actually_carry_on():
    """After a long pause the "yeah" is the next turn, not an aside."""
    segments = [
        Segment(ROOM, 0.0, 6.0, "That is everything from me.", "room-1"),
        Segment(ROOM, 12.0, 12.5, "Okay.", "room-2"),
        Segment(ROOM, 13.0, 20.0, "Next item.", "room-1"),
    ]
    turns = merge_segments(segments, coalesce_gap_s=2.0, backchannel_max_s=1.5)
    assert [t.voice for t in turns] == ["room-1", "room-2", "room-1"]


def test_back_channel_handling_is_off_unless_asked_for():
    segments = [
        Segment(ROOM, 0.0, 6.0, "First.", "room-1"),
        Segment(ROOM, 6.1, 6.5, "Yeah.", "room-2"),
        Segment(ROOM, 6.6, 12.0, "Second.", "room-1"),
    ]
    assert len(merge_segments(segments)) == 3


# --- one person, split in two -----------------------------------------------


def test_someone_returning_after_a_long_gap_is_one_person():
    """Spoke at the start, silent for forty minutes, came back as a new cluster."""
    durations = {"room-1": 90.0, "room-2": 600.0, "room-3": 45.0}
    scores = {
        frozenset(("room-1", "room-3")): 0.66,
        frozenset(("room-1", "room-2")): 0.18,
        frozenset(("room-2", "room-3")): 0.22,
    }
    groups = group_same_voices(
        list(durations), lambda a, b: scores[frozenset((a, b))], durations
    )
    assert groups["room-3"] == "room-1"
    assert groups["room-2"] == "room-2"


def test_a_voiceprint_is_taken_from_across_the_meeting():
    """Not only from the single longest stretch."""
    np = pytest.importorskip("numpy")
    from meeting_archive.diarize import _speaker_waveform

    rate = 100
    samples = np.zeros(4000 * rate, dtype=np.float32)
    samples[0 : 60 * rate] = 1.0
    samples[2000 * rate : 2010 * rate] = 2.0
    samples[3900 * rate : 3910 * rate] = 3.0
    spans = [
        SpeakerSpan(ROOM, 0.0, 60.0, "room-1"),
        SpeakerSpan(ROOM, 2000.0, 2010.0, "room-1"),
        SpeakerSpan(ROOM, 3900.0, 3910.0, "room-1"),
    ]
    clip = _speaker_waveform(samples, rate, spans, "room-1", max_s=12.0)
    assert len(clip) == 12 * rate
    assert set(np.unique(clip).tolist()) == {1.0, 2.0, 3.0}


# --- summaries ---------------------------------------------------------------


def test_filler_lines_are_not_shown_to_the_summary_model():
    turns = [
        Turn(SYS, 0.0, 8.0, "We should move the launch to March.", "sys-1"),
        Turn(MIC, 8.1, 8.5, "Yeah."),
        Turn(SYS, 9.0, 12.0, "Mm, okay.", "sys-2"),
        Turn(MIC, 13.0, 16.0, "March works for finance."),
    ]
    assert [t.text for t in summary_turns(turns)] == [
        "We should move the launch to March.",
        "March works for finance.",
    ]


def test_a_meeting_of_only_filler_is_still_summarised_from_what_there_is():
    turns = [Turn(MIC, 0.0, 0.5, "Yeah."), Turn(SYS, 1.0, 1.5, "Okay.")]
    assert summary_turns(turns) == turns


def test_an_unattributed_line_is_not_presented_as_a_participant():
    cfg = Config()
    label = cfg.turn_labeller({}, {"room-1": 1, "room-2": 2})
    turns = [
        Turn(ROOM, 0.0, 8.0, "We ship Thursday.", "room-1"),
        Turn(ROOM, 8.5, 9.5, "Is the build green"),
        Turn(ROOM, 10.0, 18.0, "It went green this morning.", "room-2"),
    ]
    loose = unattributed_labels(turns, label)
    assert loose == ["Unknown"]
    prompt = _summary_prompt("x", partial=False, unattributed=loose)
    assert '"Unknown"' in prompt and "not a participant" in prompt


def test_an_unseparated_channel_is_not_called_unattributed():
    """With no diarization, Other is simply the far side of the call."""
    label = Config().turn_labeller({}, {})
    turns = [Turn(MIC, 0.0, 2.0, "Hello."), Turn(SYS, 2.0, 4.0, "Hi there.")]
    assert unattributed_labels(turns, label) == []
    assert "not a participant" not in _summary_prompt("x", partial=False)


# --- old meetings -------------------------------------------------------------


@pytest.fixture
def old_meeting(tmp_path):
    cfg = Config(root=tmp_path / "archive")
    cfg.ensure_dirs()
    meeting_id = "2026-09-11T10-00-00"
    spans = [
        SpeakerSpan(ROOM, 0.0, 30.0, "room-1"),
        SpeakerSpan(ROOM, 30.2, 31.4, "room-2"),
        SpeakerSpan(ROOM, 32.0, 70.0, "room-3"),
        SpeakerSpan(ROOM, 70.5, 71.5, "room-4"),
    ]
    segments = [
        Segment(ROOM, 0.0, 30.0, "The first long contribution.", "room-1"),
        Segment(ROOM, 30.2, 31.4, "Yep.", "room-2"),
        Segment(ROOM, 32.0, 70.0, "The second long contribution.", "room-3"),
        Segment(ROOM, 70.5, 71.5, "Thanks all.", "room-4"),
    ]
    turns = merge_segments(segments)
    transcript = Transcript(
        meeting_id=meeting_id,
        segments=segments,
        turns=turns,
        duration_s=72.0,
        speaker_spans=spans,
        speaker_profiles=profile_speakers(turns),
        summary="Speaker 2 agreed.",
    )
    path = cfg.transcript_json(meeting_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript.write(path)
    return cfg, meeting_id


def test_old_meetings_get_the_rule_even_after_the_audio_is_gone(old_meeting):
    cfg, meeting_id = old_meeting
    changed = pipeline.fold_short_voices(cfg, meeting_id, FakeStore())
    assert changed == 2
    after = Transcript.read(cfg.transcript_json(meeting_id))
    assert [p.speaker for p in after.speaker_profiles] == ["room-1", "room-3"]
    assert [s.speaker for s in after.segments] == ["room-1", None, "room-3", None]
    # Nothing that was said is lost.
    assert [s.text for s in after.segments][1] == "Yep."
    # The old summary named a speaker who is no longer listed.
    assert after.summary == ""
    # Running it again changes nothing.
    assert pipeline.fold_short_voices(cfg, meeting_id, FakeStore()) == 0


def test_a_name_already_saved_survives_the_rule(old_meeting):
    cfg, meeting_id = old_meeting
    write_json_atomic(
        cfg.notes_json(meeting_id),
        {"meeting_id": meeting_id, "speakers": {"room-4": "Priya", "room-1": "Sam"}},
    )
    pipeline.fold_short_voices(cfg, meeting_id, FakeStore())
    after = Transcript.read(cfg.transcript_json(meeting_id))
    assert [s.speaker for s in after.segments] == ["room-1", None, "room-3", "room-4"]
    assert read_notes(cfg.notes_json(meeting_id))["speakers"] == {
        "room-4": "Priya",
        "room-1": "Sam",
    }
