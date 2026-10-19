"""Split a merged transcript into chunks for embedding.

Timestamps deliberately do NOT go into the embedded text -- they are noise to
an embedding model and would dilute every chunk. They live in chunk metadata
instead, so a search hit can still point at the right minute of the meeting.

The optional context header is the opposite trade: it adds a little noise for a
large gain in recall, because it puts the date and the attendee names inside
the text being embedded. Without it, "what did John say about pricing" can only
match on "pricing".
"""

from __future__ import annotations

from collections.abc import Callable

from .models import Chunk, Turn


def chunk_turns(
    turns: list[Turn],
    target_chars: int = 1200,
    overlap_turns: int = 1,
    label_fn: Callable[..., str] | None = None,
    header: str = "",
) -> list[Chunk]:
    """Pack turns into chunks of roughly target_chars, overlapping by N turns.

    Chunk boundaries always fall between turns, so a chunk never cuts a speaker
    off mid-sentence.

    `label_fn` may take a Turn (the diarization-aware form, marked with
    `takes_turn`) or a channel string. Whichever it is, the label that goes
    into the embedded text is the same one recorded in `Chunk.speakers`, so a
    search filtered on a speaker can never disagree with the text it returns.
    """
    if not turns:
        return []

    if label_fn is None:
        label = lambda turn: turn.channel  # noqa: E731 - trivial default
    elif getattr(label_fn, "takes_turn", False):
        label = label_fn
    else:
        label = lambda turn: label_fn(turn.channel)  # noqa: E731

    labels = [label(t) for t in turns]
    lines = [f"{labels[i]}: {t.text}" for i, t in enumerate(turns)]

    chunks: list[Chunk] = []
    total = len(turns)
    start_i = 0
    index = 0

    while start_i < total:
        end_i = start_i
        size = 0
        # Always take at least one turn, even if it alone exceeds the target.
        while end_i < total and (size == 0 or size + len(lines[end_i]) + 1 <= target_chars):
            size += len(lines[end_i]) + 1
            end_i += 1

        window = turns[start_i:end_i]
        body = "\n".join(lines[start_i:end_i])
        text = f"{header}\n\n{body}" if header else body

        chunks.append(
            Chunk(
                index=index,
                start=window[0].start,
                end=window[-1].end,
                channels=sorted({t.channel for t in window}),
                text=text,
                speakers=sorted(set(labels[start_i:end_i])),
            )
        )
        index += 1

        if end_i >= total:
            break
        # Step back by the overlap, but always make forward progress.
        start_i = max(end_i - overlap_turns, start_i + 1)

    return chunks


def context_header(
    date_label: str,
    attendees: str = "",
    detected_names: str = "",
    notes: str = "",
    identified: str = "",
) -> str:
    """One compact line of meeting context, prepended to each chunk."""
    parts = [f"Meeting {date_label}"]
    people = attendees or detected_names
    if people:
        parts.append(f"Present: {people}")
    if identified:
        parts.append(f"Speakers: {identified}")
    if notes:
        parts.append(f"Notes: {notes}")
    return " | ".join(parts)
