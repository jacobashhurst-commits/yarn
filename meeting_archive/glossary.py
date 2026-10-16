"""Whole-word spelling fixes after Whisper and before index.

Whisper has no company dictionary. A Cursor rule does not change what is
stored in the archive or what Ask Yarn searches. These replacements run on
transcript text so markdown, Chroma, and Ollama all see the same spelling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .records import Transcript


@dataclass
class GlossaryConfig:
    # Heard-as -> correct spelling. Each key matches a whole word only.
    replacements: dict[str, str] = field(default_factory=dict)


def apply_glossary(text: str, replacements: dict[str, str]) -> str:
    if not text or not replacements:
        return text
    out = text
    for heard, correct in replacements.items():
        heard = str(heard).strip()
        correct = str(correct).strip()
        if not heard or not correct:
            continue
        out = re.sub(
            rf"\b{re.escape(heard)}\b",
            correct,
            out,
            flags=re.IGNORECASE,
        )
    return out


def apply_glossary_to_transcript(
    transcript: Transcript, replacements: dict[str, str]
) -> bool:
    """Rewrite segment, turn, and summary text in place. Returns True if anything changed."""
    if not replacements:
        return False
    changed = False
    for segment in transcript.segments:
        fixed = apply_glossary(segment.text, replacements)
        if fixed != segment.text:
            segment.text = fixed
            changed = True
    for turn in transcript.turns:
        fixed = apply_glossary(turn.text, replacements)
        if fixed != turn.text:
            turn.text = fixed
            changed = True
    for turn in transcript.raw_turns or []:
        fixed = apply_glossary(turn.text, replacements)
        if fixed != turn.text:
            turn.text = fixed
            changed = True
    if transcript.summary:
        fixed = apply_glossary(transcript.summary, replacements)
        if fixed != transcript.summary:
            transcript.summary = fixed
            changed = True
    return changed
