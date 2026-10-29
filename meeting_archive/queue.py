"""How much work is waiting, for the menu bar.

Inbox manifests are meetings not yet transcribed. Transcripts with an empty
summary are meetings waiting on Ollama. Those two never overlap: a meeting
is in one bucket or the other.

`current` / `total` are a sticky batch counter so the menu can say 1/5, then
2/5, instead of restarting at 1 every time the next meeting starts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config
from .records import write_json_atomic

_EMPTY = {
    "inbox": 0,
    "summaries_pending": 0,
    "remaining": 0,
    "updated_epoch": 0,
    "current": 0,
    "total": 0,
    "working_id": "",
    "last_id": "",
    "last_finished_epoch": 0,
    "blocker": "",
}


def status_path(cfg: Config) -> Path:
    return cfg.root / ".queue-status.json"


def _load(cfg: Config) -> dict:
    path = status_path(cfg)
    if not path.exists():
        return dict(_EMPTY)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_EMPTY)
    return {
        "inbox": int(data.get("inbox") or 0),
        "summaries_pending": int(data.get("summaries_pending") or 0),
        "remaining": int(data.get("remaining") or 0),
        "updated_epoch": int(data.get("updated_epoch") or 0),
        "current": int(data.get("current") or 0),
        "total": int(data.get("total") or 0),
        "working_id": str(data.get("working_id") or ""),
        "last_id": str(data.get("last_id") or ""),
        # _load whitelists keys, so a field missing here is silently dropped
        # on every read no matter what end_work writes.
        "last_finished_epoch": int(data.get("last_finished_epoch") or 0),
        "blocker": str(data.get("blocker") or ""),
    }


def _counts(cfg: Config, exclude: str = "") -> dict:
    from .pipeline import needs_summary
    from .records import Transcript

    inbox = [
        path.stem
        for path in sorted(cfg.inbox_dir.glob("*.json"))
        if not path.name.endswith(".partial") and path.stem != exclude
    ]
    summaries: list[str] = []
    blocker = ""
    if cfg.transcripts_dir.exists():
        for path in sorted(cfg.transcripts_dir.rglob("*.json")):
            try:
                transcript = Transcript.read(path)
            except (OSError, ValueError):
                continue
            if transcript.meeting_id == exclude:
                continue
            if needs_summary(transcript):
                summaries.append(transcript.meeting_id)
                err = str(getattr(transcript, "summary_error", "") or "").strip()
                if err and not blocker:
                    blocker = err.removeprefix("deferred: ").strip()
    _write_lines(cfg, inbox, summaries)
    return {
        "inbox": len(inbox),
        "summaries_pending": len(summaries),
        "remaining": len(inbox) + len(summaries),
        "blocker": blocker,
    }


def lines_path(cfg: Config) -> Path:
    return cfg.root / ".queue-lines.txt"


def _write_lines(cfg: Config, inbox: list[str], summaries: list[str]) -> None:
    """One plain line per waiting meeting, so the shell menu can list them."""
    from .pipeline import _about

    rows: list[str] = []
    for meeting_id, what in [
        *((m, "to transcribe & separate voices") for m in inbox),
        *((m, "summary only") for m in summaries),
    ]:
        when = f"{meeting_id[5:10]} {meeting_id[11:16].replace('-', ':')}"
        try:
            about = _about(cfg, meeting_id)
        except Exception:
            about = ""
        rows.append(f"{when}  {about or 'Untitled meeting'}: {what}")
    path = lines_path(cfg)
    try:
        tmp = path.with_suffix(".partial")
        tmp.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def refresh(cfg: Config, exclude: str = "") -> dict:
    existing = _load(cfg)
    payload = {
        **existing,
        **_counts(cfg, exclude),
        "updated_epoch": int(time.time()),
    }
    current = int(payload.get("current") or 0)
    working = str(payload.get("working_id") or "")
    if payload["remaining"] == 0 and not exclude and not working:
        payload["current"] = 0
        payload["total"] = 0
    elif current > 0 and working:
        # Voice separation can run for most of an hour without rewriting
        # the batch counter. A meeting saved in that time is waiting behind
        # the one on screen, so 1 of 1 has to become 1 of 2.
        behind = _counts(cfg, exclude=working)["remaining"]
        payload["total"] = max(int(payload.get("total") or 0), current + behind)
        _sync_processing_batch(cfg, payload, behind)
    write_json_atomic(status_path(cfg), payload)
    return payload


def _sync_processing_batch(cfg: Config, payload: dict, behind: int) -> None:
    """Copy the batch counter onto the status file the window actually reads.

    A long stage writes that file once, at the start. The window polls it
    every second, so growing `total` here is what changes "1 of 1" while
    the second meeting sits in the queue.
    """
    path = cfg.processing_status
    if not path.exists():
        return
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    working = str(payload.get("working_id") or "")
    shown = str(status.get("meeting_id") or "")
    if working and shown and shown != working:
        return
    current = int(payload.get("current") or 0)
    total = int(payload.get("total") or 0)
    if (
        int(status.get("current") or 0) == current
        and int(status.get("total") or 0) == total
        and int(status.get("remaining") or 0) == behind
    ):
        return
    status["current"] = current
    status["total"] = total
    status["remaining"] = behind
    write_json_atomic(path, status)


def read(cfg: Config) -> dict:
    return _load(cfg)


def begin_work(cfg: Config, meeting_id: str) -> dict:
    """Mark this meeting as the one on screen. Same meeting does not bump 1/5."""
    existing = _load(cfg)
    counts = _counts(cfg, exclude=meeting_id)
    others = counts["remaining"]
    current = int(existing.get("current") or 0)
    total = int(existing.get("total") or 0)
    working_id = str(existing.get("working_id") or "")
    last_id = str(existing.get("last_id") or "")
    if meeting_id and meeting_id in (working_id, last_id) and current > 0:
        total = max(total, current + others)
    elif total > 0 and current > 0:
        current += 1
        total = max(total, current + others)
    else:
        current = 1
        total = 1 + others
    payload = {
        **existing,
        **counts,
        "current": current,
        "total": total,
        "working_id": meeting_id,
        "last_id": meeting_id,
        "updated_epoch": int(time.time()),
    }
    write_json_atomic(status_path(cfg), payload)
    return payload


def end_work(cfg: Config, meeting_id: str = "") -> dict:
    existing = _load(cfg)
    working_id = str(existing.get("working_id") or "")
    if meeting_id and working_id and working_id != meeting_id:
        return refresh(cfg)
    counts = _counts(cfg)
    current = int(existing.get("current") or 0)
    total = int(existing.get("total") or 0)
    last_id = meeting_id or str(existing.get("last_id") or "")
    if counts["remaining"] == 0:
        # The 1/5 counter resets when the run drains, but `last_id` must
        # survive it: an idle menu bar that cannot name the last meeting it
        # finished is indistinguishable from one whose watcher died hours ago.
        current = 0
        total = 0
    finished_epoch = int(existing.get("last_finished_epoch") or 0)
    if meeting_id:
        finished_epoch = int(time.time())
    payload = {
        **existing,
        **counts,
        "current": current,
        "total": total,
        "working_id": "",
        "last_id": last_id,
        # Distinct from updated_epoch, which moves on every refresh tick and
        # so would show an idle queue as having just finished something.
        "last_finished_epoch": finished_epoch,
        "updated_epoch": int(time.time()),
    }
    write_json_atomic(status_path(cfg), payload)
    return payload
