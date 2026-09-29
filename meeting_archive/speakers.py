"""Local known-speaker library.

Diarisation says "these voices are different". This module may say "Speaker 2
sounds like Adam". It never writes a name without confirmation.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import Config
from .models import SpeakerGuess, SpeakerProfile
from .records import write_json_atomic

log = logging.getLogger(__name__)


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
    # Filename of a short reference wav under speakers/clips/. That directory
    # is not pruned with meeting audio, so the identity snippet survives.
    clip: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": self.aliases,
            "meeting_ids": self.meeting_ids,
            "confirmation_count": self.confirmation_count,
            "last_seen": self.last_seen,
            "notes": self.notes,
            "embedding": self.embedding,
            "clip": self.clip,
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
            clip=str(d.get("clip") or ""),
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
    clip: str = "",
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
    if clip:
        found.clip = clip
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


def leftover_assignment(
    profiles: list[SpeakerProfile],
    roster: dict[str, str],
    attendee_names: list[str],
    own_names: list[str] | None = None,
) -> tuple[str, str] | None:
    """If one unnamed voice remains and one listed name remains, that pairing.

    This is the only automatic identity assignment: Jacob has already named
    everyone else on the list he typed. A blank attendee list still means
    nothing is assumed.
    """
    unnamed = [
        profile
        for profile in profiles
        if not str(roster.get(profile.speaker, "")).strip()
    ]
    if len(unnamed) != 1:
        return None
    used = {
        str(value).strip().casefold()
        for value in roster.values()
        if str(value).strip()
    }
    own = {name.casefold() for name in (own_names or [])}
    channels = {profile.channel for profile in profiles}
    # On a call the mic is already Jacob, so his name is not a leftover sys voice.
    drop_own = bool(channels) and "room" not in channels and "mic" not in channels
    leftover_names = []
    for name in attendee_names:
        key = name.casefold()
        if key in used:
            continue
        if drop_own and key in own:
            continue
        leftover_names.append(name)
    if len(leftover_names) != 1:
        return None
    return unnamed[0].speaker, leftover_names[0]


def meeting_banner(cfg: Config, meeting_id: str) -> str:
    """Human header so back-to-back identification dialogs are not interchangeable."""
    from .records import Transcript, date_label, meeting_title, read_notes

    lines = [date_label(meeting_id)]
    notes = read_notes(cfg.notes_json(meeting_id))
    title = ""
    path = cfg.transcript_json(meeting_id)
    if path.exists():
        try:
            transcript = Transcript.read(path)
            title = (transcript.title or "").strip() or meeting_title(notes, transcript)
        except (OSError, ValueError):
            title = ""
    if title:
        lines.append(title)
    note = str(notes.get("notes") or "").strip()
    if note:
        lines.append(f"Your notes: {note}")
    else:
        lines.append("No notes were saved for this meeting.")
    attendees = str(notes.get("attendees") or "").strip()
    if attendees:
        lines.append(f"Who was there: {attendees}")
    return "\n".join(lines)


def find_channel_audio(cfg: Config, meeting_id: str, channel: str) -> Path | None:
    name = f"{meeting_id}__{channel}.wav"
    for folder in (
        cfg.audio_archive_dir,
        cfg.processing_dir,
        cfg.inbox_dir,
        cfg.failed_dir,
    ):
        path = folder / name
        if path.is_file() and path.stat().st_size > 44:
            return path
    return None


def snippet_window(transcript, profile: SpeakerProfile) -> tuple[float, float]:
    """A short stretch of this voice that is mostly them, not a neighbour.

    Prefer the quip turn when it does not overlap someone else. Otherwise take
    the longest isolated turn. Inset a little from the edges so a 6s turn
    does not spill into the next speaker.
    """
    turns = list(getattr(transcript, "turns", []) or [])
    mine = [turn for turn in turns if turn.speaker == profile.speaker]
    others = [turn for turn in turns if turn.speaker and turn.speaker != profile.speaker]
    isolated = [turn for turn in mine if not _overlaps_other(turn, others)]
    quip = (profile.quip or "").strip()

    chosen = None
    if quip:
        for turn in isolated or mine:
            if quip in (turn.text or ""):
                chosen = turn
                break
    if chosen is None and isolated:
        chosen = max(isolated, key=lambda turn: (turn.end - turn.start, -turn.start))
    if chosen is None and mine:
        chosen = mine[0]
    if chosen is None:
        start = max(0.0, float(profile.first_at or 0.0))
        return start, 3.5
    return _inset_window(chosen)


def speaker_excerpt(transcript, profile: SpeakerProfile, max_chars: int = 240) -> str:
    """A few verbatim lines from this voice, for the identify dialog."""
    from .quips import trim

    parts: list[str] = []
    size = 0
    quip = (profile.quip or "").strip()
    for turn in getattr(transcript, "turns", []) or []:
        if turn.speaker != profile.speaker:
            continue
        text = " ".join((turn.text or "").split())
        if not text:
            continue
        parts.append(text)
        size += len(text)
        if size >= max_chars:
            break
    excerpt = trim(" ".join(parts), max_chars) if parts else ""
    if excerpt:
        return excerpt
    return trim(quip, max_chars) if quip else ""


def _overlaps_other(turn, others, min_overlap: float = 0.2) -> bool:
    for other in others:
        overlap = min(float(turn.end), float(other.end)) - max(
            float(turn.start), float(other.start)
        )
        if overlap >= min_overlap:
            return True
    return False


def _inset_window(turn, max_s: float = 3.5, min_s: float = 1.5) -> tuple[float, float]:
    length = max(0.0, float(turn.end) - float(turn.start))
    duration = max(min_s, min(max_s, length)) if length else min_s
    if length and duration > length:
        duration = length
    slack = max(0.0, length - duration)
    start = max(0.0, float(turn.start) + min(0.25, slack / 2.0))
    return start, duration


def clips_dir(cfg: Config) -> Path:
    return cfg.root / "speakers" / "clips"


def clip_slug(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name.strip())
    return cleaned.strip("-").lower() or "voice"


def write_voice_clip(
    cfg: Config,
    transcript,
    profile: SpeakerProfile,
    *,
    name: str = "",
    overwrite: bool = False,
) -> str:
    """Extract a reference wav. Empty string if the meeting audio is already gone.

    A named clip is stored as speakers/clips/{slug}.wav and is the durable
    identification snippet. The first validated file is kept unless overwrite
    is set; later meetings only refresh the embedding.
    """
    from .audio import AudioError, extract_clip

    dest_name = (
        f"{clip_slug(name)}.wav"
        if name.strip()
        else f"{transcript.meeting_id}__{profile.speaker}.wav"
    )
    dest = clips_dir(cfg) / dest_name
    if dest.exists() and dest.stat().st_size > 44 and not overwrite:
        return dest.name
    src = find_channel_audio(cfg, transcript.meeting_id, profile.channel)
    if src is None:
        return dest.name if dest.exists() else ""
    start, duration = snippet_window(transcript, profile)
    try:
        extract_clip(src, dest, start, duration)
    except (AudioError, OSError):
        return dest.name if dest.exists() else ""
    return dest.name


def capture_identity(
    cfg: Config,
    transcript,
    profile: SpeakerProfile,
    name: str,
) -> tuple[str, list[float]]:
    """Save a durable snippet and fingerprint once a voice is named.

    Meeting audio can be pruned later. The clip lives under speakers/clips/
    and the vector is averaged into library.json. The first validated snippet
    is kept; later confirmations only update the embedding.
    """
    from .audio import AudioError, extract_clip
    from .diarize import embed_file

    folder = clips_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    durable = folder / f"{clip_slug(name)}.wav"
    src = find_channel_audio(cfg, transcript.meeting_id, profile.channel)
    start, duration = snippet_window(transcript, profile)
    sample = durable if durable.exists() and durable.stat().st_size > 44 else None
    tmp: Path | None = None
    if src is not None:
        if sample is not None:
            tmp = folder / f".embed-{transcript.meeting_id}-{clip_slug(profile.speaker)}.wav"
            try:
                extract_clip(src, tmp, start, duration)
                sample = tmp
            except (AudioError, OSError):
                tmp = None
        else:
            try:
                extract_clip(src, durable, start, duration)
                sample = durable if durable.exists() else None
            except (AudioError, OSError):
                sample = None
    embedding: list[float] = []
    if sample is not None:
        try:
            embedding = list(embed_file(cfg.diarize, sample))
        except Exception:
            log.exception("could not embed identity clip for %s", name)
    if tmp is not None:
        tmp.unlink(missing_ok=True)
    clip = durable.name if durable.exists() and durable.stat().st_size > 44 else ""
    return clip, embedding


def embeddings_for_profiles(
    cfg: Config,
    meeting_id: str,
    turns,
    profiles: list[SpeakerProfile],
) -> dict[str, list[float]]:
    """Fingerprint each voice in the current meeting for suggest_matches.

    Temporary clips are deleted afterwards. Durable library snippets are only
    written on confirmation, never during this suggestion pass.
    """
    from .audio import AudioError, extract_clip
    from .diarize import embed_file

    holder = SimpleNamespace(turns=turns)
    folder = clips_dir(cfg)
    out: dict[str, list[float]] = {}
    for profile in profiles:
        src = find_channel_audio(cfg, meeting_id, profile.channel)
        if src is None:
            continue
        start, duration = snippet_window(holder, profile)
        tmp = folder / f".embed-{meeting_id}-{clip_slug(profile.speaker)}.wav"
        try:
            extract_clip(src, tmp, start, duration)
            vector = embed_file(cfg.diarize, tmp)
            if vector:
                out[profile.speaker] = vector
        except (AudioError, OSError):
            log.warning(
                "could not extract identity snippet for %s in %s",
                profile.speaker,
                meeting_id,
            )
        except Exception:
            log.exception(
                "could not embed %s in meeting=%s", profile.speaker, meeting_id
            )
        finally:
            tmp.unlink(missing_ok=True)
    return out


def play_wav(path: Path) -> None:
    import shutil
    import subprocess

    player = shutil.which("afplay") or shutil.which("ffplay")
    if not player:
        raise FileNotFoundError(f"no audio player available for {path}")
    if Path(player).name == "ffplay":
        subprocess.run(
            [player, "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
            check=False,
        )
        return
    subprocess.run([player, str(path)], check=False)


def play_meeting_voice(cfg: Config, meeting_id: str, speaker_key: str) -> Path:
    from .audio import AudioError, extract_clip
    from .records import Transcript

    path = cfg.transcript_json(meeting_id)
    if not path.exists():
        raise FileNotFoundError(f"no transcript for {meeting_id}")
    transcript = Transcript.read(path)
    profile = next(
        (p for p in transcript.speaker_profiles if p.speaker == speaker_key),
        None,
    )
    if profile is None:
        raise FileNotFoundError(f"{speaker_key!r} is not a voice in {meeting_id}")
    src = find_channel_audio(cfg, meeting_id, profile.channel)
    if src is None:
        from .records import read_notes, speaker_roster

        named = str(speaker_roster(read_notes(cfg.notes_json(meeting_id))).get(speaker_key) or "").strip()
        if named:
            library_clip = ensure_library_clip(
                cfg,
                next(
                    (
                        person
                        for person in load_library(cfg)
                        if person.name.casefold() == named.casefold()
                    ),
                    KnownSpeaker(name=named, clip=f"{clip_slug(named)}.wav"),
                ),
            )
            if library_clip is not None:
                play_wav(library_clip)
                return library_clip
        raise FileNotFoundError(
            f"no retained audio for {meeting_id} ({profile.channel}); "
            "name this voice while meeting audio is still kept so a "
            "reference clip can be saved"
        )
    start, duration = snippet_window(transcript, profile)
    dest = clips_dir(cfg) / f"{meeting_id}__{speaker_key}.wav"
    try:
        extract_clip(src, dest, start, duration)
    except AudioError as exc:
        raise FileNotFoundError(str(exc)) from exc
    play_wav(dest)
    return dest


def play_known_voice(cfg: Config, name: str) -> Path:
    person = next(
        (s for s in load_library(cfg) if s.name.casefold() == name.casefold()),
        None,
    )
    if person is None:
        raise FileNotFoundError(f"no known speaker named {name!r}")
    clip = ensure_library_clip(cfg, person)
    if clip is None:
        raise FileNotFoundError(
            f"no reference clip for {person.name}; name them in a meeting "
            "while audio is still kept, then try again"
        )
    play_wav(clip)
    return clip


def ensure_library_clip(cfg: Config, person: KnownSpeaker) -> Path | None:
    """Return a playable reference wav, extracting one from a past meeting if needed."""
    from .records import Transcript, read_notes, speaker_roster

    stored = clips_dir(cfg) / person.clip if person.clip else None
    if stored is not None and stored.is_file():
        return stored
    for meeting_id in reversed(person.meeting_ids):
        notes = read_notes(cfg.notes_json(meeting_id))
        roster = speaker_roster(notes)
        key = next(
            (
                speaker
                for speaker, named in roster.items()
                if named.casefold() == person.name.casefold()
            ),
            None,
        )
        path = cfg.transcript_json(meeting_id)
        if not key or not path.exists():
            continue
        try:
            transcript = Transcript.read(path)
        except (OSError, ValueError):
            continue
        profile = next(
            (p for p in transcript.speaker_profiles if p.speaker == key), None
        )
        if profile is None:
            continue
        filename = write_voice_clip(cfg, transcript, profile, name=person.name)
        if not filename:
            continue
        person.clip = filename
        speakers = load_library(cfg)
        for item in speakers:
            if item.name.casefold() == person.name.casefold():
                item.clip = filename
        save_library(cfg, speakers)
        return clips_dir(cfg) / filename
    return None


def forget_speaker(cfg: Config, name: str) -> bool:
    speakers = load_library(cfg)
    removed = [s for s in speakers if s.name.casefold() == name.casefold()]
    if not removed:
        return False
    save_library(cfg, [s for s in speakers if s.name.casefold() != name.casefold()])
    folder = clips_dir(cfg)
    for person in removed:
        if person.clip:
            (folder / person.clip).unlink(missing_ok=True)
        (folder / f"{clip_slug(person.name)}.wav").unlink(missing_ok=True)
    return True


def render_voices_html(cfg: Config, people: list[KnownSpeaker] | None = None) -> Path:
    people = people if people is not None else load_library(cfg)
    lines = [
        "<!doctype html><meta charset=utf-8><title>Yarn known speakers</title>",
        "<style>body{font:14px/1.4 -apple-system,sans-serif;margin:24px;max-width:720px}",
        "audio{width:100%;margin-top:6px} .card{border-bottom:1px solid #ddd;padding:12px 0}",
        ".meta{color:#666;font-size:12px}</style>",
        "<h1>Known speakers</h1>",
        "<p>These are voices you have named. Play a clip to check it is the "
        "right person. Yarn will only attach a name to new speech after you "
        "confirm it in a meeting.</p>",
    ]
    if not people:
        lines.append("<p>No named voices yet. Identify speakers after a meeting.</p>")
    for person in people:
        clip = ensure_library_clip(cfg, person)
        clip_html = (
            f'<audio controls src="{clip.as_uri()}"></audio>'
            if clip is not None
            else "<p class=meta>No reference clip yet. Name this voice while meeting audio is still kept.</p>"
        )
        last = person.last_seen or "—"
        lines.append(
            "<div class=card>"
            f"<strong>{_esc(person.name)}</strong>"
            f"<div class=meta>{person.confirmation_count} confirmation(s) · "
            f"last seen { _esc(last)}</div>"
            f"{clip_html}"
            "</div>"
        )
    lines.append(
        "<p class=meta>To remove someone: <code>ma voices --forget NAME</code></p>"
    )
    path = cfg.root / "speakers" / "library.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
