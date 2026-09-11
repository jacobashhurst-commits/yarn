"""Audio retention.

Audio is deleted on a timer, not immediately after a successful embed. The
first fortnight of running this is when wrong input devices, bad mic gain and
half-captured meetings get discovered, and none of those are recoverable from
a transcript.

Two guards before anything is deleted: the retention window must have elapsed,
and a transcript for that meeting must exist on disk. A meeting that failed to
transcribe keeps its audio indefinitely, which is the only sensible default.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


@dataclass
class PruneResult:
    deleted: list[str]
    kept_no_transcript: list[str]
    bytes_freed: int

    def summary(self) -> str:
        mb = self.bytes_freed / (1024 * 1024)
        parts = [f"{len(self.deleted)} file(s) eligible, {mb:.1f} MB"]
        if self.kept_no_transcript:
            parts.append(
                f"kept {len(self.kept_no_transcript)} file(s) with no transcript"
            )
        return "; ".join(parts)


def prune(cfg: Config, dry_run: bool = False, now: float | None = None) -> PruneResult:
    now = now if now is not None else time.time()
    cutoff = now - cfg.audio_retention_days * 86400

    deleted: list[str] = []
    kept: list[str] = []
    freed = 0

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

        size = path.stat().st_size
        if not dry_run:
            path.unlink()
        deleted.append(path.name)
        freed += size

    return PruneResult(deleted=deleted, kept_no_transcript=kept, bytes_freed=freed)
