"""Lightweight meeting library over the existing archive files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import jobs, records
from .config import Config
from .records import Transcript, markdown_filename, parse_meeting_id
from .summarize import is_pending_summary


@dataclass
class MeetingRow:
    meeting_id: str
    title: str
    date: str
    duration_s: float
    source: str
    state: str
    speakers: int
    confirmed: int
    has_summary: bool
    has_transcript: bool
    keep_audio: bool
    audio_present: bool
    error: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def list_meetings(cfg: Config) -> list[MeetingRow]:
    rows: dict[str, MeetingRow] = {}
    for path in sorted(cfg.transcripts_dir.rglob("*.json")):
        try:
            transcript = Transcript.read(path)
        except (OSError, ValueError):
            continue
        notes = records.read_notes(cfg.notes_json(transcript.meeting_id))
        roster = records.speaker_roster(notes)
        audio_present = any(cfg.audio_archive_dir.glob(f"{transcript.meeting_id}__*"))
        try:
            date = parse_meeting_id(transcript.meeting_id).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            date = transcript.meeting_id
        rows[transcript.meeting_id] = MeetingRow(
            meeting_id=transcript.meeting_id,
            title=transcript.title or "Meeting",
            date=date,
            duration_s=transcript.duration_s,
            source=transcript.source or "native",
            state="complete",
            speakers=len(transcript.speaker_profiles),
            confirmed=len(roster),
            has_summary=bool(transcript.summary.strip()),
            has_transcript=True,
            keep_audio=bool(notes.get("keep_audio")),
            audio_present=audio_present,
            error=transcript.summary_error or transcript.diarize_error,
        )
    for job in jobs.list_jobs(cfg):
        if job.meeting_id in rows:
            row = rows[job.meeting_id]
            # A leftover queued job must not make a finished transcript look
            # unprocessed. Only live or failed jobs override the row.
            if job.state == jobs.RUNNING:
                row.state = job.state
            elif job.state == jobs.FAILED and not row.has_transcript:
                row.state = job.state
            if job.state == jobs.FAILED and job.error and not row.error:
                row.error = job.error
            if job.title and row.title == "Meeting":
                row.title = job.title
            continue
        try:
            date = parse_meeting_id(job.meeting_id).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            date = job.meeting_id
        rows[job.meeting_id] = MeetingRow(
            meeting_id=job.meeting_id,
            title=job.title or "Meeting",
            date=date,
            duration_s=job.duration_s,
            source=job.source,
            state=job.state,
            speakers=0,
            confirmed=0,
            has_summary=False,
            has_transcript=cfg.transcript_json(job.meeting_id).exists(),
            keep_audio=False,
            audio_present=any(
                True
                for folder in (
                    cfg.inbox_dir,
                    cfg.processing_dir,
                    cfg.audio_archive_dir,
                    cfg.failed_dir,
                )
                for _ in folder.glob(f"{job.meeting_id}__*")
            ),
            error=job.error,
        )
    return sorted(rows.values(), key=lambda row: row.meeting_id, reverse=True)


def render_html(cfg: Config, rows: list[MeetingRow] | None = None) -> Path:
    from . import htmltheme

    rows = rows if rows is not None else list_meetings(cfg)
    lines = [
        htmltheme.masthead("Library", "Meetings on this Mac"),
        f"<p class=\"lede\">{len(rows)} meeting(s). Transcripts are the archive. Audio may expire.</p>",
        "<table><thead><tr>",
        "<th>When</th><th>Title</th><th>State</th><th>Duration</th>",
        "<th>Speakers</th><th>Summary</th><th>Audio</th>",
        "</tr></thead><tbody>",
    ]
    for row in rows:
        mins = int(row.duration_s // 60)
        secs = int(row.duration_s % 60)
        md = cfg.transcript_md(row.meeting_id)
        title = row.title
        if md.exists():
            title = f'<a href="{md.as_uri()}">{_esc(title)}</a>'
        else:
            title = _esc(title)
        audio = "kept" if row.keep_audio else ("yes" if row.audio_present else "expired")
        summary = (
            "yes"
            if row.has_summary
            else ("pending" if is_pending_summary(row.error) else "no")
        )
        lines.append(
            "<tr>"
            f"<td class=\"mono\">{_esc(row.date)}</td>"
            f"<td>{title}</td>"
            f"<td>{_esc(row.state)}</td>"
            f"<td>{mins}m {secs:02d}s</td>"
            f"<td>{row.confirmed}/{row.speakers or '—'}</td>"
            f"<td>{summary}</td>"
            f"<td>{audio}</td>"
            "</tr>"
        )
        if row.error:
            css = "note" if is_pending_summary(row.error) else "err"
            lines.append(
                f'<tr><td colspan="7" class="{css}">{_esc(_meeting_notice(row.error))}</td></tr>'
            )
    lines.append("</tbody></table>")
    path = cfg.root / "library.html"
    path.write_text(
        htmltheme.document("Yarn library", "\n".join(lines)),
        encoding="utf-8",
    )
    return path


def _meeting_notice(error: str) -> str:
    if is_pending_summary(error):
        if "timed out" in error.lower():
            return (
                "Summary pending — the local model ran out of time. "
                "Open Ollama, then use Summarise pending."
            )
        if error.lower().startswith("deferred:"):
            if "evening" in error.lower():
                return (
                    "Summary pending — Yarn writes these in the evening, "
                    "or when you next open the laptop after that."
                )
            if "manual" in error.lower():
                return "Summary pending — summaries are manual. Use Summarise pending."
            return (
                "Summary pending — Ollama was not running. "
                "Open Ollama, then use Summarise pending."
            )
    return error


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
