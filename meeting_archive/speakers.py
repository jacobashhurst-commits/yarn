"""Local known-speaker library.

Diarisation says "these voices are different". This module may say "Speaker 2
sounds like Adam". It never writes a name without confirmation.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config import Config
from .models import SpeakerGuess, SpeakerProfile, speaker_index
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
    # Optional job title and company, for search. Never required, and never a name.
    title: str = ""
    company: str = ""
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
            "title": self.title,
            "company": self.company,
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
            title=str(d.get("title") or ""),
            company=str(d.get("company") or ""),
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


def fold_name(name: str) -> str:
    """Identity key. jacob and Jacob are the same person."""
    return " ".join(str(name or "").casefold().split())


def search_label(person: KnownSpeaker) -> str:
    """How a person should appear in search text. Title and company are optional."""
    detail = ", ".join(
        bit for bit in (person.title.strip(), person.company.strip()) if bit
    )
    return f"{person.name} ({detail})" if detail else person.name


def library_details(cfg: Config) -> tuple[dict[str, str], dict[str, str]]:
    """Title and company by folded name, for transcript pages and search."""
    titles: dict[str, str] = {}
    companies: dict[str, str] = {}
    for person in load_library(cfg):
        key = person.name.casefold()
        if person.title.strip():
            titles[key] = person.title.strip()
        if person.company.strip():
            companies[key] = person.company.strip()
    return titles, companies


def labels_for_search(cfg: Config, names: list[str]) -> str:
    """Roster names, with a title in parentheses when one was saved."""
    library = load_library(cfg)
    labels: list[str] = []
    seen: set[str] = set()
    for raw in names:
        key = fold_name(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        people = [person for person in library if fold_name(person.name) == key]
        if not people:
            labels.append(str(raw).strip())
        elif len(people) == 1:
            labels.append(search_label(people[0]))
        else:
            labels.append("; ".join(search_label(person) for person in people))
    return ", ".join(labels)


def _detail_fits(person: KnownSpeaker, title: str, company: str) -> bool:
    """True when this row can take the new title and company.

    An empty stored field can be filled in. A different title or company
    is a different person, not a correction.
    """
    stored_title = fold_name(person.title)
    stored_company = fold_name(person.company)
    if title and stored_title and title != stored_title:
        return False
    if company and stored_company and company != stored_company:
        return False
    return True


def _match_person(
    speakers: list[KnownSpeaker],
    name: str,
    title: str = "",
    company: str = "",
) -> KnownSpeaker | None:
    """The library row this name belongs to, or None if it is a new person.

    A matching name with no title or company yet is the same person getting
    those filled in. A different title or company is someone else.
    """
    key = fold_name(name)
    title_key = fold_name(title)
    company_key = fold_name(company)
    if not key:
        return None
    candidates = [
        person
        for person in speakers
        if fold_name(person.name) == key and _detail_fits(person, title_key, company_key)
    ]
    if len(candidates) == 1:
        return candidates[0]
    exact = [
        person
        for person in candidates
        if fold_name(person.title) == title_key and fold_name(person.company) == company_key
    ]
    if len(exact) == 1:
        return exact[0]
    return None


def stored_spelling(speakers: list[KnownSpeaker], name: str) -> str:
    """Keep Jacob if the new typing is jacob."""
    key = fold_name(name)
    for person in speakers:
        if fold_name(person.name) == key:
            return person.name
    return " ".join(str(name or "").split())


def name_needs_detail(
    speakers: list[KnownSpeaker],
    name: str,
    title: str = "",
    company: str = "",
) -> bool:
    """True when this single name is already in the library and nothing new distinguishes it.

    A last name, a title, or a company is enough. Confirming the same person
    again, including a change of capitalisation, is not a second person.
    """
    folded = fold_name(name)
    if not folded or fold_name(title) or fold_name(company):
        return False
    if len(folded.split()) > 1:
        return False
    return any(fold_name(person.name).split()[0] == folded for person in speakers)


def remember_confirmation(
    cfg: Config,
    *,
    name: str,
    meeting_id: str,
    embedding: list[float] | None = None,
    clip: str = "",
    title: str = "",
    company: str = "",
    distinct: bool = False,
) -> KnownSpeaker:
    """Record that Jacob named a voice. Embeddings are averaged when present.

    `distinct` means this is a second person who shares the first name and
    was given a title or company so the two can be told apart.
    """
    speakers = load_library(cfg)
    title = title.strip()
    company = company.strip()
    name = stored_spelling(speakers, name)
    found = _match_person(speakers, name, title, company)
    if distinct and found is not None and (
        fold_name(found.title) != fold_name(title)
        or fold_name(found.company) != fold_name(company)
    ):
        found = None
    if found is None:
        found = KnownSpeaker(name=name, title=title, company=company)
        speakers.append(found)
    else:
        if title and not found.title.strip():
            found.title = title
        if company and not found.company.strip():
            found.company = company
    repeat = meeting_id in found.meeting_ids
    prior = found.confirmation_count
    if not repeat:
        found.confirmation_count += 1
        found.meeting_ids.append(meeting_id)
    found.last_seen = datetime.now().isoformat(timespec="seconds")
    if embedding:
        found.embedding = _mean_embedding(found.embedding, embedding, prior_count=prior)
    if clip:
        found.clip = clip
    save_library(cfg, speakers)
    return found


def suggest_matches(
    profiles: list[SpeakerProfile],
    library: list[KnownSpeaker],
    embeddings: dict[str, list[float]] | None = None,
    min_confidence: float = 0.35,
    min_margin: float = 0.08,
) -> list[SpeakerGuess]:
    """Return voice-match guesses that cleared both gates.

    The pipeline writes those names into the meeting notes. Anything that
    does not clear the gates is not a name. The library itself is updated
    only when a person confirms a voice in the dialog.

    Two things here are load-bearing, and both exist because the old 0.75
    floor made this function unable to return anything at all:

    `min_confidence` has to be reachable. Real same-speaker cosines with this
    extractor sit around 0.4-0.53, not the 0.9 the number implied, so the gate
    silently turned every known-speaker match into no suggestion -- which is
    why the identify dialog offered a paragraph of transcript where a name
    should have been.

    `min_margin` then stops the low floor becoming a liability. At 0.35 a
    second candidate is often close behind, and a near-tie is not an
    identification: if the runner-up is within the margin, no name is offered.
    Assignment is also global best-first rather than in profile order --
    profiles arrive sorted by who spoke first, so the greedy version let the
    earliest voice claim a name that fitted a later voice far better. That was
    harmless while the floor was unreachable and a real mislabel risk now.
    """
    embeddings = embeddings or {}

    scored: list[tuple[float, float, SpeakerProfile, KnownSpeaker]] = []
    for profile in profiles:
        # The microphone is already the wearer. Matching it here would rename
        # them to whoever they sound a bit like.
        if speaker_index(profile.speaker) is None:
            continue
        vector = embeddings.get(profile.speaker)
        if not vector:
            continue
        ranked = sorted(
            (
                (_cosine(vector, person.embedding), person)
                for person in library
                if person.embedding and is_real_name(person.name)
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not ranked:
            continue
        best_score, person = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        margin = best_score - runner_up
        if best_score < min_confidence or margin < min_margin:
            continue
        scored.append((best_score, margin, profile, person))

    # Strongest match first, so a confident pairing takes its name before a
    # weaker one can. Ties break on speaker key for a deterministic result.
    scored.sort(key=lambda row: (-row[0], -row[1], row[2].speaker))

    guesses: list[SpeakerGuess] = []
    used_names: set[str] = set()
    used_speakers: set[str] = set()
    for best_score, _margin, profile, person in scored:
        if person.name in used_names or profile.speaker in used_speakers:
            continue
        used_names.add(person.name)
        used_speakers.add(profile.speaker)
        guesses.append(
            SpeakerGuess(
                speaker=profile.speaker,
                name=person.name,
                basis="voice",
                confidence=best_score,
                evidence=(
                    f"Matched {person.name} based on "
                    f"{person.confirmation_count} previously confirmed "
                    f"meeting(s)."
                ),
            )
        )
    return guesses


def names_for_roster(guesses: list[SpeakerGuess]) -> dict[str, str]:
    """Voice matches to store on the meeting. Conversation guesses stay out."""
    kept: dict[str, str] = {}
    for guess in guesses:
        if guess.basis != "voice" or not is_real_name(guess.name):
            continue
        kept[guess.speaker] = guess.name
    return kept


_DIALOG_NAME = re.compile(
    r"^[A-Za-z][A-Za-z'\-]{0,24}(?:\s+[A-Za-z][A-Za-z'\-]{0,24}){0,2}$"
)


def usable_dialog_guess(
    name: str,
    attendees: list[str] | None = None,
    known: list[str] | None = None,
) -> str:
    """What may pre-fill the identify dialog's name field.

    Transcript quotes, locations ("Sydney" from "I'm Sydney-based"), and any
    other sentence fragment stay out of the box. If Jacob listed attendees or
    has a known-speaker library, the guess also has to be one of those people
    -- a conversation word that happens to look like a name is not a guess.
    """
    raw = " ".join((name or "").split())
    if not raw or len(raw) > 32 or not _DIALOG_NAME.match(raw):
        return ""
    allowed = {
        item.casefold()
        for item in (*(attendees or []), *(known or []))
        if str(item).strip()
    }
    if allowed and raw.casefold() not in allowed:
        return ""
    return raw


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
    # The bare microphone profile is the wearer, not a voice still to name.
    # Counting it would stop the last listed person being paired with the
    # one remaining voice on the computer audio.
    diarized = [
        profile for profile in profiles if speaker_index(profile.speaker) is not None
    ]
    unnamed = [
        profile
        for profile in diarized
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
    channels = {profile.channel for profile in diarized}
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


_CLIP_TARGET_S = 4.0
_CLIP_MIN_S = 1.2
_CLIP_MAX_S = 12.0
_CLIP_PAD_S = 0.45
_CLIP_INSET_S = 0.30


@dataclass
class IdentityClip:
    """One stretch of a single voice, with exactly the words spoken in it."""

    start: float
    duration: float
    text: str
    # False when no whole transcript segment fitted inside a solo stretch, so
    # the text is the nearest transcript rather than a word-for-word match.
    exact: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "text": self.text,
            "exact": self.exact,
        }


def identity_clips(transcript, profile: SpeakerProfile, limit: int = 10) -> list[IdentityClip]:
    """Best clips of one voice, strongest first.

    Each clip is cut on transcript segment boundaries inside a stretch where
    no other diarized voice is active, so the dialog can show the exact words
    the clip plays. Later entries are what "Next clip" steps through.
    """
    segments = sorted(
        (
            s
            for s in getattr(transcript, "segments", []) or []
            if s.speaker == profile.speaker and (s.text or "").strip()
        ),
        key=lambda s: float(s.start),
    )
    quip = (profile.quip or "").strip()
    clips: list[IdentityClip] = []
    for pad in (_CLIP_PAD_S, 0.2):
        solo = [
            iv
            for iv in _solo_intervals(transcript, profile, pad=pad)
            if iv[1] - iv[0] >= _CLIP_MIN_S
        ]
        clips = _segment_clips(solo, segments, quip)
        if clips:
            break
    if not clips:
        start, duration = _solo_window(transcript, profile)
        clips = [
            IdentityClip(
                start=start,
                duration=duration,
                text=_text_near(segments, start, start + duration),
                exact=False,
            )
        ]
    return clips[: max(1, limit)]


def _segment_clips(
    solo: list[tuple[float, float]], segments, quip: str
) -> list[IdentityClip]:
    out: list[IdentityClip] = []
    for iv_start, iv_end in solo:
        inside = [
            s
            for s in segments
            if float(s.start) >= iv_start - 0.15 and float(s.end) <= iv_end + 0.15
        ]
        run: list = []
        for segment in inside:
            if run and (
                float(segment.start) - float(run[-1].end) > 0.6
                or float(segment.end) - float(run[0].start) > _CLIP_MAX_S
            ):
                _add_clip(out, run, iv_start, iv_end)
                run = []
            run.append(segment)
        _add_clip(out, run, iv_start, iv_end)
    out.sort(key=lambda clip: _clip_score(clip, quip), reverse=True)
    return out


def _add_clip(out: list[IdentityClip], run: list, iv_start: float, iv_end: float) -> None:
    if not run:
        return
    start = max(iv_start, float(run[0].start) - 0.1)
    end = min(iv_end, float(run[-1].end) + 0.15)
    if end - start < _CLIP_MIN_S:
        return
    text = " ".join(" ".join(s.text.split()) for s in run)
    out.append(IdentityClip(start=start, duration=end - start, text=text))


def _clip_score(clip: IdentityClip, quip: str) -> float:
    from .quips import _is_all_filler, score_sentence

    if _is_all_filler(clip.text):
        return 0.0
    d = clip.duration
    if d < 3.0:
        length = d / 3.0
    elif d <= 9.0:
        length = 1.0
    else:
        length = 9.0 / d
    score = length * 3.0 + min(score_sentence(clip.text, position=1), 6.0) * 0.5
    if quip and quip in clip.text:
        score += 0.5
    return score


def _text_near(segments, start: float, end: float) -> str:
    words = [
        " ".join(s.text.split())
        for s in segments
        if min(end, float(s.end)) - max(start, float(s.start)) > 0
    ]
    return " ".join(words)


def snippet_window(transcript, profile: SpeakerProfile) -> tuple[float, float]:
    """The best single-voice clip of this speaker, as (start, duration)."""
    clip = identity_clips(transcript, profile, limit=1)[0]
    return clip.start, clip.duration


def _solo_window(transcript, profile: SpeakerProfile) -> tuple[float, float]:
    """A short stretch where only this voice is speaking.

    Diarization spans are the source of truth. ASR turns can look isolated
    while another span is still active, which is how Play clip used to play
    two people at once. If nothing is cleanly solo, return the longest
    exclusive leftover rather than a neighbour-contaminated fallback.
    """
    quip_turn = _quip_turn(transcript, profile)
    for pad in (_CLIP_PAD_S, 0.2, 0.0):
        solo = _solo_intervals(transcript, profile, pad=pad)
        usable = [iv for iv in solo if iv[1] - iv[0] >= _CLIP_MIN_S]
        chosen = _pick_solo_interval(usable or solo, quip_turn)
        if chosen is not None:
            return _window_from_interval(chosen)
    start = max(0.0, float(profile.first_at or 0.0))
    return start, _CLIP_MIN_S


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


def _quip_turn(transcript, profile: SpeakerProfile):
    quip = (profile.quip or "").strip()
    if not quip:
        return None
    for turn in getattr(transcript, "turns", []) or []:
        if turn.speaker == profile.speaker and quip in (turn.text or ""):
            return turn
    return None


def _solo_intervals(
    transcript, profile: SpeakerProfile, pad: float
) -> list[tuple[float, float]]:
    speaker = profile.speaker
    spans = [s for s in (getattr(transcript, "speaker_spans", None) or []) if s.speaker]
    if spans:
        mine = [(float(s.start), float(s.end)) for s in spans if s.speaker == speaker]
        others = [
            (float(s.start), float(s.end)) for s in spans if s.speaker != speaker
        ]
    else:
        turns = list(getattr(transcript, "turns", []) or [])
        mine = [
            (float(t.start), float(t.end))
            for t in turns
            if t.speaker == speaker
        ]
        others = [
            (float(t.start), float(t.end))
            for t in turns
            if t.speaker and t.speaker != speaker
        ]
    blocked = _merge_intervals([(s - pad, e + pad) for s, e in others])
    return _subtract_intervals(_merge_intervals(mine), blocked)


def _pick_solo_interval(
    intervals: list[tuple[float, float]], quip_turn
) -> tuple[float, float] | None:
    if not intervals:
        return None

    def score(interval: tuple[float, float]) -> tuple[float, float]:
        length = interval[1] - interval[0]
        bonus = 0.0
        if quip_turn is not None:
            overlap = min(interval[1], float(quip_turn.end)) - max(
                interval[0], float(quip_turn.start)
            )
            # Quip is a tie-break only. A chopped leftover of an overlapping
            # quote must not beat a longer stretch that is only this voice.
            if overlap >= 0.8 and length >= _CLIP_MIN_S:
                bonus = 0.4
        return (length + bonus, length)

    return max(intervals, key=score)


def _window_from_interval(
    interval: tuple[float, float],
    target: float = _CLIP_TARGET_S,
    inset: float = _CLIP_INSET_S,
    min_s: float = _CLIP_MIN_S,
) -> tuple[float, float]:
    start, end = interval
    length = max(0.0, end - start)
    if length <= min_s + 0.05:
        used_inset = min(0.12, length * 0.08)
    else:
        used_inset = min(inset, max(0.0, (length - min_s) / 2.0))
    inner_start = start + used_inset
    inner_end = end - used_inset
    inner = max(0.4, inner_end - inner_start)
    duration = min(target, inner)
    slack = max(0.0, inner - duration)
    return max(0.0, inner_start + slack / 2.0), duration


def _merge_intervals(
    intervals: list[tuple[float, float]], join_gap: float = 0.12
) -> list[tuple[float, float]]:
    ordered = sorted((float(s), float(e)) for s, e in intervals if e > s)
    if not ordered:
        return []
    out = [ordered[0]]
    for start, end in ordered[1:]:
        prev_s, prev_e = out[-1]
        if start <= prev_e + join_gap:
            out[-1] = (prev_s, max(prev_e, end))
        else:
            out.append((start, end))
    return out


def _subtract_intervals(
    base: list[tuple[float, float]],
    blocked: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in base:
        pieces = [(start, end)]
        for block_start, block_end in blocked:
            next_pieces: list[tuple[float, float]] = []
            for piece_start, piece_end in pieces:
                if block_end <= piece_start or block_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if piece_start < block_start:
                    next_pieces.append((piece_start, block_start))
                if block_end < piece_end:
                    next_pieces.append((block_end, piece_end))
            pieces = next_pieces
        result.extend((s, e) for s, e in pieces if e - s > 0.05)
    return result


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


_EMBED_CLIPS = 4


def voice_embedding(
    cfg: Config,
    transcript,
    profile: SpeakerProfile,
    max_clips: int = _EMBED_CLIPS,
) -> list[float]:
    """Average fingerprint over several single-voice clips of one speaker.

    One 4s clip gave cosines that could not separate people (0.2-0.5 for
    everyone). Averaging a few clean clips is what makes the match usable.
    """
    from .audio import AudioError, extract_clip
    from .diarize import embed_file

    src = find_channel_audio(cfg, transcript.meeting_id, profile.channel)
    if src is None:
        return []
    folder = clips_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    vectors: list[list[float]] = []
    clips = [c for c in identity_clips(transcript, profile, limit=max_clips) if c.duration >= 1.5]
    for index, clip in enumerate(clips):
        tmp = folder / f".embed-{transcript.meeting_id}-{clip_slug(profile.speaker)}-{index}.wav"
        try:
            extract_clip(src, tmp, clip.start, clip.duration)
            vector = list(embed_file(cfg.diarize, tmp))
            if vector:
                vectors.append(_normalise(vector))
        except (AudioError, OSError):
            log.warning(
                "could not extract identity clip for %s in %s",
                profile.speaker,
                transcript.meeting_id,
            )
        except Exception:
            log.exception(
                "could not embed %s in meeting=%s", profile.speaker, transcript.meeting_id
            )
        finally:
            tmp.unlink(missing_ok=True)
    if not vectors:
        return []
    size = len(vectors[0])
    vectors = [v for v in vectors if len(v) == size]
    return _normalise([sum(v[i] for v in vectors) / len(vectors) for i in range(size)])


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

    folder = clips_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    durable = folder / f"{clip_slug(name)}.wav"
    src = find_channel_audio(cfg, transcript.meeting_id, profile.channel)
    if src is not None and not (durable.exists() and durable.stat().st_size > 44):
        start, duration = snippet_window(transcript, profile)
        try:
            extract_clip(src, durable, start, duration)
        except (AudioError, OSError):
            pass
    embedding = voice_embedding(cfg, transcript, profile)
    clip = durable.name if durable.exists() and durable.stat().st_size > 44 else ""
    return clip, embedding


def embeddings_for_profiles(
    cfg: Config,
    meeting_id: str,
    turns,
    profiles: list[SpeakerProfile],
    *,
    spans=None,
    segments=None,
) -> dict[str, list[float]]:
    """Fingerprint each voice in the current meeting for suggest_matches.

    Temporary clips are deleted afterwards. Durable library snippets are only
    written on confirmation, never during this suggestion pass.
    """
    holder = SimpleNamespace(
        meeting_id=meeting_id,
        turns=turns,
        speaker_spans=list(spans or []),
        segments=list(segments or []),
    )
    out: dict[str, list[float]] = {}
    for profile in profiles:
        if speaker_index(profile.speaker) is None:
            continue
        vector = voice_embedding(cfg, holder, profile)
        if vector:
            out[profile.speaker] = vector
    return out


_JUNK_NAMES = {
    "unsure", "not sure", "unknown", "dont know", "don't know", "no idea",
    "idk", "n/a", "na", "none", "skip", "?", "??", "-", "nobody",
}


def is_real_name(name: str) -> bool:
    """False for answers like "unsure" that must never become a known speaker."""
    folded = " ".join(str(name or "").split()).casefold()
    return bool(folded) and folded not in _JUNK_NAMES


def voice_matches(
    vector: list[float], library: list[KnownSpeaker], limit: int = 3
) -> list[tuple[str, float]]:
    """Known speakers ranked by how close they sound, best first."""
    if not vector:
        return []
    ranked = sorted(
        (
            (person.name, _cosine(vector, person.embedding))
            for person in library
            if person.embedding and is_real_name(person.name)
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )
    return ranked[:limit]


# Prefill thresholds for the identify dialog. Cosines with this extractor sit
# around 0.45-0.7 for the same person and 0.2-0.4 for others.
_PREFILL_STRONG = 0.50
_PREFILL_LIKELY = 0.38
_PREFILL_MARGIN = 0.04


def dialog_prediction(
    ranked: list[tuple[str, float]],
    conversation_guess: str = "",
) -> tuple[str, str]:
    """(name to pre-fill, why) for one voice. Name is "" when nothing is credible."""
    if ranked:
        best_name, best = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        if best >= _PREFILL_STRONG and best - runner >= _PREFILL_MARGIN:
            return best_name, f"sounds like {best_name} (strong voice match {best:.2f})"
    if conversation_guess:
        return conversation_guess, f"named in the conversation as {conversation_guess}"
    if ranked:
        best_name, best = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        if best >= _PREFILL_LIKELY and best - runner >= _PREFILL_MARGIN:
            return best_name, f"probably {best_name} (voice match {best:.2f})"
    return "", "no confident voice match"


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
    dest = meeting_clip_path(cfg, meeting_id, speaker_key, 0, transcript=transcript)
    play_wav(dest)
    return dest


def meeting_clip_path(
    cfg: Config,
    meeting_id: str,
    speaker_key: str,
    index: int = 0,
    *,
    transcript=None,
) -> Path:
    """Extract clip `index` of one voice (see identity_clips) and return the wav."""
    from .audio import AudioError, extract_clip
    from .records import Transcript

    if transcript is None:
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
        raise FileNotFoundError(f"no retained audio for {meeting_id}")
    clips = identity_clips(transcript, profile)
    clip = clips[max(0, min(int(index), len(clips) - 1))]
    dest = clips_dir(cfg) / f"{meeting_id}__{speaker_key}__{int(index)}.wav"
    try:
        extract_clip(src, dest, clip.start, clip.duration)
    except AudioError as exc:
        raise FileNotFoundError(str(exc)) from exc
    return dest


def identify_payload(cfg: Config, meeting_id: str) -> dict[str, Any]:
    """Everything the identify window needs for one meeting, as JSON-able data."""
    from . import models
    from .records import Transcript, listed_speakers, read_notes, speaker_roster

    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    notes = read_notes(cfg.notes_json(meeting_id))
    roster = speaker_roster(notes)
    attendees = listed_speakers(notes)
    library = load_library(cfg)
    known = [p.name for p in library if is_real_name(p.name)]
    has_audio = bool(transcript.speaker_profiles) and any(
        find_channel_audio(cfg, meeting_id, p.channel) is not None
        for p in transcript.speaker_profiles
    )
    display = models.speaker_display_indices(transcript.speaker_profiles)
    voices = []
    for profile in transcript.speaker_profiles:
        if models.speaker_index(profile.speaker) is None:
            continue
        clips = identity_clips(transcript, profile)
        ranked: list[tuple[str, float]] = []
        if has_audio:
            try:
                ranked = voice_matches(voice_embedding(cfg, transcript, profile), library)
            except Exception:
                log.exception("voice match failed for %s in %s", profile.speaker, meeting_id)
        conversation = usable_dialog_guess(profile.guess, attendees=attendees, known=known)
        guess, reason = dialog_prediction(ranked, conversation)
        index = display.get(profile.speaker) or models.speaker_index(profile.speaker) or 0
        # Under ten seconds in a real meeting is a fragment, not a person to
        # name. New meetings fold these into a longer voice before they become
        # a speaker. This still hides them on a meeting already transcribed.
        from .diarize import MIN_NOTABLE_SPEAKER_S

        crumb = float(profile.speaking_s or 0.0) < MIN_NOTABLE_SPEAKER_S
        voices.append(
            {
                "key": profile.speaker,
                "label": f"Speaker {index}",
                "detail": (
                    f"Spoke {models.format_duration(profile.speaking_s)} "
                    f"across {profile.turns} "
                    f"{'turn' if profile.turns == 1 else 'turns'}, from "
                    f"{models.format_timestamp(profile.first_at)}"
                ),
                "named": roster.get(profile.speaker, ""),
                "guess": guess,
                "reason": reason,
                "speaking_s": round(float(profile.speaking_s or 0.0), 3),
                "turns": int(profile.turns or 0),
                "crumb": crumb,
                "matches": [{"name": n, "score": round(s, 3)} for n, s in ranked],
                "clips": [
                    {**c.to_dict(), "at": models.format_timestamp(c.start)} for c in clips
                ],
            }
        )
    names: list[str] = []
    for name in (*attendees, *known):
        if name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    return {
        "meeting_id": meeting_id,
        "banner": meeting_banner(cfg, meeting_id),
        "has_audio": has_audio,
        "names": names,
        "people": [
            {
                "name": person.name,
                "title": person.title,
                "company": person.company,
            }
            for person in library
            if is_real_name(person.name)
        ],
        "voices": voices,
    }


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
    from . import htmltheme

    people = people if people is not None else load_library(cfg)
    lines = [
        htmltheme.masthead("Speakers", "Known speakers"),
        "<p class=\"lede\">These are voices you have named. Play a clip to check it is the "
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
            else '<p class="mono">No reference clip yet. Name this voice while meeting audio is still kept.</p>'
        )
        last = person.last_seen or "not seen"
        lines.append(
            '<div class="voice">'
            f"<strong>{_esc(person.name)}</strong>"
            + (
                f'<div class="mono">{_esc(", ".join(bit for bit in (person.title, person.company) if bit.strip()))}</div>'
                if person.title.strip() or person.company.strip()
                else ""
            )
            + f'<div class="mono">{person.confirmation_count} confirmation(s) · '
            f"last seen {_esc(last)}</div>"
            f"{clip_html}"
            "</div>"
        )
    lines.append(
        '<p class="mono">To remove someone: <code>ma voices --forget NAME</code></p>'
    )
    path = cfg.root / "speakers" / "library.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        htmltheme.document("Yarn known speakers", "\n".join(lines)),
        encoding="utf-8",
    )
    return path


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        return list(vector)
    return [x / norm for x in vector]


def _mean_embedding(
    existing: list[float], incoming: list[float], prior_count: int = 1
) -> list[float]:
    """Running mean over confirmations, so one odd meeting cannot drag a voice
    halfway to someone else."""
    if not existing or len(existing) != len(incoming):
        return _normalise(list(incoming))
    weight = max(1, int(prior_count))
    a = _normalise(existing)
    b = _normalise(incoming)
    return _normalise([(old * weight + new) / (weight + 1) for old, new in zip(a, b)])


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))
