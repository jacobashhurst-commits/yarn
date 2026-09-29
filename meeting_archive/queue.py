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
    return {
        "inbox": len(inbox),
        "summaries_pending": len(summaries),
        "remaining": len(inbox) + len(summaries),
    }


def refresh(cfg: Config, exclude: str = "") -> dict:
    existing = _load(cfg)
    payload = {
        **existing,
        **_counts(cfg, exclude),
        "updated_epoch": int(time.time()),
    }
    if payload["remaining"] == 0 and not exclude and not payload["working_id"]:
        payload["current"] = 0
        payload["total"] = 0
        payload["last_id"] = ""
    write_json_atomic(status_path(cfg), payload)
    return payload


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
        current = 0
        total = 0
        last_id = ""
    payload = {
        **existing,
        **counts,
        "current": current,
        "total": total,
        "working_id": "",
        "last_id": last_id,
        "updated_epoch": int(time.time()),
    }
    write_json_atomic(status_path(cfg), payload)
    return payload
