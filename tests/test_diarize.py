"""Tests for diarization, speaker labelling, quips and name-to-voice guessing.

sherpa-onnx inference itself is stubbed; model execution does not belong in the
fast test run.
What is tested for real is everything built on top of it, and that is where the
bugs live: renumbering, overlap attribution, turn splitting, and the rule that
a voice we cannot identify never gets a name.

Two tests here are mutation checks in the same spirit as the channel-mapping
one in test_pipeline.py:

  test_spans_are_renumbered_by_first_appearance
      clustering labels are arbitrary. If they are passed through
      instead of renumbered, "Speaker 1" silently becomes "whichever cluster
      the model happened to emit first", and Jacob cannot check a single label
      against his memory of the meeting.

  test_turns_break_on_speaker_change_within_a_channel
      if turns collapse on channel alone, three people round one microphone
      become one turn with one label, and the whole feature is inert while
      appearing to work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meeting_archive import records
from meeting_archive.config import Config, DiarizeConfig
from meeting_archive.diarize import (
    DiarizationUnavailable,
    assign_speakers,
    absorb_short_voices,
    drop_brief_voices,
    choose_richer_split,
    drop_tiny_extra_cluster,
    group_same_voices,
    speaker_keys,
    to_spans,
)
from meeting_archive.merge import merge_segments, split_long_turns
from meeting_archive.models import (
    MIC,
    ROOM,
    SYS,
    Segment,
    SpeakerProfile,
    SpeakerSpan,
    Turn,
    speaker_display_indices,
    speaker_index,
    speaker_key,
)
from meeting_archive.names import infer_speaker_names, speaker_guess_map
from meeting_archive.audio import spoken_seconds
from meeting_archive.quips import pick_quip, profile_speakers, score_sentence, trim
from meeting_archive.records import Transcript


# ---------------------------------------------------------------------------
# speaker keys
# ---------------------------------------------------------------------------

def test_speaker_key_roundtrip():
    assert speaker_key("room", 3) == "room-3"
    assert speaker_index("room-3") == 3
    assert speaker_index("room") is None
    assert speaker_index("") is None
    assert speaker_index("mic-notanumber") is None


def test_speaker_display_indices_renumber_by_first_appearance():
    profiles = [
        SpeakerProfile("room-1", "room", first_at=0.0),
        SpeakerProfile("room-8", "room", first_at=12.0),
    ]
    assert speaker_display_indices(profiles) == {"room-1": 1, "room-8": 2}
    # Same start time: the number in the key, so room-12 cannot become Speaker 2.
    tied = [
        SpeakerProfile("room-12", "room", first_at=0.0),
        SpeakerProfile("room-2", "room", first_at=0.0),
        SpeakerProfile("room-1", "room", first_at=0.0),
    ]
    assert speaker_display_indices(tied) == {"room-1": 1, "room-2": 2, "room-12": 3}


def test_speaker_label_uses_display_map_over_key_suffix(cfg):
    display = {"room-8": 5}
    assert cfg.speaker_label(ROOM, "room-8", {}, display=display) == "Speaker 5"


# ---------------------------------------------------------------------------
# to_spans
# ---------------------------------------------------------------------------

def test_spans_are_renumbered_by_first_appearance():
    """The diarizer's own cluster order is arbitrary and must not survive.

    SPEAKER_09 speaks first here, so it has to become room-1. Passing the
    model's numbering through would make "Speaker 1" mean nothing checkable.
    """
    raw = [
        (10.0, 12.0, "SPEAKER_02"),
        (0.0, 4.0, "SPEAKER_09"),
        (5.0, 8.0, "SPEAKER_00"),
        (14.0, 16.0, "SPEAKER_09"),
    ]
    spans = to_spans(raw, ROOM)

    assert [s.speaker for s in spans] == [
        "room-1",  # SPEAKER_09 at t=0
        "room-2",  # SPEAKER_00 at t=5
        "room-3",  # SPEAKER_02 at t=10
        "room-1",  # SPEAKER_09 again
    ]
    # And they come back in time order, whatever order they arrived in.
    assert [s.start for s in spans] == [0.0, 5.0, 10.0, 14.0]


def test_short_spans_are_dropped_before_numbering():
    """A crosstalk blip must not take the Speaker 1 slot.

    The blip is first in time, so dropping it *after* numbering would still
    leave the real opener as Speaker 2.
    """
    raw = [
        (0.0, 0.2, "SPEAKER_07"),   # a cough
        (1.0, 9.0, "SPEAKER_01"),   # the person who actually opened
        (10.0, 18.0, "SPEAKER_03"),
    ]
    spans = to_spans(raw, SYS, min_span_s=0.6)

    assert [s.speaker for s in spans] == ["sys-1", "sys-2"]
    assert spans[0].start == 1.0


def test_to_spans_empty_and_all_dropped():
    assert to_spans([], ROOM) == []
    assert to_spans([(0.0, 0.1, "A")], ROOM, min_span_s=0.5) == []


def test_same_voice_groups_a_chain_of_strong_matches():
    """A third fragment joins even when it is not anyone's nearest voice.

    room-11's nearest voice is room-4, and room-13 still clears 0.58 against
    room-11, so all three are one person. room-1 does not clear the bar
    against any of them. The kept key is the longest stretch.
    """
    speakers = ["room-4", "room-11", "room-13", "room-1"]
    durations = {"room-4": 180.0, "room-11": 200.0, "room-13": 20.0, "room-1": 120.0}
    scores = {
        frozenset(("room-4", "room-11")): 0.70,
        frozenset(("room-11", "room-13")): 0.61,
        frozenset(("room-4", "room-13")): 0.41,
        frozenset(("room-4", "room-1")): 0.02,
        frozenset(("room-11", "room-1")): 0.06,
        frozenset(("room-13", "room-1")): 0.11,
    }

    def cosine(left, right):
        return scores.get(frozenset((left, right)), 0.0)

    groups = group_same_voices(speakers, cosine, durations)
    assert groups["room-4"] == groups["room-11"] == groups["room-13"] == "room-11"
    assert groups["room-1"] == "room-1"


def test_a_moderate_resemblance_does_not_join_two_people():
    speakers = ["room-1", "room-2", "room-3"]
    durations = {"room-1": 100.0, "room-2": 80.0, "room-3": 40.0}
    scores = {
        frozenset(("room-1", "room-2")): 0.70,
        frozenset(("room-2", "room-3")): 0.50,
        frozenset(("room-1", "room-3")): 0.20,
    }

    def cosine(left, right):
        return scores.get(frozenset((left, right)), 0.0)

    groups = group_same_voices(speakers, cosine, durations)
    assert groups["room-1"] == groups["room-2"]
    assert groups["room-3"] == "room-3"


def test_a_short_fragment_is_folded_into_the_voice_it_sounds_like():
    spans = [
        SpeakerSpan(ROOM, 0.0, 30.0, "room-1"),
        SpeakerSpan(ROOM, 30.0, 34.0, "room-2"),
        SpeakerSpan(ROOM, 40.0, 70.0, "room-3"),
    ]
    cleaned = absorb_short_voices(
        spans,
        voice_scores={("room-2", "room-3"): 0.8, ("room-2", "room-1"): 0.2},
    )
    assert [span.speaker for span in cleaned] == ["room-1", "room-3", "room-3"]


def test_a_short_fragment_without_a_voice_match_follows_the_nearest_speaker():
    spans = [
        SpeakerSpan(ROOM, 0.0, 30.0, "room-1"),
        SpeakerSpan(ROOM, 30.0, 34.0, "room-2"),
        SpeakerSpan(ROOM, 50.0, 80.0, "room-3"),
    ]
    cleaned = absorb_short_voices(spans)
    assert [span.speaker for span in cleaned] == ["room-1", "room-1", "room-3"]


def test_a_short_meeting_keeps_everyone_who_spoke():
    spans = [
        SpeakerSpan(ROOM, 0.0, 4.0, "room-1"),
        SpeakerSpan(ROOM, 5.0, 9.0, "room-2"),
    ]
    cleaned = absorb_short_voices(spans)
    assert [span.speaker for span in cleaned] == ["room-1", "room-2"]


def test_a_voice_under_one_second_is_not_listed_as_a_speaker():
    spans = [
        SpeakerSpan(ROOM, 0.0, 12.0, "room-1"),
        SpeakerSpan(ROOM, 12.0, 12.4, "room-2"),
        SpeakerSpan(ROOM, 20.0, 20.5, "room-2"),
    ]
    cleaned = drop_brief_voices(spans)
    assert [span.speaker for span in cleaned] == ["room-1"]


def test_two_voices_who_each_spoke_for_more_than_a_second_both_stay():
    spans = [
        SpeakerSpan(ROOM, 0.0, 4.0, "room-2"),
        SpeakerSpan(ROOM, 4.0, 6.0, "room-1"),
    ]
    cleaned = drop_brief_voices(spans)
    assert [span.speaker for span in cleaned] == ["room-2", "room-1"]


def test_a_blip_does_not_keep_the_first_speaker_slot():
    spans = [
        SpeakerSpan(ROOM, 0.0, 0.4, "room-1"),
        SpeakerSpan(ROOM, 1.0, 8.0, "room-2"),
    ]
    cleaned = drop_brief_voices(spans)
    assert [(span.speaker, span.start) for span in cleaned] == [("room-1", 1.0)]


def test_one_tiny_overclustered_artifact_is_dropped_and_voices_renumbered():
    spans = [
        SpeakerSpan(ROOM, 0.0, 10.0, "room-4"),
        SpeakerSpan(ROOM, 10.0, 20.0, "room-2"),
        SpeakerSpan(ROOM, 20.0, 25.0, "room-3"),
        SpeakerSpan(ROOM, 25.0, 25.6, "room-1"),
    ]
    cleaned = drop_tiny_extra_cluster(spans, expected_speakers=3)
    assert cleaned is not None
    assert [span.speaker for span in cleaned] == [
        "room-1", "room-2", "room-3"
    ]


def test_two_tiny_clusters_are_ambiguous_and_neither_is_deleted():
    spans = [
        SpeakerSpan(ROOM, 0.0, 10.0, "room-1"),
        SpeakerSpan(ROOM, 10.0, 20.0, "room-2"),
        SpeakerSpan(ROOM, 20.0, 20.6, "room-3"),
        SpeakerSpan(ROOM, 21.0, 21.7, "room-4"),
    ]
    assert drop_tiny_extra_cluster(spans, expected_speakers=3) is None


def test_a_brief_but_substantive_fourth_voice_is_not_deleted():
    spans = [
        SpeakerSpan(ROOM, 0.0, 10.0, "room-1"),
        SpeakerSpan(ROOM, 10.0, 20.0, "room-2"),
        SpeakerSpan(ROOM, 20.0, 25.0, "room-3"),
        SpeakerSpan(ROOM, 25.0, 28.0, "room-4"),
    ]
    assert drop_tiny_extra_cluster(spans, expected_speakers=3) is None


def test_span_duration():
    assert SpeakerSpan(ROOM, 1.0, 3.5, "room-1").duration == 2.5
    # Never negative, whatever the diarizer emits.
    assert SpeakerSpan(ROOM, 3.0, 1.0, "room-1").duration == 0.0


# ---------------------------------------------------------------------------
# assign_speakers
# ---------------------------------------------------------------------------

def test_segments_go_to_the_speaker_holding_most_of_them():
    segments = [
        Segment(ROOM, 0.0, 2.0, "First thing."),
        Segment(ROOM, 5.0, 7.0, "Second thing."),
    ]
    spans = [
        SpeakerSpan(ROOM, 0.0, 3.0, "room-1"),
        SpeakerSpan(ROOM, 4.0, 8.0, "room-2"),
    ]
    assert assign_speakers(segments, spans) == 2
    assert [s.speaker for s in segments] == ["room-1", "room-2"]


def test_a_short_reply_inside_a_longer_voice_keeps_its_speaker():
    """Headphone audio marks both people at once. The reply is the short span."""
    segment = Segment(SYS, 3.96, 5.74, "Oh, my pleasure, glad you had a good time.")
    spans = [
        SpeakerSpan(SYS, 0.03, 12.74, "sys-1"),
        SpeakerSpan(SYS, 4.43, 5.87, "sys-2"),
    ]
    assign_speakers([segment], spans)
    assert segment.speaker == "sys-2"


def test_straddling_segment_goes_to_the_majority_holder():
    """A segment crossing a speaker change belongs to whoever holds more of it."""
    segment = Segment(ROOM, 0.0, 10.0, "A sentence across a handover.")
    spans = [
        SpeakerSpan(ROOM, 0.0, 3.0, "room-1"),   # 3s
        SpeakerSpan(ROOM, 3.0, 10.0, "room-2"),  # 7s -- wins
    ]
    assign_speakers([segment], spans)
    assert segment.speaker == "room-2"


def test_segment_with_no_overlap_is_left_unattributed():
    """Better an honest blank than the nearest guess."""
    segment = Segment(ROOM, 50.0, 52.0, "Said during a stretch nobody was assigned.")
    assign_speakers([segment], [SpeakerSpan(ROOM, 0.0, 3.0, "room-1")])
    assert segment.speaker is None


def test_spans_never_cross_channels():
    """A sys span must not attribute a mic segment, or the mic becomes a stranger."""
    mic = Segment(MIC, 0.0, 2.0, "Jacob talking.")
    sys_seg = Segment(SYS, 0.0, 2.0, "Someone else talking.")
    spans = [SpeakerSpan(SYS, 0.0, 5.0, "sys-1")]

    assign_speakers([mic, sys_seg], spans)
    assert mic.speaker is None
    assert sys_seg.speaker == "sys-1"


def test_assign_speakers_is_deterministic_on_a_tie():
    """An exact tie must resolve the same way every run, or tests flap."""
    for _ in range(5):
        segment = Segment(ROOM, 0.0, 4.0, "Dead heat.")
        spans = [
            SpeakerSpan(ROOM, 0.0, 2.0, "room-2"),
            SpeakerSpan(ROOM, 2.0, 4.0, "room-1"),
        ]
        assign_speakers([segment], spans)
        assert segment.speaker == "room-1"


def test_assign_speakers_with_no_spans_is_a_noop():
    segments = [Segment(SYS, 0.0, 1.0, "Anything.")]
    assert assign_speakers(segments, []) == 0
    assert segments[0].speaker is None


def test_speaker_keys_ordered_by_first_appearance():
    segments = [
        Segment(ROOM, 9.0, 10.0, "c", speaker="room-3"),
        Segment(ROOM, 1.0, 2.0, "a", speaker="room-2"),
        Segment(ROOM, 5.0, 6.0, "b", speaker="room-1"),
        Segment(ROOM, 0.5, 0.9, "x", speaker=None),
    ]
    assert speaker_keys(segments) == ["room-2", "room-1", "room-3"]


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------

def test_turns_break_on_speaker_change_within_a_channel():
    """Mutation check: collapsing on channel alone welds a whole room together.

    All four segments share the `room` channel and sit inside coalesce_gap_s.
    Grouping by channel gives one turn; grouping by voice gives three.
    """
    segments = [
        Segment(ROOM, 0.0, 1.0, "Morning all.", speaker="room-1"),
        Segment(ROOM, 1.2, 2.0, "Still me.", speaker="room-1"),
        Segment(ROOM, 2.2, 3.0, "Marina here.", speaker="room-2"),
        Segment(ROOM, 3.2, 4.0, "And me.", speaker="room-3"),
    ]
    turns = merge_segments(segments, coalesce_gap_s=2.0)

    assert [t.speaker for t in turns] == ["room-1", "room-2", "room-3"]
    assert turns[0].text == "Morning all. Still me."


def test_undiarized_segments_merge_exactly_as_before():
    """No speaker keys must mean byte-identical behaviour to the old merge."""
    segments = [
        Segment(SYS, 0.0, 1.0, "One."),
        Segment(SYS, 1.5, 2.0, "Two."),
        Segment(MIC, 2.2, 3.0, "Three."),
    ]
    turns = merge_segments(segments, coalesce_gap_s=2.0)
    assert [t.channel for t in turns] == [SYS, MIC]
    assert turns[0].text == "One. Two."
    assert all(t.speaker is None for t in turns)


def test_turn_voice_falls_back_to_channel():
    assert Turn(SYS, 0.0, 1.0, "x").voice == SYS
    assert Turn(ROOM, 0.0, 1.0, "x", speaker="room-2").voice == "room-2"


def test_split_long_turns_keeps_the_speaker():
    """A split monologue must not lose track of whose it was."""
    long_text = " ".join(f"Sentence number {i}." for i in range(60))
    turns = split_long_turns(
        [Turn(ROOM, 0.0, 60.0, long_text, speaker="room-2")], max_chars=120
    )
    assert len(turns) > 1
    assert all(t.speaker == "room-2" for t in turns)


# ---------------------------------------------------------------------------
# labelling -- constraint 5
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(root=tmp_path / "archive")


def test_label_precedence(cfg):
    # A name Jacob confirmed wins.
    assert cfg.speaker_label(ROOM, "room-2", {"room-2": "Marina"}) == "Marina"
    # Otherwise a distinguished-but-unnamed voice is numbered, never named.
    assert cfg.speaker_label(ROOM, "room-2", {}) == "Speaker 2"
    # With no speaker at all it is the old channel label.
    assert cfg.speaker_label(ROOM, None, {}) == cfg.room_label
    assert cfg.speaker_label(SYS, None, {}) == cfg.other_label
    assert cfg.speaker_label(MIC, None, {}) == cfg.own_label


def test_blank_roster_entries_do_not_name_a_voice(cfg):
    """An empty or whitespace name must not read as an identification."""
    assert cfg.speaker_label(ROOM, "room-1", {"room-1": ""}) == "Speaker 1"
    assert cfg.speaker_label(ROOM, "room-1", {"room-1": "   "}) == "Speaker 1"
    assert cfg.speaker_label(ROOM, "room-1", None) == "Speaker 1"


def test_numbered_mic_voice_stays_jacob(cfg):
    """The mic is one person; a numbered mic key would be an upstream bug.

    Labelling it "Speaker 1" would be worse than the known truth.
    """
    assert cfg.speaker_label(MIC, "mic-1", {}) == cfg.own_label


def test_turn_labeller_marks_itself(cfg):
    labeller = cfg.turn_labeller({"room-1": "Ana"})
    assert labeller.takes_turn is True
    assert labeller(Turn(ROOM, 0.0, 1.0, "hi", speaker="room-1")) == "Ana"
    assert labeller(Turn(ROOM, 0.0, 1.0, "hi", speaker="room-9")) == "Speaker 9"


# ---------------------------------------------------------------------------
# quips
# ---------------------------------------------------------------------------

def test_filler_and_mechanics_score_zero():
    assert score_sentence("Yeah, sure, sounds good.") == 0.0
    assert score_sentence("Can you hear me now?") == 0.0
    assert score_sentence("Sorry, you're on mute.") == 0.0
    assert score_sentence("Ok.") == 0.0


def test_distinctive_sentence_beats_filler():
    distinctive = "We just finished renovating the kitchen in Lisbon."
    assert score_sentence(distinctive, position=1) > score_sentence(
        "That sounds fine to me then.", position=1
    )


def test_pick_quip_chooses_the_recognisable_line():
    turns = [
        Turn(ROOM, 0.0, 2.0, "Hey everyone. Yeah, sure.", speaker="room-1"),
        Turn(
            ROOM,
            3.0,
            9.0,
            "We finally finished the kitchen renovation last Tuesday. Okay.",
            speaker="room-1",
        ),
    ]
    quip = pick_quip(turns)
    assert "kitchen renovation" in quip


def test_pick_quip_returns_empty_rather_than_misleading():
    """All-filler input must produce no quip at all."""
    turns = [
        Turn(ROOM, 0.0, 1.0, "Yeah.", speaker="room-1"),
        Turn(ROOM, 2.0, 3.0, "Sure, sounds good.", speaker="room-1"),
        Turn(ROOM, 4.0, 5.0, "You're on mute.", speaker="room-1"),
    ]
    assert pick_quip(turns) == ""


def test_trim_is_word_safe():
    assert trim("short enough", 40) == "short enough"
    out = trim("a" * 10 + " " + "b" * 200, 30)
    assert out.endswith("…")
    assert len(out) <= 31


def test_trim_collapses_whitespace():
    """Quips go into tab-separated output, so tabs and newlines must not survive."""
    assert trim("one\ttwo\nthree   four") == "one two three four"


def test_profiles_are_ordered_by_first_appearance():
    turns = [
        Turn(ROOM, 10.0, 14.0, "I drove up from Brighton this morning.", speaker="room-2"),
        Turn(ROOM, 0.0, 6.0, "I look after the billing integrations here.", speaker="room-1"),
        Turn(ROOM, 20.0, 22.0, "Yeah.", speaker="room-2"),
        Turn(MIC, 7.0, 8.0, "Undiarized, ignored.", speaker=None),
    ]
    profiles = profile_speakers(turns, guesses={"room-1": "Ana"})

    assert [p.speaker for p in profiles] == ["room-1", "mic", "room-2"]
    assert profiles[0].guess == "Ana"
    assert profiles[2].turns == 2
    assert profiles[2].speaking_s == pytest.approx(6.0)
    assert "Brighton" in profiles[2].quip


def test_a_headphone_call_profiles_both_sides():
    """The microphone and the computer audio are two speakers, not one."""
    turns = [
        Turn(SYS, 0.0, 4.0, "pixels to hide the camera. That is so clean.", speaker="sys-1"),
        Turn(MIC, 1.0, 8.0, "This is Headphone Test 3, and this is Jacob."),
        Turn(SYS, 4.0, 7.0, "The indicator has become very easy to read.", speaker="sys-1"),
    ]
    profiles = profile_speakers(turns)
    assert [p.speaker for p in profiles] == ["sys-1", "mic"]
    assert profiles[0].channel == SYS
    assert profiles[1].channel == MIC
    assert "Jacob" in profiles[1].quip
    # Numbering stays with the diarized voice, so an old "Speaker 1" summary
    # still names the computer audio after the microphone is listed too.
    assert speaker_display_indices(profiles) == {"sys-1": 1}


def _pcm(path: Path, pieces: list[tuple[float, int]], rate: int = 16000) -> None:
    """Mono 16-bit wav. Each piece is (seconds, sample value)."""
    import array
    import wave

    frames = array.array("h")
    for seconds, sample in pieces:
        frames.extend([sample] * int(seconds * rate))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames.tobytes())


def test_speaking_time_ignores_the_quiet_around_the_words(tmp_path):
    """A 12s Whisper span around a short sentence is not 12s of speech.

    The microphone idles well above digital silence, so the quiet has to be
    judged against this recording rather than a fixed loudness.
    """
    wav = tmp_path / "mic.wav"
    _pcm(wav, [(2.0, 300), (0.4, 4000), (2.0, 300)])
    measured = spoken_seconds(wav, [(0.0, 4.4)])
    assert measured == pytest.approx(0.4, abs=0.06)

    turns = [Turn(MIC, 0.0, 4.4, "This is Jacob.")]
    profiles = profile_speakers(turns, audio={"mic": wav})
    assert profiles[0].speaking_s == pytest.approx(0.4, abs=0.06)
    assert profile_speakers(turns)[0].speaking_s == pytest.approx(4.4)


def test_a_turn_that_is_all_speech_keeps_its_span(tmp_path):
    wav = tmp_path / "sys.wav"
    _pcm(wav, [(1.5, 5000)])
    assert spoken_seconds(wav, [(0.0, 1.5)]) == pytest.approx(1.5, abs=0.04)


# ---------------------------------------------------------------------------
# name -> voice inference
# ---------------------------------------------------------------------------

def test_self_introduction_names_that_voice():
    turns = [
        Turn(ROOM, 0.0, 3.0, "Hi, I'm Marina, I run the data team.", speaker="room-1"),
        Turn(ROOM, 4.0, 6.0, "Good to meet you.", speaker="room-2"),
    ]
    guesses = infer_speaker_names(turns, own_names=["Jacob"], own_label="Jacob")
    assert speaker_guess_map(guesses) == {"room-1": "Marina"}
    assert guesses[0].basis == "self"


def test_self_introduction_ignores_location_adjectives():
    turns = [
        Turn(ROOM, 0.0, 3.0, "Yeah, yeah, I'm Sydney-based.", speaker="room-1"),
        Turn(ROOM, 4.0, 6.0, "I'm Marina.", speaker="room-2"),
    ]
    guesses = infer_speaker_names(turns, own_names=["Jacob"], own_label="Jacob")
    assert speaker_guess_map(guesses) == {"room-2": "Marina"}


def test_handoff_names_the_next_voice():
    """Jacob's own example: he says the name, John's voice gets it.

    "I'm gonna pass to John" is spoken on the mic channel, which is not
    diarized, and John answers on the system channel. The name has to cross
    channels to land.
    """
    turns = [
        Turn(MIC, 0.0, 4.0, "I'm Jacob. I'm gonna pass to John.", speaker=None),
        Turn(SYS, 5.0, 9.0, "Thanks. So the numbers are up.", speaker="sys-1"),
    ]
    guesses = infer_speaker_names(turns, own_names=["Jacob"], own_label="Jacob")
    assert speaker_guess_map(guesses) == {"sys-1": "John"}
    assert guesses[0].basis == "handoff"


def test_handoff_skips_the_speaker_finishing_their_own_sentence():
    turns = [
        Turn(MIC, 0.0, 4.0, "Over to you, Marina.", speaker=None),
        Turn(MIC, 4.2, 5.0, "Just one more thing first.", speaker=None),
        Turn(SYS, 6.0, 9.0, "Right, thanks.", speaker="sys-1"),
    ]
    guesses = infer_speaker_names(turns, own_names=["Jacob"])
    assert speaker_guess_map(guesses) == {"sys-1": "Marina"}


def test_handoff_expires_after_a_long_silence():
    """A handoff is answered immediately or not at all."""
    turns = [
        Turn(MIC, 0.0, 4.0, "Over to you, Marina.", speaker=None),
        Turn(SYS, 600.0, 604.0, "Sorry, I was on mute.", speaker="sys-1"),
    ]
    assert infer_speaker_names(turns, own_names=["Jacob"]) == []


def test_handoff_to_an_undiarized_voice_is_dropped():
    """With no speaker key there is nothing to attach a name to."""
    turns = [
        Turn(MIC, 0.0, 4.0, "Over to you, Marina.", speaker=None),
        Turn(SYS, 5.0, 9.0, "Hello.", speaker=None),
    ]
    assert infer_speaker_names(turns, own_names=["Jacob"]) == []


def test_self_introduction_beats_a_handoff_for_the_same_voice():
    turns = [
        Turn(MIC, 0.0, 3.0, "Passing to John.", speaker=None),
        Turn(SYS, 4.0, 8.0, "Actually I'm Marina, John couldn't make it.", speaker="sys-1"),
    ]
    guesses = infer_speaker_names(turns, own_names=["Jacob"])
    assert speaker_guess_map(guesses) == {"sys-1": "Marina"}
    assert guesses[0].basis == "self"


def test_one_name_is_never_offered_for_two_voices():
    turns = [
        Turn(ROOM, 0.0, 3.0, "I'm Marina.", speaker="room-1"),
        Turn(ROOM, 4.0, 7.0, "Over to you, Marina.", speaker="room-1"),
        Turn(ROOM, 8.0, 11.0, "Hello.", speaker="room-2"),
    ]
    guesses = infer_speaker_names(turns, own_names=["Jacob"])
    names = [g.name for g in guesses]
    assert names.count("Marina") == 1
    # The confident self-introduction keeps it.
    assert speaker_guess_map(guesses)["room-1"] == "Marina"


def test_room_self_introduction_of_jacob_identifies_his_own_voice():
    """In a room Jacob shares the microphone, so "I'm Jacob" is informative.

    It is the only evidence available for which of the voices is his.
    """
    turns = [
        Turn(ROOM, 0.0, 3.0, "I'm Jacob, thanks for coming in.", speaker="room-1"),
        Turn(ROOM, 4.0, 7.0, "I'm Marina.", speaker="room-2"),
    ]
    guesses = infer_speaker_names(turns, own_names=["Jacob"], own_label="Jacob")
    assert speaker_guess_map(guesses) == {"room-1": "Jacob", "room-2": "Marina"}


def test_own_name_on_the_system_channel_is_ignored():
    """A remote person saying "I'm Jacob" is a different Jacob, not ours."""
    turns = [
        Turn(SYS, 0.0, 3.0, "Hi, I'm Jacob from the vendor side.", speaker="sys-1"),
    ]
    assert infer_speaker_names(turns, own_names=["Jacob"], own_label="Jacob") == []


def test_stopwords_are_not_treated_as_handoff_targets():
    turns = [
        Turn(MIC, 0.0, 3.0, "Go ahead, Everyone.", speaker=None),
        Turn(SYS, 4.0, 7.0, "Hello.", speaker="sys-1"),
    ]
    assert infer_speaker_names(turns, own_names=["Jacob"]) == []


# ---------------------------------------------------------------------------
# the roster in the notes file
# ---------------------------------------------------------------------------

def test_roster_merges_and_never_wipes(tmp_path):
    path = tmp_path / "notes.json"
    records.write_notes(path, "m1", notes="first", speakers={"room-1": "Ana"})
    records.write_notes(path, "m1", speakers={"room-2": "Ben"})

    data = records.read_notes(path)
    assert records.speaker_roster(data) == {"room-1": "Ana", "room-2": "Ben"}
    # An unrelated later write must not lose the notes text either.
    assert data["notes"] == "first"
    assert data["applied"] is False


def test_blank_roster_values_are_dropped(tmp_path):
    path = tmp_path / "notes.json"
    records.write_notes(path, "m1", speakers={"room-1": "Ana", "room-2": "   "})
    assert records.speaker_roster(records.read_notes(path)) == {"room-1": "Ana"}


def test_a_dismissed_dialog_cannot_unname_a_voice(tmp_path):
    path = tmp_path / "notes.json"
    records.write_notes(path, "m1", speakers={"room-1": "Ana"})
    records.write_notes(path, "m1", speakers={"room-1": ""})
    assert records.speaker_roster(records.read_notes(path)) == {"room-1": "Ana"}


def test_roster_tolerates_a_malformed_speakers_field():
    assert records.speaker_roster({"speakers": "not a dict"}) == {}
    assert records.speaker_roster({}) == {}
    assert records.speaker_roster(None) == {}


def test_unnamed_speakers(tmp_path):
    transcript = Transcript(meeting_id="m1")
    transcript.speaker_profiles = profile_speakers(
        [
            Turn(ROOM, 0.0, 5.0, "I handle the billing integrations.", speaker="room-1"),
            Turn(ROOM, 6.0, 9.0, "I came up from Brighton today.", speaker="room-2"),
            Turn(MIC, 1.0, 3.0, "This is Jacob."),
        ]
    )
    pending = transcript.unnamed_speakers({"room-1": "Ana"})
    assert [p.speaker for p in pending] == ["room-2"]
    assert transcript.unnamed_speakers({"room-1": "Ana", "room-2": "Ben"}) == []


# ---------------------------------------------------------------------------
# transcript round-trip
# ---------------------------------------------------------------------------

def test_transcript_roundtrips_diarization(tmp_path):
    turns = [
        Turn(ROOM, 0.0, 4.0, "I'm Marina and I run the data team.", speaker="room-1"),
        Turn(ROOM, 5.0, 8.0, "I drove in from Brighton.", speaker="room-2"),
    ]
    guesses = infer_speaker_names(turns, own_names=["Jacob"])
    original = Transcript(
        meeting_id="2026-09-11T10-00-00",
        segments=[Segment(ROOM, 0.0, 4.0, "I'm Marina.", speaker="room-1")],
        turns=turns,
        speaker_spans=[SpeakerSpan(ROOM, 0.0, 4.0, "room-1")],
        speaker_profiles=profile_speakers(turns, speaker_guess_map(guesses)),
        speaker_guesses=guesses,
        diarize_model="sherpa-onnx-pyannote-segmentation-3.0",
    )
    path = tmp_path / "t.json"
    original.write(path)
    back = Transcript.read(path)

    assert [t.speaker for t in back.turns] == ["room-1", "room-2"]
    assert [s.speaker for s in back.segments] == ["room-1"]
    assert back.speaker_spans[0].speaker == "room-1"
    assert back.diarize_model == "sherpa-onnx-pyannote-segmentation-3.0"
    assert [p.speaker for p in back.speaker_profiles] == ["room-1", "room-2"]
    assert back.speaker_guesses[0].name == "Marina"


def test_undiarized_transcript_omits_the_speaker_keys(tmp_path):
    """An undiarized transcript must stay exactly as small as it always was."""
    transcript = Transcript(
        meeting_id="m1",
        segments=[Segment(SYS, 0.0, 1.0, "Hello.")],
        turns=[Turn(SYS, 0.0, 1.0, "Hello.")],
    )
    payload = transcript.to_dict()
    assert "speaker" not in payload["segments"][0]
    assert "speaker" not in payload["turns"][0]
    for key in ("speaker_spans", "speaker_profiles", "speaker_guesses",
                "diarize_model", "diarize_error"):
        assert key not in payload


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_mic_is_not_in_the_default_diarize_channels():
    """Diarizing the mic could only ever invent a second Jacob."""
    assert MIC not in DiarizeConfig().diarize_channels
    assert SYS in DiarizeConfig().diarize_channels
    assert ROOM in DiarizeConfig().diarize_channels


def test_diarize_config_loads_from_toml(tmp_path):
    from meeting_archive.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[audio]",
                'device_name = "Meeting Capture"',
                "[diarize]",
                "enabled = false",
                'model = "sherpa-test"',
                'segmentation_model = "/models/segment.onnx"',
                'embedding_model = "/models/embed.onnx"',
                "num_threads = 3",
                "cluster_threshold = 0.65",
                "max_speakers = 4",
                "min_span_s = 1.25",
                'diarize_channels = ["room"]',
                "identify_popup = false",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.audio.device_name == "Meeting Capture"
    assert cfg.diarize.enabled is False
    assert cfg.diarize.model == "sherpa-test"
    assert cfg.diarize.segmentation_model == "/models/segment.onnx"
    assert cfg.diarize.embedding_model == "/models/embed.onnx"
    assert cfg.diarize.num_threads == 3
    assert cfg.diarize.cluster_threshold == 0.65
    assert cfg.diarize.max_speakers == 4
    assert cfg.diarize.min_span_s == 1.25
    assert cfg.diarize.diarize_channels == ["room"]
    assert cfg.diarize.identify_popup is False


def test_room_speaker_count_comes_from_every_listed_voice():
    notes = {"attendees": "Jacob, Bert, Teddy, Leanne"}
    assert records.expected_diarized_speakers(
        notes, {ROOM}, own_names=["Jacob", "Jake"]
    ) == 4


def test_call_speaker_count_excludes_the_known_mic_voice():
    notes = {"attendees": "Jacob, Bert, Teddy"}
    assert records.expected_diarized_speakers(
        notes, {SYS}, own_names=["Jacob", "Jake"]
    ) == 2


def test_blank_speaker_list_keeps_unlimited_automatic_clustering():
    assert records.expected_diarized_speakers(
        {"attendees": ""}, {ROOM}, own_names=["Jacob"]
    ) is None


def test_listed_speakers_accepts_commas_and_newlines():
    assert records.listed_speakers(
        {"attendees": "Jacob, Bert\nTeddy,  Leanne "}
    ) == ["Jacob", "Bert", "Teddy", "Leanne"]


def test_a_split_that_finds_a_second_person_replaces_one_merged_voice():
    merged = [SpeakerSpan(SYS, 0.0, 34.0, "sys-1")]
    split = [
        SpeakerSpan(SYS, 0.0, 22.0, "sys-1"),
        SpeakerSpan(SYS, 4.0, 20.0, "sys-2"),
    ]
    assert choose_richer_split(merged, split) is split


def test_a_second_pass_that_only_adds_a_blip_keeps_the_first():
    first = [SpeakerSpan(SYS, 0.0, 30.0, "sys-1")]
    blip = [
        SpeakerSpan(SYS, 0.0, 30.0, "sys-1"),
        SpeakerSpan(SYS, 30.0, 30.4, "sys-2"),
    ]
    assert choose_richer_split(first, blip) is first
