"""Audio retention.

Audio is deleted on a timer, not immediately after a successful embed. The
first fortnight of running this is when wrong input devices, bad mic gain and
half-captured meetings get discovered, and none of those are recoverable from
a transcript.

Guards before anything is deleted:

- the policy is not `never`
- the retention window must have elapsed
- a transcript for that meeting must exist on disk
- the meeting is not marked keep_audio

A meeting that failed to transcribe keeps its audio indefinitely. A deferred
summary does not block deletion: the transcript is already the durable record.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .records import read_notes

log = logging.getLogger(__name__)


@dataclass
class PruneResult:
    deleted: list[str]
    kept_no_transcript: list[str]
    bytes_freed: int
    kept_override: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mb = self.bytes_freed / (1024 * 1024)
        parts = [f"{len(self.deleted)} file(s) eligible, {mb:.1f} MB"]
        if self.kept_no_transcript:
            parts.append(
                f"kept {len(self.kept_no_transcript)} file(s) with no transcript"
            )
        if self.kept_override:
            parts.append(
                f"kept {len(self.kept_override)} file(s) marked keep_audio"
            )
        return "; ".join(parts)


def prune(cfg: Config, dry_run: bool = False, now: float | None = None) -> PruneResult:
    now = now if now is not None else time.time()
    deleted: list[str] = []
    kept: list[str] = []
    kept_override: list[str] = []
    freed = 0

    if cfg.audio_retention_days <= 0:
        return PruneResult(
            deleted=deleted,
            kept_no_transcript=kept,
            bytes_freed=freed,
            kept_override=kept_override,
        )

    cutoff = now - cfg.audio_retention_days * 86400

    for path in sorted(cfg.audio_archive_dir.glob("*")):
        if not path.is_file():
            continue
        if path.stat().st_mtime > cutoff:
            continue

        meeting_id = path.name.split("__")[0]
        if not cfg.transcript_json(meeting_id).exists():
            kept.append(path.name)
            log.warning(
                "keeping %s: no transcript for meeting=%s, so the audio is the "
                "only copy of this meeting",
                path.name,
                meeting_id,
            )
            continue

        notes = read_notes(cfg.notes_json(meeting_id))
        if notes.get("keep_audio"):
            kept_override.append(path.name)
            continue

        size = path.stat().st_size
        if not dry_run:
            path.unlink()
        deleted.append(path.name)
        freed += size

    return PruneResult(
        deleted=deleted,
        kept_no_transcript=kept,
        bytes_freed=freed,
        kept_override=kept_override,
    )


def delete_meeting_audio(cfg: Config, meeting_id: str) -> list[str]:
    """Remove archived audio for one meeting. Refuses if no transcript exists."""
    if not cfg.transcript_json(meeting_id).exists():
        raise FileNotFoundError(
            f"no transcript for {meeting_id}; audio is the only copy and was kept"
        )
    removed: list[str] = []
    for path in sorted(cfg.audio_archive_dir.glob(f"{meeting_id}__*")):
        if path.is_file():
            path.unlink()
            removed.append(path.name)
    return removed


def set_keep_audio(cfg: Config, meeting_id: str, keep: bool) -> None:
    from . import records

    records.write_notes(
        cfg.notes_json(meeting_id),
        meeting_id=meeting_id,
        keep_audio=keep,
        applied=True,
    )
