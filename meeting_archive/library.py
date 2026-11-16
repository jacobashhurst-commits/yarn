"""Lightweight meeting library over the existing archive files."""

from __future__ import annotations

import json
import re
import logging
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote
from urllib.request import urlopen

from . import jobs, records
from .config import Config
from .records import Transcript, parse_meeting_id
from .summarize import is_pending_summary

log = logging.getLogger(__name__)

# The library page is a file the browser opens. Delete is a button on that
# page, so a small local server has to be awake to receive it. It listens
# only on this Mac, and only for the token written into the page.
LIBRARY_PORT = 47331
# Bumped when the routes change, so a server left running from an older
# Yarn is replaced instead of answering 404 to a link it does not know.
_SERVER_MARK = b"yarn-library/3"


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
        "<th>Speakers</th><th>Summary</th><th>Audio</th><th></th>",
        "</tr></thead><tbody>",
    ]
    action = _delete_action(cfg)
    for row in rows:
        mins = int(row.duration_s // 60)
        secs = int(row.duration_s % 60)
        title = _title_cell(action, row, _meeting_href(cfg, row, action[2]))
        audio = _audio_label(row)
        summary = (
            "Written"
            if row.has_summary
            else ("Waiting" if is_pending_summary(row.error) else "Not yet")
        )
        lines.append(
            "<tr>"
            f"<td class=\"mono\">{_esc(row.date)}</td>"
            f"<td>{title}</td>"
            f"<td>{_esc(_state_label(row.state))}</td>"
            f"<td>{mins}m {secs:02d}s</td>"
            f"<td>{row.confirmed}/{row.speakers}</td>"
            f"<td>{summary}</td>"
            f"<td>{audio}</td>"
            f"<td>{_delete_button(action, row.meeting_id)}</td>"
            "</tr>"
        )
        if row.error:
            css = "note" if is_pending_summary(row.error) else "err"
            lines.append(
                f'<tr><td colspan="8" class="{css}">{_esc(_meeting_notice(row.error))}</td></tr>'
            )
    lines.append("</tbody></table>")
    path = cfg.root / "library.html"
    path.write_text(
        htmltheme.document("Yarn library", "\n".join(lines)),
        encoding="utf-8",
    )
    return path


def render_transcript_index(cfg: Config, rows: list[MeetingRow] | None = None) -> Path:
    """One page of meetings, each opening a Yarn-styled transcript."""
    from . import htmltheme, records

    rows = rows if rows is not None else list_meetings(cfg)
    items = []
    token = _delete_action(cfg)[2]
    for row in rows:
        if not row.has_transcript:
            continue
        href = _meeting_href(cfg, row, token)
        if not href:
            continue
        mins = int(row.duration_s // 60)
        secs = int(row.duration_s % 60)
        items.append(
            f'<li><a href="{_esc(href)}">{_esc(row.title)}</a> '
            f'<span class="mono">{_esc(row.date)} · {mins}m {secs:02d}s</span></li>'
        )
    body = "\n".join(
        [
            htmltheme.masthead("Transcripts", "Meetings on this Mac"),
            '<p class="lede">Each meeting opens as a page. The text files stay on disk.</p>',
            "<ul>",
            *items,
            "</ul>" if items else "<p>No transcripts yet.</p>",
        ]
    )
    if not items:
        body = "\n".join(
            [
                htmltheme.masthead("Transcripts", "Meetings on this Mac"),
                "<p>No transcripts yet.</p>",
            ]
        )
    path = cfg.root / "transcripts.html"
    path.write_text(htmltheme.document("Yarn transcripts", body), encoding="utf-8")
    return path


def _audio_label(row: MeetingRow) -> str:
    if row.keep_audio:
        return "Kept"
    if row.audio_present:
        return "Held"
    return "Purged"


def _delete_action(cfg: Config) -> tuple[str, str, str]:
    token_path = cfg.root / ".library-token"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(24)
        token_path.write_text(token, encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
    base = f"http://127.0.0.1:{LIBRARY_PORT}"
    return (f"{base}/delete", f"{base}/rename", token)


def _meeting_href(cfg: Config, row: MeetingRow, token: str) -> str:
    """Where a meeting's title links to. Empty when there is nothing to open.

    Normally the page file, written here if it is missing. With Space saver
    on, an older meeting has no page file: the link goes to the small local
    server, which renders the page from the transcript when it is opened.
    Writing every page each time the library opened would undo the saving.
    """
    from . import space_saver

    page = cfg.transcript_html(row.meeting_id)
    if not row.has_transcript:
        if page.exists():
            return page.as_uri()
        fallback = cfg.transcript_md(row.meeting_id)
        return fallback.as_uri() if fallback.exists() else ""
    if not space_saver.keeps_page(cfg, row.meeting_id):
        return (
            f"http://127.0.0.1:{LIBRARY_PORT}/meeting"
            f"?id={quote(row.meeting_id)}&token={quote(token)}"
        )
    try:
        page = records.ensure_transcript_page(
            cfg, Transcript.read(cfg.transcript_json(row.meeting_id))
        )
    except (OSError, ValueError):
        if not page.exists():
            page = cfg.transcript_md(row.meeting_id)
    return page.as_uri() if page.exists() else ""


def render_meeting_page(cfg: Config, meeting_id: str) -> bytes:
    """One meeting's page, made now and not saved.

    No audio player: a page served from the local server cannot play a file
    from disk, which the browser blocks. Recent meetings keep a real page
    file, and that is where recordings still within their retention play.
    """
    from .models import speaker_display_indices
    from .speakers import library_details

    if not re.fullmatch(r"[0-9T:\-]{10,32}", meeting_id or ""):
        raise ValueError("not a meeting id")
    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    notes = records.read_notes(cfg.notes_json(meeting_id))
    roster = records.speaker_roster(notes)
    display = speaker_display_indices(transcript.speaker_profiles)
    titles, companies = library_details(cfg)
    return records.render_transcript_html(
        transcript,
        cfg.turn_labeller(roster, display),
        notes,
        titles=titles,
        companies=companies,
        recordings=[],
    ).encode("utf-8")


def _title_cell(action: tuple[str, str, str], row: MeetingRow, href: str) -> str:
    _, rename, token = action
    shown = row.title
    link = f'<a href="{_esc(href)}">{_esc(shown)}</a>' if href else _esc(shown)
    return (
        f'<div class="name-line">{link}'
        '<button type="button" class="texty" '
        "onclick=\"var f=this.parentElement.nextElementSibling;"
        'f.hidden=!f.hidden">Edit</button></div>'
        f'<form class="name-edit" hidden method="post" action="{rename}">'
        f'<input name="name" value="{_esc(shown)}" aria-label="Meeting name">'
        f'<input type="hidden" name="meeting" value="{_esc(row.meeting_id)}">'
        f'<input type="hidden" name="token" value="{_esc(token)}">'
        '<button class="texty" type="submit">Save</button>'
        "</form>"
    )


def _delete_button(action: tuple[str, str, str], meeting_id: str) -> str:
    url, _, token = action
    return (
        f'<form method="post" action="{url}" '
        'onsubmit="return confirm(\'Delete this meeting and its transcript?\')">'
        f'<input type="hidden" name="meeting" value="{_esc(meeting_id)}">'
        f'<input type="hidden" name="token" value="{_esc(token)}">'
        '<button class="delete" type="submit">Delete</button>'
        "</form>"
    )


def _belongs(name: str, meeting_id: str) -> bool:
    return name == meeting_id or name.startswith(meeting_id + ".") or name.startswith(
        meeting_id + "__"
    )


def _meeting_paths(cfg: Config, meeting_id: str) -> list[Path]:
    json_path = cfg.transcript_json(meeting_id)
    found: list[Path] = [
        json_path,
        cfg.notes_json(meeting_id),
        jobs.job_path(cfg, meeting_id),
        jobs.whisper_checkpoint_path(cfg, meeting_id),
    ]
    if json_path.exists():
        found.append(cfg.transcript_md(meeting_id))
        found.append(cfg.transcript_html(meeting_id))
    folders = [
        cfg.in_progress_dir,
        cfg.inbox_dir,
        cfg.processing_dir,
        cfg.audio_archive_dir,
        cfg.failed_dir,
        cfg.jobs_dir,
        json_path.parent,
    ]
    for folder in folders:
        if not folder.exists():
            continue
        for path in folder.iterdir():
            if path.is_file() and _belongs(path.name, meeting_id):
                found.append(path)
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _busy_reason(cfg: Config, meeting_id: str) -> str:
    from .pipeline import active_capture

    active = active_capture(cfg)
    if active is not None and _belongs(active.name, meeting_id):
        return "Yarn is still recording this meeting."
    try:
        status = json.loads(cfg.processing_status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if status.get("meeting_id") == meeting_id and status.get("phase"):
        return "Yarn is still processing this meeting."
    return ""


def delete_meeting(cfg: Config, meeting_id: str) -> None:
    """Remove one meeting, its transcript, and the audio and search copy."""
    try:
        parse_meeting_id(meeting_id)
    except ValueError as exc:
        raise ValueError(f"unknown meeting {meeting_id}") from exc
    reason = _busy_reason(cfg, meeting_id)
    if reason:
        raise RuntimeError(reason)
    # The readable page is named from the transcript, so resolve it first.
    paths = _meeting_paths(cfg, meeting_id)
    for path in paths:
        path.unlink(missing_ok=True)
    try:
        from .store import MeetingStore

        MeetingStore(cfg.chroma_dir, cfg.collection).delete_meeting(meeting_id)
    except Exception:
        log.warning("could not drop the search copy of %s", meeting_id, exc_info=True)
    from . import queue as queue_mod

    queue_mod.refresh(cfg)


def _server_is_current(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/status", timeout=0.4) as resp:
            return resp.read() == _SERVER_MARK
    except (OSError, ValueError):
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def ensure_server(cfg: Config) -> None:
    """Start the delete listener if this Mac does not already have the current one."""
    info_path = cfg.root / ".library-server.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        pid = int(info.get("pid") or 0)
        port = int(info.get("port") or LIBRARY_PORT)
    except (OSError, ValueError):
        pid, port = 0, LIBRARY_PORT
    if port == LIBRARY_PORT and _server_is_current(port):
        return
    if _pid_alive(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.05)
    _delete_action(cfg)
    subprocess.Popen(
        [sys.executable, "-m", "meeting_archive.library"],
        env={**os.environ, "YARN_LIBRARY_SERVE": "1"},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if _server_is_current(LIBRARY_PORT):
            return
        time.sleep(0.05)
    log.warning("the library delete button could not start")


def rename_meeting(cfg: Config, meeting_id: str, name: str) -> None:
    """Correct the name shown in the library, and on the transcript page."""
    try:
        parse_meeting_id(meeting_id)
    except ValueError as exc:
        raise ValueError(f"unknown meeting {meeting_id}") from exc
    name = " ".join(name.split())
    if not name:
        raise ValueError("The meeting needs a name.")
    reason = _busy_reason(cfg, meeting_id)
    if reason:
        raise RuntimeError(reason)
    path = cfg.transcript_json(meeting_id)
    if not path.exists():
        raise FileNotFoundError(f"no transcript for {meeting_id}")
    transcript = Transcript.read(path)
    previous_html = cfg.transcript_html(meeting_id)
    previous_md = cfg.transcript_md(meeting_id)
    records.write_notes(
        cfg.notes_json(meeting_id),
        meeting_id=meeting_id,
        notes=name,
        applied=False,
    )
    notes = records.read_notes(cfg.notes_json(meeting_id))
    transcript.title = records.meeting_title(notes, transcript)
    transcript.md_name = records.markdown_filename(meeting_id, transcript.title)
    transcript.write(path)
    records.ensure_transcript_page(cfg, transcript, notes)
    current_html = cfg.transcript_html(meeting_id)
    if previous_html != current_html:
        previous_html.unlink(missing_ok=True)
    current_md = cfg.transcript_md(meeting_id)
    if previous_md != current_md and previous_md.exists() and not current_md.exists():
        previous_md.replace(current_md)
    elif previous_md != current_md:
        previous_md.unlink(missing_ok=True)
    job = jobs.load(cfg, meeting_id)
    if job is not None and job.title != transcript.title:
        job.title = transcript.title
        jobs.save(cfg, job)


def serve(cfg: Config) -> None:
    token = _delete_action(cfg)[2]
    handler = _handler(cfg, token)

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True

    try:
        httpd = Server(("127.0.0.1", LIBRARY_PORT), handler)
    except OSError:
        log.warning("library delete listener is already using port %s", LIBRARY_PORT)
        return
    info = {"pid": os.getpid(), "port": LIBRARY_PORT}
    (cfg.root / ".library-server.json").write_text(json.dumps(info), encoding="utf-8")
    httpd.serve_forever()


def _is_local_host_header(value: str) -> bool:
    host = value.strip().casefold()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1"}


def _handler(cfg: Config, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _from_this_mac(self) -> bool:
            """Refuse a request a web page reached through another name.

            The server only listens on 127.0.0.1, but a page on the internet
            can point its own domain at that address. Its requests then
            arrive here carrying that domain as Host. A browser opening the
            library does not.
            """
            return _is_local_host_header(self.headers.get("Host") or "")

        def _has_token(self, given: str) -> bool:
            return len(given) == len(token) and secrets.compare_digest(given, token)

        def do_GET(self) -> None:  # noqa: N802
            if not self._from_this_mac():
                self._send(403, b"Refused", "text/plain")
                return
            path, _, query = self.path.partition("?")
            if path == "/status":
                self._send(200, _SERVER_MARK, "text/plain")
                return
            if path in ("/", "/library"):
                # The page lists every meeting by title. Any program on this
                # Mac could ask for it, so it needs the same token the
                # delete button does.
                given = (parse_qs(query).get("token") or [""])[0]
                if not self._has_token(given):
                    self._send(403, b"Refused", "text/plain")
                    return
                self._send(200, render_html(cfg).read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/meeting":
                fields = parse_qs(query)
                if not self._has_token((fields.get("token") or [""])[0]):
                    self._send(403, b"Refused", "text/plain")
                    return
                try:
                    body = render_meeting_page(cfg, (fields.get("id") or [""])[0])
                except (OSError, ValueError):
                    self._send(404, b"That meeting is not here.", "text/plain")
                    return
                self._send(200, body, "text/html; charset=utf-8")
                return
            self._send(404, b"Not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if not self._from_this_mac():
                self._send(403, b"Refused", "text/plain")
                return
            path = self.path.split("?", 1)[0]
            if path not in ("/delete", "/rename"):
                self._send(404, b"Not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(max(length, 0), 20000))
            fields = parse_qs(raw.decode("utf-8", "replace"))
            given = (fields.get("token") or [""])[0]
            meeting_id = (fields.get("meeting") or [""])[0]
            if not self._has_token(given):
                self._send(403, b"Refused", "text/plain")
                return
            try:
                if path == "/rename":
                    rename_meeting(cfg, meeting_id, (fields.get("name") or [""])[0])
                else:
                    delete_meeting(cfg, meeting_id)
            except (OSError, RuntimeError, ValueError) as exc:
                body = f"<p>{_esc(str(exc))}</p>".encode()
                self._send(409, body, "text/html; charset=utf-8")
                return
            self._send(200, render_html(cfg).read_bytes(), "text/html; charset=utf-8")

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _state_label(state: str) -> str:
    return {
        "complete": "Done",
        "running": "Working",
        "failed": "Failed",
        "pending": "Waiting",
    }.get(state, state.replace("_", " "))


def _meeting_notice(error: str) -> str:
    if is_pending_summary(error):
        if "timed out" in error.lower():
            return (
                "Summary pending. The local model ran out of time. "
                "Open Ollama, then use Summarise pending."
            )
        if error.lower().startswith("deferred:"):
            if "evening" in error.lower():
                return (
                    "Summary pending. Yarn writes these in the evening, "
                    "or when you next open the laptop after that."
                )
            if "manual" in error.lower():
                return "Summary pending. Summaries are manual. Use Summarise pending."
            return (
                "Summary pending. Ollama was not running. "
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


if __name__ == "__main__" and os.environ.get("YARN_LIBRARY_SERVE") == "1":
    from .config import load_config

    serve(load_config())
