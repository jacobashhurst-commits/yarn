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
    speaker_display_indices,
    speaker_index,
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
    """`2026-09-11 Watch show.md` - date first so a folder sorts chronologically."""
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
    source: str = "native"
    title: str = ""
    # Empty means English, including meetings recorded before this existed.
    language: str = ""

    def to_dict(self) -> dict:
        payload = {
            "meeting_id": self.meeting_id,
            "channels": self.channels,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.duration_s, 3),
            "raw_file": self.raw_file,
            "source": self.source,
            "title": self.title,
        }
        if self.language and self.language != "en":
            payload["language"] = self.language
        return payload

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            meeting_id=d["meeting_id"],
            channels=dict(d.get("channels") or {}),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            duration_s=float(d.get("duration_s") or 0.0),
            raw_file=d.get("raw_file", ""),
            source=d.get("source", "native"),
            title=d.get("title", ""),
            language=d.get("language", ""),
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
    source: str = "native"
    whisper_engine: str = ""
    # Turns as they were before optional cleanup. Empty means cleanup never ran.
    raw_turns: list[Turn] = field(default_factory=list)
    # Spoken language. Empty and "en" are English. "auto" means it was not decided.
    language: str = ""

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
        if self.source and self.source != "native":
            d["source"] = self.source
        if self.whisper_engine:
            d["whisper_engine"] = self.whisper_engine
        if self.raw_turns:
            d["raw_turns"] = [t.to_dict() for t in self.raw_turns]
        if self.language and self.language != "en":
            d["language"] = self.language
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
            source=d.get("source", "native"),
            whisper_engine=d.get("whisper_engine", ""),
            raw_turns=[Turn.from_dict(t) for t in d.get("raw_turns") or []],
            language=d.get("language", ""),
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


_SPEAKER_MENTION = re.compile(r"\bSpeaker (\d+)\b")


def name_speakers_in_text(text: str, transcript: "Transcript", roster: dict) -> str:
    """Swap "Speaker N" for the confirmed name of that voice.

    The summary is written once, with whatever labels existed then, and is
    stored with the numbers. Substituting at render time means naming (or
    renaming) a voice later fixes the summary without asking Ollama again.
    Numbers shared by voices on different channels are left alone.
    """
    if not text or not roster:
        return text
    display = speaker_display_indices(transcript.speaker_profiles)
    by_index: dict[int, str | None] = {}
    for profile in transcript.speaker_profiles:
        index = display.get(profile.speaker)
        if index is None:
            continue
        name = str(roster.get(profile.speaker) or "").strip()
        by_index[index] = None if index in by_index else (name or None)

    def swap(match: re.Match) -> str:
        return by_index.get(int(match.group(1))) or match.group(0)

    return _SPEAKER_MENTION.sub(swap, text)


# Display-only: join consecutive same-speaker turns when the gap is short.
# Does not change the JSON transcript used for search / diarize / reindex.
_DISPLAY_COALESCE_GAP_S = 45.0


def _role_text(
    name: str,
    titles: dict[str, str] | None,
    companies: dict[str, str] | None,
) -> str:
    key = name.casefold()
    return ", ".join(
        bit
        for bit in (
            (titles or {}).get(key, "").strip(),
            (companies or {}).get(key, "").strip(),
        )
        if bit
    )


def render_markdown(
    transcript: Transcript,
    label_fn,
    notes: dict | None = None,
    *,
    processing: dict | None = None,
    titles: dict[str, str] | None = None,
    companies: dict[str, str] | None = None,
) -> str:
    """Human-readable transcript. Written for Jacob's eyes, not for embedding.

    `label_fn` takes a Turn, not a channel, so a diarized recording can label
    each voice separately. A plain channel labeller is still accepted for
    callers that predate diarization.

    Formatting here is display-only. Search still indexes the JSON turns.
    `processing` is optional stage timings from the job record
    (e.g. {"transcribe": "4m 37s", "diarize": "1h 06m", "total": "1h 12m"}).
    """
    notes = notes or {}
    roster = speaker_roster(notes)
    minutes = int(transcript.duration_s // 60)
    seconds = int(transcript.duration_s % 60)
    label_turn = _turn_labeller(label_fn)

    heading = transcript.title.strip() if transcript.title else "Meeting"
    lines = [f"# {heading}", ""]
    lines.append(f"*{date_label(transcript.meeting_id)}*")
    lines.append("")

    # Same facts as before - labeled, just easier to scan.
    lines.append(f"- **Duration:** {minutes}m {seconds:02d}s")
    if notes.get("attendees"):
        lines.append(f"- **Attendees:** {notes['attendees']}")
    suggested = ", ".join(
        c.name for c in transcript.detected_names if c.attributed_to == "other"
    )
    if suggested:
        lines.append(f"- **Names heard in conversation:** {suggested}")
    if notes.get("notes"):
        lines.append(f"- **Notes:** {notes['notes']}")
    if transcript.model:
        engine = f" ({transcript.whisper_engine})" if transcript.whisper_engine else ""
        lines.append(f"- **Model:** {transcript.model}{engine}")
    if transcript.diarize_model:
        voices = len(transcript.speaker_profiles)
        lines.append(
            f"- **Diarization:** {transcript.diarize_model} ({voices} voices)"
        )
    if transcript.diarize_error:
        # Surfaced in the transcript, not just the log, because the absence of
        # speaker labels is otherwise indistinguishable from a one-person call.
        lines.append(f"- **Diarization unavailable:** {transcript.diarize_error}")
    if processing:
        parts = []
        if processing.get("transcribe"):
            parts.append(f"Transcribe {processing['transcribe']}")
        if processing.get("diarize"):
            parts.append(f"Separate voices {processing['diarize']}")
        if processing.get("recognise"):
            parts.append(f"Match voices {processing['recognise']}")
        if processing.get("summarize"):
            parts.append(f"Summary {processing['summarize']}")
        if processing.get("index"):
            parts.append(f"Index {processing['index']}")
        if processing.get("total"):
            parts.append(f"Total {processing['total']}")
        if parts:
            lines.append(f"- **Processing:** {' · '.join(parts)}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    if transcript.summary:
        lines.append(name_speakers_in_text(transcript.summary.strip(), transcript, roster))
    elif transcript.summary_error:
        lines.append(f"_Summary unavailable: {transcript.summary_error}_")
    elif transcript.turns:
        lines.append("_Summary has not been generated._")
    else:
        lines.append("_No speech was available to summarize._")
    lines.append("")

    # One row per person. A clear voice that diarization split into several
    # tracks is still one person once they share a name.
    if transcript.speaker_profiles:
        lines.append("## Voices")
        lines.append("")
        named: dict[str, dict] = {}
        unnamed: list = []
        for profile in transcript.speaker_profiles:
            name = str(roster.get(profile.speaker, "")).strip()
            if not name:
                unnamed.append(profile)
                continue
            key = name.casefold()
            bucket = named.setdefault(
                key,
                {"name": name, "speaking_s": 0.0, "turns": 0, "quip": "", "quip_s": -1.0},
            )
            speaking = float(profile.speaking_s or 0.0)
            bucket["speaking_s"] += speaking
            bucket["turns"] += int(profile.turns or 0)
            if profile.quip and speaking >= float(bucket["quip_s"]):
                bucket["quip"] = profile.quip
                bucket["quip_s"] = speaking
        for bucket in named.values():
            detail = (
                f"{format_duration(bucket['speaking_s'])} across {bucket['turns']} turns"
            )
            role = _role_text(bucket["name"], titles, companies)
            line = f"- **{bucket['name']}**"
            if role:
                line += f", {role}"
            line += f" ({detail})"
            if bucket["quip"]:
                line += f' said: "{bucket["quip"]}"'
            lines.append(line)
        display = speaker_display_indices(transcript.speaker_profiles)
        for profile in unnamed:
            index = display.get(profile.speaker) or speaker_index(profile.speaker) or "?"
            detail = (
                f"{format_duration(profile.speaking_s)} across {profile.turns} turns, "
                "not yet identified"
            )
            line = f"- **Speaker {index}** ({detail})"
            if profile.quip:
                line += f' said: "{profile.quip}"'
            lines.append(line)
        lines.append("")

    lines.append("## Transcript")
    lines.append("")

    # An empty transcript and a transcript that failed to render look the same
    # on the page, so say which it is.
    if not transcript.turns:
        lines.append(
            "_No speech was recognised in this recording. The audio was "
            "captured but Whisper found nothing to transcribe, usually a "
            "microphone that was muted, unplugged, or not permitted._"
        )
        lines.append("")
    else:
        # Formatting only: join consecutive same-speaker turns so the page
        # breathes. JSON / search still use the original turn list.
        for speaker, start, end, text in _coalesce_turns_for_display(
            transcript.turns, label_turn
        ):
            start_stamp = format_timestamp(start)
            if end - start >= 1.0:
                stamp = f"{start_stamp}–{format_timestamp(end)}"
            else:
                stamp = start_stamp
            lines.append(f"**[{stamp}] {speaker}**")
            lines.append("")
            lines.append(text)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def meeting_recordings(cfg, meeting_id: str) -> list[tuple[str, Path]]:
    """Playable channel files still on disk, archive first.

    The raw multi-channel file is skipped. Browsers play the split wavs,
    and those are what a listen-back is for. A purged meeting returns nothing.
    """
    folders = (
        getattr(cfg, "audio_archive_dir", None),
        getattr(cfg, "processing_dir", None),
        getattr(cfg, "inbox_dir", None),
        getattr(cfg, "failed_dir", None),
    )
    found: dict[str, Path] = {}
    for folder in folders:
        if folder is None or not Path(folder).is_dir():
            continue
        for path in sorted(Path(folder).glob(f"{meeting_id}__*.wav")):
            channel = path.stem.split("__", 1)[-1]
            if channel == "raw" or channel in found:
                continue
            found[channel] = path
    order = {"room": 0, "mic": 1, "sys": 2}
    only = len(found) == 1
    return [
        (_recording_caption(cfg, channel, only), found[channel])
        for channel in sorted(found, key=lambda item: (order.get(item, 9), item))
    ]


def _recording_caption(cfg, channel: str, only: bool) -> str:
    if only:
        return "The recording"
    if channel == "mic":
        return str(getattr(cfg, "own_label", "") or "You")
    if channel == "sys":
        other = str(getattr(cfg, "other_label", "") or "")
        return "The other side" if other in {"", "Other"} else other
    if channel == "room":
        return "The room"
    return channel


def ensure_transcript_page(cfg, transcript: Transcript, notes: dict | None = None) -> Path:
    """Write the Yarn transcript page and return it.

    The daily brief and the library both link here, so a meeting opened from
    either place is the same page.
    """
    from .models import speaker_display_indices
    from .speakers import library_details

    if notes is None:
        notes = read_notes(cfg.notes_json(transcript.meeting_id))
    roster = speaker_roster(notes)
    display = speaker_display_indices(transcript.speaker_profiles)
    titles, companies = library_details(cfg)
    page = cfg.transcript_html(transcript.meeting_id)
    page.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        page,
        render_transcript_html(
            transcript,
            cfg.turn_labeller(roster, display),
            notes,
            titles=titles,
            companies=companies,
            recordings=meeting_recordings(cfg, transcript.meeting_id),
        ),
    )
    return page


def render_transcript_html(
    transcript: Transcript,
    label_fn,
    notes: dict | None = None,
    *,
    titles: dict[str, str] | None = None,
    companies: dict[str, str] | None = None,
    recordings: list[tuple[str, Path]] | None = None,
) -> str:
    """Readable meeting page. Same facts as the markdown, in the Yarn style."""
    from . import htmltheme

    notes = notes or {}
    titles = titles or {}
    companies = companies or {}
    recordings = recordings or []
    roster = speaker_roster(notes)
    minutes = int(transcript.duration_s // 60)
    seconds = int(transcript.duration_s % 60)
    label_turn = _turn_labeller(label_fn)
    heading = transcript.title.strip() if transcript.title else "Meeting"
    when = date_label(transcript.meeting_id)
    from .languages import language_label

    if transcript.language and transcript.language not in {"en", "auto"}:
        heard = language_label(transcript.language)
        if heard:
            when = f"{when} · {heard}"

    def titled(name: str) -> str:
        shown = htmltheme.html_escape(name)
        role = _role_text(name, titles, companies)
        if role:
            shown += f'<div class="role">{htmltheme.html_escape(role)}</div>'
        return shown

    summary = name_speakers_in_text(transcript.summary.strip(), transcript, roster)
    if summary:
        from .markup import markup_to_html

        summary_html = f'<div class="prose overview">{markup_to_html(summary)}</div>'
    elif transcript.summary_error:
        summary_html = f'<p class="empty">Summary unavailable.</p>'
    elif transcript.turns:
        summary_html = '<p class="empty">Summary has not been written yet.</p>'
    else:
        summary_html = '<p class="empty">No speech was available to summarize.</p>'

    voice_rows = []
    if transcript.speaker_profiles:
        named: dict[str, dict] = {}
        unnamed: list = []
        for profile in transcript.speaker_profiles:
            name = str(roster.get(profile.speaker, "")).strip()
            if not name:
                unnamed.append(profile)
                continue
            key = name.casefold()
            bucket = named.setdefault(
                key,
                {"name": name, "speaking_s": 0.0, "turns": 0, "quip": ""},
            )
            speaking = float(profile.speaking_s or 0.0)
            bucket["speaking_s"] += speaking
            bucket["turns"] += int(profile.turns or 0)
            if profile.quip and speaking >= float(bucket.get("quip_s", -1)):
                bucket["quip"] = profile.quip
                bucket["quip_s"] = speaking
        for bucket in named.values():
            quip = (
                f'<div class="why">“{htmltheme.html_escape(bucket["quip"])}”</div>'
                if bucket["quip"]
                else ""
            )
            voice_rows.append(
                f'<li><div class="item">{titled(bucket["name"])}</div>'
                f'<div class="mono">{format_duration(bucket["speaking_s"])} '
                f'across {bucket["turns"]} turns</div>{quip}</li>'
            )
        display = speaker_display_indices(transcript.speaker_profiles)
        for profile in unnamed:
            index = display.get(profile.speaker) or speaker_index(profile.speaker) or "?"
            quip = (
                f'<div class="why">“{htmltheme.html_escape(profile.quip)}”</div>'
                if profile.quip
                else ""
            )
            voice_rows.append(
                f'<li><div class="item">Speaker {htmltheme.html_escape(str(index))}</div>'
                f'<div class="mono">{format_duration(profile.speaking_s)} '
                f"across {profile.turns} turns, not yet identified</div>{quip}</li>"
            )

    attendees = str(notes.get("attendees") or "").strip()
    attendee_line = (
        f'<p class="lede">{htmltheme.html_escape(attendees)}</p>' if attendees else ""
    )
    if recordings:
        players = []
        for caption, path in recordings:
            players.append(
                '<div class="player">'
                f'<div class="item">{htmltheme.html_escape(caption)}</div>'
                f'<audio controls preload="none" src="{htmltheme.html_escape(path.as_uri())}"></audio>'
                "</div>"
            )
        tracks = ""
        if len(recordings) > 1:
            tracks = '<p class="why">A call is two tracks. Play both to hear everyone.</p>'
        listen = (
            '<section class="card"><h2>Listen</h2>'
            '<p class="why">Still on this Mac.</p>'
            f"{tracks}{''.join(players)}</section>"
        )
    else:
        listen = (
            '<section class="card"><h2>Listen</h2>'
            '<p class="empty">The recording has been cleared. The transcript stays.</p>'
            "</section>"
        )
    bars = []
    spoken: list[tuple[str, float]] = []
    if transcript.speaker_profiles:
        display = speaker_display_indices(transcript.speaker_profiles)
        totals: dict[str, float] = {}
        for profile in transcript.speaker_profiles:
            name = str(roster.get(profile.speaker, "")).strip()
            if not name:
                index = display.get(profile.speaker) or speaker_index(profile.speaker) or "?"
                name = f"Speaker {index}"
            totals[name] = totals.get(name, 0.0) + float(profile.speaking_s or 0.0)
        spoken = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:8]
    peak = max((seconds_ for _, seconds_ in spoken), default=0.0) or 1.0
    for name, amount in spoken:
        height = max(8, int(72 * (amount / peak))) if amount else 4
        tip = format_duration(amount)
        bars.append(
            f'<div class="bar-col"><div class="bar" style="height:{height}px" '
            f'title="{htmltheme.html_escape(tip)}"></div>'
            f"<span>{htmltheme.html_escape(name.split()[0])}</span>"
            f"<small>{htmltheme.html_escape(tip)}</small></div>"
        )
    who = (
        '<section class="card"><h2>Who spoke</h2>'
        + (
            f'<div class="bars">{"".join(bars)}</div>'
            if bars
            else '<p class="empty">No separated voices in this one.</p>'
        )
        + "</section>"
    )
    voices_card = ""
    if voice_rows:
        voices_card = (
            '<section class="card"><h2>Voices</h2><ol class="items">'
            + "".join(voice_rows)
            + "</ol></section>"
        )
    dots = ["#167DFF", "#48C8FA", "#0D244B", "#5aa2ff", "#1f4f86", "#8fd4fb"]
    seen_speakers: dict[str, str] = {}
    coloured = []
    if not transcript.turns:
        coloured.append('<p class="empty">No speech was recognised in this recording.</p>')
    else:
        for speaker, start, end, text in _coalesce_turns_for_display(
            transcript.turns, label_turn
        ):
            if speaker not in seen_speakers:
                seen_speakers[speaker] = dots[len(seen_speakers) % len(dots)]
            start_stamp = format_timestamp(start)
            stamp = (
                f"{start_stamp}–{format_timestamp(end)}"
                if end - start >= 1.0
                else start_stamp
            )
            coloured.append(
                '<article class="turn">'
                f'<div class="turn-meta"><span class="mono">{htmltheme.html_escape(stamp)}</span> '
                f'<span class="dot" style="background:{seen_speakers[speaker]}"></span>'
                f"{titled(speaker)}</div>"
                f"<p>{htmltheme.html_escape(text)}</p>"
                "</article>"
            )
    overview = summary_html
    body = f"""
  {htmltheme.masthead("Transcript", heading)}
  <p class="when">{htmltheme.html_escape(when)} · {minutes}m {seconds:02d}s · saved on this Mac</p>
  {attendee_line}
  {overview}
  <div class="grid">
    {listen}
    {who}
  </div>
  {voices_card}
  <section class="card"><h2>What was said</h2>{"".join(coloured)}</section>
"""
    return htmltheme.document(f"Yarn transcript, {heading}", body)


def _format_processing_span(seconds: float) -> str:
    """Like format_duration, but uses hours once a stage runs past an hour."""
    total = max(0, int(seconds))
    if total < 3600:
        return format_duration(total)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m"


def processing_from_job(job) -> dict[str, str]:
    """Human stage timings for the markdown header, from a jobs.Job-like object."""
    if job is None:
        return {}
    stages = getattr(job, "stages", None) or {}
    out: dict[str, str] = {}
    total = 0.0
    for name in ("transcribe", "diarize", "recognise", "summarize", "index"):
        stage = stages.get(name) if isinstance(stages, dict) else None
        if stage is None:
            continue
        status = getattr(stage, "status", "") or (
            stage.get("status") if isinstance(stage, dict) else ""
        )
        duration = getattr(stage, "duration_s", None)
        if duration is None and isinstance(stage, dict):
            duration = stage.get("duration_s")
        try:
            duration_s = float(duration or 0.0)
        except (TypeError, ValueError):
            duration_s = 0.0
        if status in ("done", "skipped") and duration_s > 0:
            out[name] = _format_processing_span(duration_s)
            total += duration_s
    if total > 0:
        out["total"] = _format_processing_span(total)
    return out


def _coalesce_turns_for_display(
    turns: list,
    label_turn,
    gap_s: float = _DISPLAY_COALESCE_GAP_S,
) -> list[tuple[str, float, float, str]]:
    """Join consecutive same-speaker turns for readable Markdown only."""
    blocks: list[tuple[str, float, float, str]] = []
    for turn in turns:
        speaker = label_turn(turn)
        text = (turn.text or "").strip()
        if not text:
            continue
        if (
            blocks
            and blocks[-1][0] == speaker
            and turn.start - blocks[-1][2] <= gap_s
        ):
            prev_speaker, start, _, prev_text = blocks[-1]
            blocks[-1] = (prev_speaker, start, turn.end, f"{prev_text} {text}".strip())
        else:
            blocks.append((speaker, turn.start, turn.end, text))
    return blocks


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
        "keep_audio": False,
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
    keep_audio: bool | None = None,
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
        "keep_audio": (
            bool(existing.get("keep_audio")) if keep_audio is None else bool(keep_audio)
        ),
        "applied": applied,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json_atomic(path, payload)
    return payload


def clear_speaker_names(path: Path, keys: list[str]) -> None:
    """Un-name voices. The one deliberate exception to write_notes never erasing."""
    data = read_notes(path)
    if not data:
        return
    speakers = dict(data.get("speakers") or {})
    for key in keys:
        speakers.pop(str(key), None)
    data["speakers"] = speakers
    data["applied"] = False
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json_atomic(path, data)


def mark_notes_applied(path: Path) -> None:
    data = read_notes(path)
    if not data:
        return
    data["applied"] = True
    write_json_atomic(path, data)
