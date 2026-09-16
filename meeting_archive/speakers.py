"""Local known-speaker library.

Diarisation says "these voices are different". This module may say "Speaker 2
sounds like Adam". It never writes a name without confirmation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .models import SpeakerGuess, SpeakerProfile
from .records import write_json_atomic


@dataclass
class KnownSpeaker:
    name: str
    aliases: list[str] = field(default_factory=list)
    meeting_ids: list[str] = field(default_factory=list)
    confirmation_count: int = 0
    last_seen: str = ""
    notes: str = ""
    # Mean embedding, if we were able to extract one. Omitted from UI.
    embedding: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": self.aliases,
            "meeting_ids": self.meeting_ids,
            "confirmation_count": self.confirmation_count,
            "last_seen": self.last_seen,
            "notes": self.notes,
            "embedding": self.embedding,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KnownSpeaker":
        return cls(
            name=d["name"],
            aliases=list(d.get("aliases") or []),
            meeting_ids=list(d.get("meeting_ids") or []),
            confirmation_count=int(d.get("confirmation_count") or 0),
            last_seen=d.get("last_seen", ""),
            notes=d.get("notes", ""),
            embedding=[float(x) for x in (d.get("embedding") or [])],
        )


def library_path(cfg: Config) -> Path:
    return cfg.root / "speakers" / "library.json"


def load_library(cfg: Config) -> list[KnownSpeaker]:
    path = library_path(cfg)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [KnownSpeaker.from_dict(item) for item in data.get("speakers") or []]


def save_library(cfg: Config, speakers: list[KnownSpeaker]) -> Path:
    path = library_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_json_atomic(
        path,
        {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "speakers": [s.to_dict() for s in speakers],
        },
    )


def remember_confirmation(
    cfg: Config,
    *,
    name: str,
    meeting_id: str,
    embedding: list[float] | None = None,
) -> KnownSpeaker:
    """Record that Jacob named a voice. Embeddings are averaged when present."""
    speakers = load_library(cfg)
    found = next((s for s in speakers if s.name.casefold() == name.casefold()), None)
    if found is None:
        found = KnownSpeaker(name=name)
        speakers.append(found)
    found.confirmation_count += 1
    found.last_seen = datetime.now().isoformat(timespec="seconds")
    if meeting_id not in found.meeting_ids:
        found.meeting_ids.append(meeting_id)
    if embedding:
        found.embedding = _mean_embedding(found.embedding, embedding)
    save_library(cfg, speakers)
    return found


def suggest_matches(
    profiles: list[SpeakerProfile],
    library: list[KnownSpeaker],
    embeddings: dict[str, list[float]] | None = None,
    min_confidence: float = 0.75,
) -> list[SpeakerGuess]:
    """Return voice-match guesses. Never applied silently."""
    embeddings = embeddings or {}
    guesses: list[SpeakerGuess] = []
    used_names: set[str] = set()
    for profile in profiles:
        vector = embeddings.get(profile.speaker)
        best: tuple[float, KnownSpeaker] | None = None
        if vector:
            for person in library:
                if not person.embedding or person.name in used_names:
                    continue
                score = _cosine(vector, person.embedding)
                if best is None or score > best[0]:
                    best = (score, person)
        if best and best[0] >= min_confidence:
            person = best[1]
            used_names.add(person.name)
            guesses.append(
                SpeakerGuess(
                    speaker=profile.speaker,
                    name=person.name,
                    basis="voice",
                    confidence=best[0],
                    evidence=(
                        f"Matched {person.name} based on "
                        f"{person.confirmation_count} previously confirmed "
                        f"meeting(s)."
                    ),
                )
            )
    return guesses


def _mean_embedding(existing: list[float], incoming: list[float]) -> list[float]:
    if not existing:
        return list(incoming)
    if len(existing) != len(incoming):
        return list(incoming)
    n = 1.0  # running mean with equal weight on the new sample
    return [(old + new) / 2.0 for old, new in zip(existing, incoming)]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))
