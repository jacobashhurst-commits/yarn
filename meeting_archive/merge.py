"""Interleave the channels into a single ordered transcript.

Every channel is transcribed independently, so each has its own segment list
with timestamps relative to the same recording start. Merging is therefore just
a sort by start time, followed by collapsing runs of the same speaker into
turns.

A run is broken by a change of *voice*, not of channel. Before diarization
those were the same thing; now they are not, and it matters: three people on
one `room` microphone all share a channel, and collapsing on the channel alone
would weld all three into a single turn with one label. `Turn.voice` is the
speaker key when there is one and the channel otherwise, which keeps
undiarized recordings behaving exactly as they did.

Simultaneous speech (two people talking at once) is ordered by who started
first. There is no way to represent true overlap in a linear transcript and
trying to would make it unreadable, so we accept the ordering.
"""

from __future__ import annotations

import re

from .models import Segment, Turn

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def merge_segments(
    segments: list[Segment],
    coalesce_gap_s: float = 2.0,
    max_turn_s: float = 90.0,
) -> list[Turn]:
    """Sort segments by time and collapse same-speaker runs into turns.

    A run is broken when the voice changes, when the gap between segments
    exceeds `coalesce_gap_s`, or when the turn would exceed `max_turn_s`.
    """
    cleaned = [s for s in segments if s.text and s.text.strip()]
    if not cleaned:
        return []

    # Sort by start, then end, then channel, then speaker, for a deterministic
    # order when two segments start at exactly the same timestamp.
    ordered = sorted(
        cleaned, key=lambda s: (s.start, s.end, s.channel, s.speaker or "")
    )

    turns: list[Turn] = []
    for seg in ordered:
        text = seg.text.strip()
        current = turns[-1] if turns else None
        seg_voice = seg.speaker or seg.channel
        if (
            current is not None
            and current.voice == seg_voice
            and current.channel == seg.channel
            and seg.start - current.end <= coalesce_gap_s
            and seg.end - current.start <= max_turn_s
        ):
            current.text = f"{current.text} {text}".strip()
            current.end = max(current.end, seg.end)
        else:
            turns.append(
                Turn(
                    channel=seg.channel,
                    start=seg.start,
                    end=max(seg.end, seg.start),
                    text=text,
                    speaker=seg.speaker,
                )
            )
    return turns


def split_long_turns(turns: list[Turn], max_chars: int = 1500) -> list[Turn]:
    """Break monologues into sub-turns so chunking stays near its target size.

    Splits on sentence boundaries and interpolates timestamps proportionally to
    character count. The timestamps are approximate by construction, which is
    fine: they exist to point Jacob at roughly the right minute of a meeting,
    not to cut audio.
    """
    out: list[Turn] = []
    for turn in turns:
        if len(turn.text) <= max_chars:
            out.append(turn)
            continue

        pieces = _group_sentences(turn.text, max_chars)
        total_chars = sum(len(p) for p in pieces) or 1
        duration = max(turn.end - turn.start, 0.0)
        cursor = turn.start
        for i, piece in enumerate(pieces):
            share = duration * (len(piece) / total_chars)
            start = cursor
            end = turn.end if i == len(pieces) - 1 else min(cursor + share, turn.end)
            out.append(
                Turn(
                    channel=turn.channel,
                    start=start,
                    end=end,
                    text=piece,
                    speaker=turn.speaker,
                )
            )
            cursor = end
    return out


def _group_sentences(text: str, max_chars: int) -> list[str]:
    """Greedily pack sentences into pieces of at most max_chars."""
    sentences = [s for s in _SENTENCE_END.split(text) if s]
    if not sentences:
        return [text]

    pieces: list[str] = []
    buf = ""
    for sentence in sentences:
        # A single sentence longer than the limit gets hard-wrapped on words.
        if len(sentence) > max_chars:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.extend(_hard_wrap(sentence, max_chars))
            continue
        candidate = f"{buf} {sentence}".strip() if buf else sentence
        if len(candidate) > max_chars and buf:
            pieces.append(buf)
            buf = sentence
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces


def _hard_wrap(text: str, max_chars: int) -> list[str]:
    words = text.split()
    pieces: list[str] = []
    buf = ""
    for word in words:
        candidate = f"{buf} {word}".strip() if buf else word
        if len(candidate) > max_chars and buf:
            pieces.append(buf)
            buf = word
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces
