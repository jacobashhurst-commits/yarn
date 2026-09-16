"""Conservative ASR cleanup. Proofreading, not rewriting."""

from __future__ import annotations

import re

from .config import SummaryConfig
from .models import Turn
from .summarize import Summarizer, SummaryUnavailable


CLEANUP_PROMPT = """You proofread an automatic speech-recognition transcript.
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
If unsure, leave the original wording.
Return the cleaned transcript only, same number of turns, each line:
[HH:MM:SS] SPEAKER: text

TRANSCRIPT
"""


def cleanup_turns(turns: list[Turn], label_fn, summarizer: Summarizer) -> list[Turn]:
    """Return cleaned copies, or raise SummaryUnavailable and leave raw intact."""
    if not turns:
        return []
    if not summarizer.enabled or not summarizer.is_running():
        raise SummaryUnavailable("cleanup skipped: Ollama is not running")
    from .models import format_timestamp

    body = "\n".join(
        f"[{format_timestamp(turn.start)}] {label_fn(turn)}: {turn.text}"
        for turn in turns
    )
    text = summarizer._run(CLEANUP_PROMPT + body)
    parsed = _parse_cleaned(text, turns)
    if not parsed:
        raise SummaryUnavailable("cleanup returned nothing usable; raw transcript kept")
    return parsed


def _parse_cleaned(text: str, original: list[Turn]) -> list[Turn]:
    """Map cleaned lines back onto original turns by order, not by invention."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    usable = []
    for line in lines:
        stripped = re.sub(r"^\[.*?\]\s*", "", line)
        if ":" in stripped:
            usable.append(stripped)
    if len(usable) < max(1, len(original) // 3):
        return []
    out: list[Turn] = []
    for index, turn in enumerate(original):
        if index >= len(usable):
            out.append(turn)
            continue
        line = usable[index]
        _, _, body = line.partition(":")
        cleaned = body.strip() or turn.text
        out.append(
            Turn(
                channel=turn.channel,
                start=turn.start,
                end=turn.end,
                text=cleaned,
                speaker=turn.speaker,
            )
        )
    return out
