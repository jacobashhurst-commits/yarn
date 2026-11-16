"""Space saver: stop keeping a web page for every old meeting.

A meeting is stored as a transcript (`.json`, the record everything is
rebuilt from) and a readable `.md`. The `.html` page beside them is a third
copy, made only so a meeting opens nicely in a browser. With Space saver on,
that page is kept for recent meetings and made when asked for older ones.

What this module may delete is deliberately narrow, and it is decided by
construction, not by pattern:

  - a transcript page, found by asking the config where the page for a
    known transcript lives. It never globs `*.html` and deletes the result,
    so a file someone saved next to their transcripts is not touched.
  - a dated daily brief (`briefs/YYYY-MM-DD.html`) past the same age. The
    current brief, `latest.html`, always stays.

It never deletes `.json`, `.md`, notes, audio, voice clips or anything in
the search index. Everything it removes can be made again from the `.json`.

Off by default. Nothing here runs unless the setting is on or someone asks.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .space_saved import SPACE_SAVER_FILE, format_bytes

# A meeting newer than this keeps its page. Recent meetings are the ones
# opened from the brief and the library, and while their audio is still kept
# the page is also where it plays. An older one is rendered when opened.
KEEP_PAGES_DAYS = 30

_BRIEF_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.html$")


@dataclass
class Sweep:
    paths: list[Path] = field(default_factory=list)
    bytes: int = 0

    @property
    def count(self) -> int:
        return len(self.paths)

    def summary(self) -> str:
        if not self.paths:
            return "Nothing to remove. Older meetings have no saved pages."
        word = "page" if self.count == 1 else "pages"
        return f"{self.count} {word}, {format_bytes(self.bytes)}"

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "bytes": self.bytes,
            "label": format_bytes(self.bytes),
            "summary": self.summary(),
        }


def is_on(cfg) -> bool:
    return bool(getattr(cfg, "space_saver", False))


def meeting_is_recent(meeting_id: str, now: datetime | None = None) -> bool:
    """Whether this meeting still keeps its page. Unreadable ids count as recent.

    Keeping a page that could have gone costs a few kilobytes. Removing one
    on a guess about a name we could not read is the wrong way to be wrong.
    """
    from .records import parse_meeting_id

    try:
        when = parse_meeting_id(meeting_id)
    except (ValueError, TypeError):
        return True
    return when >= (now or datetime.now()) - timedelta(days=KEEP_PAGES_DAYS)


def keeps_page(cfg, meeting_id: str, now: datetime | None = None) -> bool:
    """True when this meeting's page should exist on disk."""
    return not is_on(cfg) or meeting_is_recent(meeting_id, now)


def removable(cfg, now: datetime | None = None) -> Sweep:
    """The pages Space saver would remove right now, and their size."""
    now = now or datetime.now()
    sweep = Sweep()
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path in seen or path.is_symlink() or not path.is_file():
            return
        if path.suffix != ".html":
            return
        seen.add(path)
        sweep.paths.append(path)
        sweep.bytes += path.stat().st_size

    if cfg.transcripts_dir.exists():
        for record in sorted(cfg.transcripts_dir.rglob("*.json")):
            meeting_id = record.stem
            if meeting_is_recent(meeting_id, now):
                continue
            # The page is named after the markdown, which may carry the
            # meeting's title. Both possible names are checked; nothing else.
            add(cfg.transcript_html(meeting_id))
            add(record.with_suffix(".html"))

    briefs = cfg.briefs_dir
    if briefs.exists():
        cutoff = (now - timedelta(days=KEEP_PAGES_DAYS)).date()
        for path in sorted(briefs.iterdir()):
            match = _BRIEF_NAME.match(path.name)
            if not match:
                continue
            try:
                day = datetime(*(int(part) for part in match.groups())).date()
            except ValueError:
                continue
            if day < cutoff:
                add(path)
    return sweep


def purge(cfg, now: datetime | None = None) -> Sweep:
    """Remove those pages and add what was freed to the running total."""
    planned = removable(cfg, now)
    done = Sweep()
    for path in planned.paths:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        done.paths.append(path)
        done.bytes += size
    if done.bytes:
        _record_freed(cfg.root, done.bytes)
    return done


def _record_freed(root: Path, freed: int) -> None:
    """The Dock's "GB saved" counts this, so it is what was really removed."""
    path = root / SPACE_SAVER_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    try:
        before = max(0, int(data.get("freed_bytes") or 0))
    except (TypeError, ValueError):
        before = 0
    data["freed_bytes"] = before + freed
    data["last_run"] = datetime.now().isoformat(timespec="seconds")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, path)
