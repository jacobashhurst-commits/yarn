"""Conservative ASR cleanup. Proofreading, not rewriting.

This runs on every meeting by default, so it is built to be unable to do
harm. The model only ever returns text for a numbered line. Who said the
line, and when, never come from the model: they stay exactly as recorded.
Each returned line is then checked against the original, and a line that
is missing, duplicated, or no longer says what the speaker said is thrown
away in favour of the original. The untouched turns are also kept beside
the cleaned ones in the transcript.

The earlier version sent the whole meeting as one prompt and matched the
answer to turns by position. One merged or dropped line shifted every line
after it onto the wrong speaker, and a long meeting was one prompt too big
for an 8 GB Mac.
"""

from __future__ import annotations

import re

from .models import Turn
from .summarize import Summarizer, SummaryUnavailable


CLEANUP_PROMPT = """You proofread an automatic speech-recognition transcript.
Each line is: NUMBER| SPEAKER: text
Fix only:
- punctuation and sentence boundaries
- obvious repeated-phrase loops
- clear ASR mishearings of names/terms when the intended word is unambiguous
Do NOT:
- paraphrase anyone
- invent dialogue or facts
- summarise
- remove disagreement or hedging
- add names that were not said
- merge, split, reorder, add or drop lines
If unsure, leave the original wording.
Return every line, one per line, as: NUMBER| corrected text
Do not repeat the speaker. Do not add anything else.

TRANSCRIPT
"""

# The model writes about as much as it reads, so a cleanup prompt has to be
# roughly half the size a summary prompt can be.
_CHUNK_CHARS = 5000

_LINE = re.compile(r"^\s*(\d+)\s*\|\s*(.*\S)\s*$")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def cleanup_turns(turns: list[Turn], label_fn, summarizer: Summarizer) -> list[Turn]:
    """Return cleaned copies, or raise SummaryUnavailable and leave raw intact."""
    if not turns:
        return []
    if not summarizer.enabled or not summarizer.is_running():
        raise SummaryUnavailable("cleanup skipped: Ollama is not running")

    out: list[Turn] = []
    changed = 0
    for chunk in _chunks(turns, _CHUNK_CHARS):
        body = "\n".join(
            f"{number}| {label_fn(turn)}: {turn.text}"
            for number, turn in enumerate(chunk, start=1)
        )
        answer = summarizer._run(CLEANUP_PROMPT + body)
        cleaned = _parse_numbered(answer, chunk)
        changed += sum(1 for old, new in zip(chunk, cleaned) if old.text != new.text)
        out.extend(cleaned)
    if not changed:
        raise SummaryUnavailable("cleanup changed nothing; raw transcript kept")
    return out


def _chunks(turns: list[Turn], limit: int) -> list[list[Turn]]:
    chunks: list[list[Turn]] = []
    current: list[Turn] = []
    size = 0
    for turn in turns:
        cost = len(turn.text) + 24
        if current and size + cost > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(turn)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def _parse_numbered(text: str, original: list[Turn]) -> list[Turn]:
    """Cleaned copies of `original`, line by line, each one checked.

    A number that appears twice, or not at all, leaves that turn as it was.
    So does a line that fails `_same_words`. Nothing here can move text
    from one turn to another.
    """
    seen: dict[int, str] = {}
    twice: set[int] = set()
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if number in seen:
            twice.add(number)
        seen[number] = match.group(2)

    out: list[Turn] = []
    for number, turn in enumerate(original, start=1):
        candidate = seen.get(number, "")
        if number in twice or not candidate:
            out.append(turn)
            continue
        candidate = _without_speaker(candidate)
        if not _same_words(turn.text, candidate):
            out.append(turn)
            continue
        out.append(
            Turn(
                channel=turn.channel,
                start=turn.start,
                end=turn.end,
                text=candidate,
                speaker=turn.speaker,
            )
        )
    return out


def _without_speaker(text: str) -> str:
    """Drop a "Speaker 2:" the model echoed back despite being told not to."""
    head, sep, rest = text.partition(":")
    if sep and rest.strip() and len(head) <= 40 and len(head.split()) <= 4:
        if not any(ch in head for ch in ".,?!"):
            return rest.strip()
    return text


def _same_words(original: str, cleaned: str) -> bool:
    """Whether `cleaned` is still what was said, give or take proofreading.

    Proofreading changes punctuation, capitals and the odd misheard word.
    It does not change how much was said or most of the words. Anything
    outside that is a rewrite, and a rewrite is refused.
    """
    before = [w.casefold() for w in _WORD.findall(original)]
    after = [w.casefold() for w in _WORD.findall(cleaned)]
    if not before:
        return not after
    if not after:
        return False
    # A repeated-phrase loop may be collapsed, so shorter is allowed further
    # than longer. Longer means the model added words nobody said.
    if len(after) > len(before) + max(2, len(before) // 5):
        return False
    if len(after) < len(before) * 0.5:
        return False
    kept = set(before) & set(after)
    if len(kept) < len(set(after)) * 0.7:
        return False
    return len(kept) >= len(set(before)) * 0.6


def _parse_cleaned(text: str, original: list[Turn]) -> list[Turn]:
    """Kept for callers of the old name. Same checks as `_parse_numbered`."""
    return _parse_numbered(text, original)
