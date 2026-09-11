"""The on-disk formats. These, not Chroma, are the source of truth.

Three small JSON files per meeting:

  manifest   written by `ma finalize` into the inbox; the only thing the
             watcher triggers on. Written last and moved into place
             atomically, so the watcher can never see a half-written
             recording.
  transcript segments, merged turns, detected names. Permanent.
  notes      whatever Jacob typed into the popup, if anything. Arrives
             whenever he gets round to it, possibly after ingest.

Everything is written via a temp file plus rename, because the watcher is
reading the same directories.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    NameCandidate,
    Segment,
    SpeakerGuess,
    SpeakerProfile,
    SpeakerSpan,
    Turn,
    format_duration,
    format_timestamp,
)

MEETING_ID_FORMAT = "%Y-%m-%dT%H-%M-%S"


def new_meeting_id(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime(MEETING_ID_FORMAT)


def parse_meeting_id(meeting_id: str) -> datetime:
    return datetime.strptime(meeting_id, MEETING_ID_FORMAT)


def date_label(meeting_id: str) -> str:
    """Human-readable meeting time for headers, e.g. 'Fri 11 Sep 2026 14:03'."""
    try:
        return parse_meeting_id(meeting_id).strftime("%a %d %b %Y %H:%M")
    except ValueError:
        return meeting_id


_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\n\r\t]+')
_WORD = re.compile(r"[A-Za-z0-9']+")
_TITLE_STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "this", "that", "it", "is", "was", "are", "be",
}


def meeting_title(notes: dict | None, transcript: "Transcript") -> str:
    """Five words at most: Jacob's notes if he typed any, otherwise a suggestion.

    The date is added later, in the filename. This is only the human title.
    """
    note = str((notes or {}).get("notes") or "").strip()
    if note:
        words = [w[:24] for w in _WORD.findall(note.splitlines()[0])[:5]]
        if words:
            return " ".join(words)
    source = (transcript.summary or "").strip()
    if source:
        for line in source.splitlines():
            line = line.strip().lstrip("#*- ")
            if line and not line.lower().startswith(("key point", "decision", "action")):
                source = line
                break
    else:
        source = " ".join(turn.text for turn in transcript.turns[:3])
    words = [w[:24] for w in _WORD.findall(source) if w.lower() not in _TITLE_STOP][:5]
    return " ".join(words) if words else "Meeting"


def markdown_filename(meeting_id: str, title: str) -> str:
    """`2026-09-11 Watch show.md` — date first so a folder sorts chronologically."""
    date = meeting_id[:10] if len(meeting_id) >= 10 else meeting_id
    cleaned = _UNSAFE_FILENAME.sub(" ", title).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "Meeting"
    return f"{date} {cleaned}.md"


def write_json_atomic(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def write_text_atomic(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

@dataclass
class Manifest:
    meeting_id: str
    channels: dict[str, str]  # {"mic": "<filename>", "sys": "<filename>"}
    started_at: str = ""
    ended_at: str = ""
    duration_s: float = 0.0
    raw_file: str = ""

    def to_dict(self) -> dict:
        return {
            "meeting_id": self.meeting_id,
            "channels": self.channels,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.duration_s, 3),
            "raw_file": self.raw_file,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            meeting_id=d["meeting_id"],
            channels=dict(d.get("channels") or {}),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            duration_s=float(d.get("duration_s") or 0.0),
            raw_file=d.get("raw_file", ""),
        )

    @classmethod
    def read(cls, path: Path) -> "Manifest":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def write(self, path: Path) -> Path:
        return write_json_atomic(path, self.to_dict())


# ---------------------------------------------------------------------------
# transcript
# ---------------------------------------------------------------------------

@dataclass
class Transcript:
    meeting_id: str
    segments: list[Segment] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    detected_names: list[NameCandidate] = field(default_factory=list)
    duration_s: float = 0.0
    started_at: str = ""
    ended_at: str = ""
    model: str = ""
    created_at: str = ""
    summary: str = ""
    summary_model: str = ""
    summary_error: str = ""
    # Human-facing markdown filename, e.g. "2026-09-11 Watch show.md".
    # The JSON stays named after the meeting id so the rest of the pipeline
    # never has to parse a title back out of a path.
    title: str = ""
    md_name: str = ""
    # --- diarization ---
    # Spans are kept for the same reason raw segments are: the audio is deleted
    # after a week, and re-deriving turns or re-running the merge later must not
    # need it back.
    speaker_spans: list[SpeakerSpan] = field(default_factory=list)
    speaker_profiles: list[SpeakerProfile] = field(default_factory=list)
    speaker_guesses: list[SpeakerGuess] = field(default_factory=list)
    diarize_model: str = ""
    # Set when diarization was wanted but could not run. Recorded rather than
    # raised: a missing local model file must not cost a transcript.
    diarize_error: str = ""

    def to_dict(self) -> dict:
        d = {
            "meeting_id": self.meeting_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.duration_s, 3),
            "model": self.model,
            "created_at": self.created_at or datetime.now().isoformat(timespec="seconds"),
            "summary": self.summary,
            "summary_model": self.summary_model,
            "title": self.title,
            "md_name": self.md_name,
            "detected_names": [c.to_dict() for c in self.detected_names],
            "turns": [t.to_dict() for t in self.turns],
            # Raw segments are kept so a future version can re-merge or re-run
            # diarization without needing the deleted audio back.
            "segments": [s.to_dict() for s in self.segments],
        }
        # Omitted entirely when diarization did not run, so an undiarized
        # transcript stays as small and as readable as it was before.
        if self.diarize_model:
            d["diarize_model"] = self.diarize_model
        if self.summary_error:
            d["summary_error"] = self.summary_error
        if self.diarize_error:
            d["diarize_error"] = self.diarize_error
        if self.speaker_profiles:
            d["speaker_profiles"] = [p.to_dict() for p in self.speaker_profiles]
        if self.speaker_guesses:
            d["speaker_guesses"] = [g.to_dict() for g in self.speaker_guesses]
        if self.speaker_spans:
            d["speaker_spans"] = [s.to_dict() for s in self.speaker_spans]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        names = []
        for n in d.get("detected_names") or []:
            names.append(
                NameCandidate(
                    name=n["name"],
                    count=int(n.get("count", 0)),
                    attributed_to=n.get("attributed_to", "other"),
                    patterns=set(n.get("patterns") or []),
                    evidence=n.get("evidence", ""),
                )
            )
        return cls(
            meeting_id=d["meeting_id"],
            segments=[Segment.from_dict(s) for s in d.get("segments") or []],
            turns=[Turn.from_dict(t) for t in d.get("turns") or []],
            detected_names=names,
            duration_s=float(d.get("duration_s") or 0.0),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            model=d.get("model", ""),
            created_at=d.get("created_at", ""),
            summary=d.get("summary", ""),
            summary_model=d.get("summary_model", ""),
            summary_error=d.get("summary_error", ""),
            title=d.get("title", ""),
            md_name=d.get("md_name", ""),
            speaker_spans=[
                SpeakerSpan.from_dict(s) for s in d.get("speaker_spans") or []
            ],
            speaker_profiles=[
                SpeakerProfile.from_dict(p) for p in d.get("speaker_profiles") or []
            ],
            speaker_guesses=[
                SpeakerGuess.from_dict(g) for g in d.get("speaker_guesses") or []
            ],
            diarize_model=d.get("diarize_model", ""),
            diarize_error=d.get("diarize_error", ""),
        )

    def unnamed_speakers(self, roster: dict | None = None) -> list[SpeakerProfile]:
        """Profiles for voices Jacob has not yet put a name to."""
        roster = roster or {}
        return [
            p
            for p in self.speaker_profiles
            if not str(roster.get(p.speaker, "")).strip()
        ]

    @classmethod
    def read(cls, path: Path) -> "Transcript":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def write(self, path: Path) -> Path:
        return write_json_atomic(path, self.to_dict())


def render_markdown(
    transcript: Transcript,
    label_fn,
    notes: dict | None = None,
) -> str:
    """Human-readable transcript. Written for Jacob's eyes, not for embedding.

    `label_fn` takes a Turn, not a channel, so a diarized recording can label
    each voice separately. A plain channel labeller is still accepted for
    callers that predate diarization.
    """
    notes = notes or {}
    roster = speaker_roster(notes)
    minutes = int(transcript.duration_s // 60)
    seconds = int(transcript.duration_s % 60)
    label_turn = _turn_labeller(label_fn)

    heading = transcript.title.strip() if transcript.title else "Meeting"
    lines = [f"# {heading} — {date_label(transcript.meeting_id)}", ""]
    lines.append(f"- Duration: {minutes}m {seconds:02d}s")
    if notes.get("attendees"):
        lines.append(f"- Attendees: {notes['attendees']}")
    suggested = ", ".join(
        c.name for c in transcript.detected_names if c.attributed_to == "other"
    )
    if suggested:
        lines.append(f"- Names heard in conversation: {suggested}")
    if notes.get("notes"):
        lines.append(f"- Jacob's notes: {notes['notes']}")
    lines.append(f"- Model: {transcript.model}")
    if transcript.diarize_model:
        voices = len(transcript.speaker_profiles)
        lines.append(f"- Diarization: {transcript.diarize_model} ({voices} voices)")
    if transcript.diarize_error:
        # Surfaced in the transcript, not just the log, because the absence of
        # speaker labels is otherwise indistinguishable from a one-person call.
        lines.append(f"- Diarization unavailable: {transcript.diarize_error}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    if transcript.summary:
        lines.append(transcript.summary.strip())
    elif transcript.summary_error:
        lines.append(f"_Summary unavailable: {transcript.summary_error}_")
    elif transcript.turns:
        lines.append("_Summary has not been generated._")
    else:
        lines.append("_No speech was available to summarize._")
    lines.append("")

    # A short who-was-who block, so the numbered labels below are decodable
    # without scrolling and without opening the JSON.
    if transcript.speaker_profiles:
        lines.append("## Voices")
        lines.append("")
        for profile in transcript.speaker_profiles:
            name = str(roster.get(profile.speaker, "")).strip()
            shown = name or f"Speaker {profile.speaker.rsplit('-', 1)[-1]}"
            detail = f"{format_duration(profile.speaking_s)} across {profile.turns} turns"
            if not name:
                detail += " — not yet identified"
            line = f"- **{shown}** ({detail})"
            if profile.quip:
                line += f' — said: "{profile.quip}"'
            lines.append(line)
        lines.append("")

    lines.append("---")
    lines.append("")

    # An empty transcript and a transcript that failed to render look the same
    # on the page, so say which it is.
    if not transcript.turns:
        lines.append(
            "_No speech was recognised in this recording. The audio was "
            "captured but Whisper found nothing to transcribe — usually a "
            "microphone that was muted, unplugged, or not permitted._"
        )
        lines.append("")

    for turn in transcript.turns:
        stamp = format_timestamp(turn.start)
        lines.append(f"**[{stamp}] {label_turn(turn)}:** {turn.text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _turn_labeller(label_fn):
    """Accept either a Turn labeller or a plain channel labeller.

    Turn labellers carry a `takes_turn` marker (set by Config.turn_labeller).
    Anything without it is treated as the older channel-only labeller, which
    keeps every caller that never cared about speakers working unchanged.
    """
    if getattr(label_fn, "takes_turn", False):
        return label_fn
    return lambda turn: label_fn(turn.channel)


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------

def empty_notes(meeting_id: str) -> dict:
    return {
        "meeting_id": meeting_id,
        "notes": "",
        "attendees": "",
        "speakers": {},
        "applied": True,
        "updated_at": "",
    }


def read_notes(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def listed_speakers(notes: dict | None) -> list[str]:
    """Names entered in the "Who spoke?" box, in display order.

    The field is still called ``attendees`` on disk for compatibility with
    existing transcripts and search metadata. Its UI meaning is now narrower:
    include every voice in the recording, including the user and anyone heard
    over a phone or video call. That makes the count useful to diarization
    without inventing a separate source of meeting truth.
    """
    raw = str((notes or {}).get("attendees") or "")
    return [name.strip() for name in raw.replace("\n", ",").split(",") if name.strip()]


def expected_diarized_speakers(
    notes: dict | None,
    channels: set[str],
    own_names: list[str],
) -> int | None:
    """Expected cluster count for the channel the diarizer actually sees.

    Room audio contains every listed voice. Call audio separates the user's
    microphone and only diarizes ``sys``, so remove one voice when the user
    included them as requested. A blank list leaves clustering automatic and
    therefore unlimited.
    """
    names = listed_speakers(notes)
    if not names:
        return None
    if "room" in channels:
        return len(names)
    if "sys" in channels:
        own = {name.casefold() for name in own_names}
        others = [name for name in names if name.casefold() not in own]
        return len(others) or None
    return None


def speaker_roster(notes: dict | None) -> dict[str, str]:
    """{speaker_key: name} from a notes payload, skipping blanks.

    The roster lives in the notes file rather than a file of its own so it
    inherits the whole late-arrival path for free: the watcher already
    reindexes a meeting whose notes changed, which is exactly what naming a
    voice needs. Identifying Speaker 2 an hour after the meeting costs one
    reindex and no re-transcription.
    """
    raw = (notes or {}).get("speakers") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in raw.items()
        if str(value).strip()
    }


def write_notes(
    path: Path,
    meeting_id: str,
    notes: str = "",
    attendees: str = "",
    speakers: dict | None = None,
    applied: bool = False,
) -> dict:
    """Save popup output. `applied=False` tells the watcher to reindex."""
    existing = read_notes(path)
    merged_speakers = dict(existing.get("speakers") or {})
    for key, value in (speakers or {}).items():
        text = str(value).strip()
        if text:
            # Only ever adds or corrects. A dismissed dialog sends nothing and
            # therefore cannot un-name a voice Jacob already identified.
            merged_speakers[str(key)] = text

    payload: dict[str, Any] = {
        "meeting_id": meeting_id,
        # Blank input never wipes an earlier answer.
        "notes": notes or existing.get("notes", ""),
        "attendees": attendees or existing.get("attendees", ""),
        "speakers": merged_speakers,
        "applied": applied,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json_atomic(path, payload)
    return payload


def mark_notes_applied(path: Path) -> None:
    data = read_notes(path)
    if not data:
        return
    data["applied"] = True
    write_json_atomic(path, data)
