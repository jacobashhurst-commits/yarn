"""Tests for the pure logic: merging, chunking, name detection.

These are the parts that can be verified without a microphone, a Whisper model
or a vector store, which makes them the parts worth testing hardest.
"""

from __future__ import annotations

import pytest

from meeting_archive.chunking import chunk_turns, context_header
from meeting_archive.merge import merge_segments, split_long_turns
from meeting_archive.models import MIC, ROOM, SYS, Segment, Turn
from meeting_archive.names import attendee_suggestion, detect_names


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------

def seg(channel, start, end, text):
    return Segment(channel=channel, start=start, end=end, text=text)


def test_merge_interleaves_by_start_time():
    segments = [
        seg(SYS, 5.0, 7.0, "Good, thanks."),
        seg(MIC, 0.0, 2.0, "How are you?"),
        seg(MIC, 8.0, 9.0, "Great."),
    ]
    turns = merge_segments(segments)
    assert [t.channel for t in turns] == [MIC, SYS, MIC]
    assert turns[0].text == "How are you?"


def test_merge_coalesces_same_speaker_within_gap():
    segments = [
        seg(MIC, 0.0, 2.0, "So the first thing"),
        seg(MIC, 2.5, 4.0, "is the pricing model."),
    ]
    turns = merge_segments(segments, coalesce_gap_s=2.0)
    assert len(turns) == 1
    assert turns[0].text == "So the first thing is the pricing model."
    assert turns[0].start == 0.0
    assert turns[0].end == 4.0


def test_merge_breaks_same_speaker_on_long_gap():
    segments = [
        seg(MIC, 0.0, 2.0, "First point."),
        seg(MIC, 30.0, 32.0, "Different point entirely."),
    ]
    turns = merge_segments(segments, coalesce_gap_s=2.0)
    assert len(turns) == 2


def test_merge_breaks_when_turn_would_exceed_max_duration():
    segments = [seg(MIC, float(i) * 2, float(i) * 2 + 1.9, f"part {i}") for i in range(20)]
    turns = merge_segments(segments, coalesce_gap_s=2.0, max_turn_s=10.0)
    assert len(turns) > 1
    for turn in turns:
        assert turn.end - turn.start <= 10.0


def test_merge_does_not_coalesce_across_an_interjection():
    segments = [
        seg(MIC, 0.0, 1.0, "So I was thinking"),
        seg(SYS, 1.2, 1.6, "Mm."),
        seg(MIC, 1.8, 3.0, "that we should wait."),
    ]
    turns = merge_segments(segments)
    assert [t.channel for t in turns] == [MIC, SYS, MIC]


def test_merge_drops_empty_and_whitespace_segments():
    segments = [seg(MIC, 0.0, 1.0, "   "), seg(MIC, 2.0, 3.0, ""), seg(SYS, 4.0, 5.0, "Hi")]
    turns = merge_segments(segments)
    assert len(turns) == 1
    assert turns[0].channel == SYS


def test_merge_empty_input():
    assert merge_segments([]) == []


def test_merge_is_deterministic_on_identical_start_times():
    a = [seg(SYS, 1.0, 2.0, "sys"), seg(MIC, 1.0, 2.0, "mic")]
    b = [seg(MIC, 1.0, 2.0, "mic"), seg(SYS, 1.0, 2.0, "sys")]
    assert [t.channel for t in merge_segments(a)] == [t.channel for t in merge_segments(b)]


# --------------------------------------------------------------------------
# split_long_turns
# --------------------------------------------------------------------------

def test_split_long_turn_on_sentence_boundaries():
    text = " ".join(["This is a sentence about pricing."] * 40)
    turns = [Turn(channel=MIC, start=0.0, end=120.0, text=text)]
    out = split_long_turns(turns, max_chars=200)
    assert len(out) > 1
    assert all(len(t.text) <= 200 for t in out)
    # Timestamps must stay ordered, contiguous and inside the original span.
    assert out[0].start == 0.0
    assert out[-1].end == 120.0
    for earlier, later in zip(out, out[1:]):
        assert earlier.end <= later.start + 1e-6
        assert earlier.start <= earlier.end


def test_split_long_turns_leaves_short_turns_alone():
    turns = [Turn(channel=MIC, start=0.0, end=5.0, text="Short.")]
    assert split_long_turns(turns, max_chars=200) == turns


def test_split_handles_one_enormous_sentence_without_punctuation():
    text = "word " * 500
    turns = [Turn(channel=SYS, start=0.0, end=60.0, text=text.strip())]
    out = split_long_turns(turns, max_chars=100)
    assert len(out) > 1
    assert all(len(t.text) <= 100 for t in out)


def test_split_preserves_zero_duration_turn():
    turns = [Turn(channel=MIC, start=4.0, end=4.0, text="a. " * 200)]
    out = split_long_turns(turns, max_chars=100)
    assert all(t.start == 4.0 and t.end == 4.0 for t in out)


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------

def label(channel):
    return "Jacob" if channel == MIC else "Other"


def make_turns(count, text="Some words about the quarterly numbers."):
    return [
        Turn(channel=MIC if i % 2 == 0 else SYS, start=float(i), end=float(i) + 0.9, text=text)
        for i in range(count)
    ]


def test_chunking_respects_target_size():
    chunks = chunk_turns(make_turns(30), target_chars=200, overlap_turns=0, label_fn=label)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert len(chunk.text) <= 200 + 60  # allow one line of slack


def test_chunking_overlaps_by_requested_turns():
    turns = [
        Turn(channel=MIC, start=float(i), end=float(i) + 1, text=f"line {i}") for i in range(10)
    ]
    chunks = chunk_turns(turns, target_chars=30, overlap_turns=1, label_fn=label)
    assert len(chunks) > 1
    # The last line of one chunk should reappear in the next.
    first_last_line = chunks[0].text.splitlines()[-1]
    assert first_last_line in chunks[1].text


def test_chunking_always_progresses_with_large_overlap():
    turns = [Turn(channel=MIC, start=float(i), end=float(i) + 1, text="x" * 50) for i in range(12)]
    chunks = chunk_turns(turns, target_chars=40, overlap_turns=99, label_fn=label)
    # A too-large overlap must not loop forever or emit a chunk per character.
    assert 1 < len(chunks) <= 12
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunking_single_oversized_turn_still_emits_one_chunk():
    turns = [Turn(channel=MIC, start=0.0, end=1.0, text="y" * 5000)]
    chunks = chunk_turns(turns, target_chars=100, overlap_turns=1, label_fn=label)
    assert len(chunks) == 1


def test_chunking_records_span_and_channels():
    turns = make_turns(4)
    chunks = chunk_turns(turns, target_chars=10_000, overlap_turns=0, label_fn=label)
    assert len(chunks) == 1
    assert chunks[0].start == turns[0].start
    assert chunks[0].end == turns[-1].end
    assert set(chunks[0].channels) == {MIC, SYS}


def test_chunking_empty():
    assert chunk_turns([], label_fn=label) == []


def test_header_is_prepended_to_every_chunk():
    header = context_header("2026-09-11 14:03", attendees="John, Marina")
    chunks = chunk_turns(make_turns(20), target_chars=150, label_fn=label, header=header)
    assert len(chunks) > 1
    assert all(c.text.startswith(header) for c in chunks)


def test_header_omits_empty_fields():
    assert context_header("2026-09-11 14:03") == "Meeting 2026-09-11 14:03"
    header = context_header("2026-09-11 14:03", notes="pricing pushback")
    assert "Present" not in header
    assert "pricing pushback" in header


def test_header_prefers_manual_attendees_over_detected():
    header = context_header("d", attendees="John", detected_names="Marina")
    assert "John" in header and "Marina" not in header


# --------------------------------------------------------------------------
# name detection
# --------------------------------------------------------------------------

def t(channel, text):
    return Turn(channel=channel, start=0.0, end=1.0, text=text)


def names_of(turns, own=("Jacob",)):
    return [c.name for c in detect_names(turns, own_names=list(own)) if c.attributed_to == "other"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hey John, good to see you.", "John"),
        ("Hi Marina, thanks for joining.", "Marina"),
        ("Thanks Priya, that's helpful.", "Priya"),
        ("Thank you Sam for the update.", "Sam"),
        ("What do you think, Dave?", "Dave"),
        ("Nikhil, can you share your screen?", "Nikhil"),
        ("So, Aisha, what do you reckon?", "Aisha"),
        ("Over to you Tom.", "Tom"),
        ("This is Wei from the platform team.", "Wei"),
        ("Morning Chloe.", "Chloe"),
    ],
)
def test_detects_names_from_jacobs_side(text, expected):
    assert names_of([t(MIC, text)]) == [expected]


def test_detects_self_introduction_on_the_other_channel():
    turns = [t(SYS, "Hi, I'm Marina and I look after billing.")]
    assert names_of(turns) == ["Marina"]


def test_name_here_pattern():
    assert names_of([t(SYS, "Ravi here, sorry I'm late.")]) == ["Ravi"]


def test_jacobs_own_self_introduction_is_not_an_attendee():
    turns = [t(MIC, "Hi everyone, I'm Jacob and I'll run through the numbers.")]
    assert names_of(turns) == []


def test_own_name_spoken_by_the_other_side_is_excluded():
    turns = [t(SYS, "Thanks Jacob, that makes sense.")]
    assert names_of(turns) == []


def test_own_name_aliases_are_excluded():
    turns = [t(SYS, "Hey Jake, quick question.")]
    assert names_of(turns, own=("Jacob", "Jake")) == []


def test_self_intro_on_mic_channel_is_attributed_to_own_even_if_not_configured():
    # Jacob saying "I'm Jacoby" on his own mic is him, whatever the config says.
    candidates = detect_names([t(MIC, "I'm Jacoby by the way.")], own_names=["Jacob"])
    assert [c.attributed_to for c in candidates] == ["own"]


def test_counts_accumulate_across_turns_and_rank_results():
    turns = [
        t(MIC, "Hey John, welcome."),
        t(MIC, "John, can you take this one?"),
        t(MIC, "Thanks Marina."),
    ]
    candidates = [c for c in detect_names(turns, own_names=["Jacob"]) if c.attributed_to == "other"]
    assert [c.name for c in candidates] == ["John", "Marina"]
    assert candidates[0].count == 2


def test_a_name_is_counted_once_per_turn():
    turns = [t(MIC, "Hey John, John, can you hear me, John?")]
    candidates = detect_names(turns, own_names=["Jacob"])
    assert candidates[0].count == 1


@pytest.mark.parametrize(
    "text",
    [
        "Yeah, that works for me.",
        "Okay, so the next thing is the invoice.",
        "Sorry, could you repeat that?",
        "Right, let's move on.",
        "Hey everyone, thanks for coming.",
        "Hi team, quick one today.",
        "Thanks guys.",
        "Well, that's the whole problem.",
        "Hey Google, what's the weather.",
        "I spoke to Legal, Finance, and Procurement about it.",
        "We reviewed Sales, Marketing, and Support.",
        "This is great, thanks.",
        "I'm fine with that.",
        "Actually, that changes things.",
        "Monday, Tuesday, whatever suits.",
        "Perfect, let's do that.",
    ],
)
def test_does_not_hallucinate_names_from_common_speech(text):
    assert names_of([t(MIC, text)]) == []


def test_list_of_capitalised_nouns_is_not_treated_as_vocatives():
    turns = [t(MIC, "The owners are Legal, Finance, Procurement, and Risk.")]
    assert names_of(turns) == []


def test_prefixed_surnames_are_skipped_rather_than_truncated():
    # We would rather miss McKenzie than suggest "Mc".
    assert "Mc" not in names_of([t(MIC, "Hey McKenzie, welcome aboard.")])


def test_min_count_filters_one_off_mentions():
    turns = [t(MIC, "Hey John."), t(MIC, "Thanks Marina."), t(MIC, "John, over to you.")]
    candidates = detect_names(turns, own_names=["Jacob"], min_count=2)
    assert [c.name for c in candidates] == ["John"]


def test_candidate_records_evidence_and_pattern():
    candidates = detect_names([t(MIC, "Hey John, good to see you.")], own_names=["Jacob"])
    assert candidates[0].patterns == {"greeting_vocative"}
    assert "John" in candidates[0].evidence


def test_attendee_suggestion_is_a_clean_comma_list():
    turns = [t(MIC, "Hey John."), t(SYS, "I'm Marina."), t(SYS, "Thanks Jacob.")]
    candidates = detect_names(turns, own_names=["Jacob"])
    assert attendee_suggestion(candidates) == "John, Marina"


def test_same_name_via_different_patterns_is_one_candidate():
    turns = [t(MIC, "Hey John."), t(MIC, "Thanks John."), t(SYS, "I'm John, by the way.")]
    candidates = [c for c in detect_names(turns, own_names=["Jacob"]) if c.attributed_to == "other"]
    assert len(candidates) == 1
    assert candidates[0].name == "John"
    assert candidates[0].count == 3
    assert len(candidates[0].patterns) > 1


def test_all_caps_names_are_not_detected():
    # Documented limitation rather than desired behaviour: Whisper writes names
    # in Title Case, and allowing ALL CAPS would match shouted stopwords.
    assert names_of([t(MIC, "Thanks JOHN.")]) == []


@pytest.mark.parametrize(
    "channel,text",
    [
        (SYS, "I'm Australian, based in Sydney."),
        (SYS, "I'm Keen to get started."),
        (SYS, "I'm Based in Singapore these days."),
        (MIC, "Welcome Aboard, good to have you."),
        (MIC, "Over here we have the revenue split."),
        (MIC, "Nobody here has seen the contract."),
        (MIC, "This is Confidential so keep it internal."),
        (MIC, "I spoke to Legal, Finance, do you know who owns it?"),
        (MIC, "Ops, Engineering, can you two sync on this?"),
        (MIC, "Thanks Everyone, that's a wrap."),
    ],
)
def test_regression_false_positive_classes(channel, text):
    # Each of these produced a bogus suggestion before the stoplist was
    # extended. They are the realistic failure modes, not contrived ones.
    assert names_of([t(channel, text)]) == []


def test_no_turns_means_no_names():
    assert detect_names([], own_names=["Jacob"]) == []


# --------------------------------------------------------------------------
# room recordings: one microphone, several people
# --------------------------------------------------------------------------

def test_self_introduction_in_a_room_is_not_assumed_to_be_jacob():
    # The bug this mode exists to fix. On a call, "I'm Marina" on the mic
    # channel is Jacob speaking. In a room, the same microphone carries
    # everyone, so it is Marina introducing herself -- and hers is exactly the
    # name we most need to keep.
    turns = [Turn(channel=ROOM, start=0.0, end=3.0, text="Hi all, I'm Marina from billing.")]
    assert names_of(turns) == ["Marina"]


def test_room_recordings_still_exclude_jacobs_own_names():
    turns = [
        Turn(channel=ROOM, start=0.0, end=2.0, text="I'm Jacob, I'll take notes."),
        Turn(channel=ROOM, start=2.0, end=4.0, text="Thanks Jake, appreciated."),
    ]
    assert names_of(turns, own=("Jacob", "Jake")) == []


def test_room_recordings_pick_up_names_from_the_conversation():
    turns = [
        Turn(channel=ROOM, start=0.0, end=3.0, text="Hey John, grab a seat."),
        Turn(channel=ROOM, start=3.0, end=6.0, text="Priya here, sorry I'm late."),
        Turn(channel=ROOM, start=6.0, end=9.0, text="What do you think, Wei?"),
    ]
    assert sorted(names_of(turns)) == ["John", "Priya", "Wei"]


def test_room_and_call_channels_coexist_in_one_detection_pass():
    # Not a real recording shape, but proves attribution is per-turn rather
    # than a global mode flag that could be set wrong.
    turns = [
        Turn(channel=MIC, start=0.0, end=2.0, text="I'm Jacoby."),
        Turn(channel=ROOM, start=2.0, end=4.0, text="I'm Marina."),
    ]
    by_name = {c.name: c.attributed_to for c in detect_names(turns, own_names=[])}
    assert by_name["Jacoby"] == "own"
    assert by_name["Marina"] == "other"


# --------------------------------------------------------------------------
# readable markdown titles
# --------------------------------------------------------------------------

def test_meeting_title_uses_the_first_five_words_of_notes():
    from meeting_archive.records import Transcript, meeting_title

    transcript = Transcript(meeting_id="2026-09-11T16-21-31")
    title = meeting_title({"notes": "Watch show with AJ and Tony later"}, transcript)
    assert title == "Watch show with AJ and"


def test_meeting_title_falls_back_to_a_short_summary_when_notes_are_blank():
    from meeting_archive.records import Transcript, meeting_title

    transcript = Transcript(
        meeting_id="2026-09-11T16-21-31",
        summary="A speaker discusses their strong preference for a bracelet.",
        turns=[Turn(ROOM, 0, 4, "Should not be used when a summary exists.")],
    )
    assert meeting_title({"notes": ""}, transcript) == "speaker discusses their strong preference"


def test_markdown_filename_puts_the_date_first():
    from meeting_archive.records import markdown_filename

    assert markdown_filename("2026-09-11T16-21-31", "Watch show") == "2026-09-11 Watch show.md"
    assert "/" not in markdown_filename("2026-09-11T16-21-31", "a/b:c")
