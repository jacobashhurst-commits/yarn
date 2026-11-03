"""Space Yarn did not keep.

A video of a meeting is about 1 GB an hour. Yarn never records that.
The audio it does record is about 320 KB a second (the raw capture plus
the mono track), and that is deleted once the transcript exists and the
retention window has passed. Audio still on disk is not counted: it is
still there.
"""

from __future__ import annotations

from pathlib import Path

# A plain video recording of the meeting is 1 GB an hour.
VIDEO_BYTES_PER_HOUR = 1_000_000_000
# Measured from Yarn's own archive: raw.wav is about 288 KB/s and the
# mono track is 32 KB/s.
AUDIO_BYTES_PER_SECOND = 320_000


def bytes_not_stored(duration_s: float, *, audio_kept: bool) -> int:
    seconds = max(0.0, float(duration_s))
    video = round(seconds * VIDEO_BYTES_PER_HOUR / 3600)
    if audio_kept:
        return video
    return video + round(seconds * AUDIO_BYTES_PER_SECOND)


def format_saved(num_bytes: int) -> str:
    """840 MB, 4.2 GB, 86 GB, 1.4 TB. The unit steps up at each thousand."""
    size = max(0, int(num_bytes))
    if size >= 1_000_000_000_000:
        return _unit(size / 1_000_000_000_000, "TB")
    if size >= 1_000_000_000:
        return _unit(size / 1_000_000_000, "GB")
    return _unit(size / 1_000_000, "MB")


def _unit(value: float, name: str) -> str:
    if value >= 10:
        return f"{round(value):.0f} {name} saved"
    return f"{value:.1f} {name} saved"


def saved_bytes(root: Path) -> int:
    """Video never recorded, plus audio that is no longer on disk."""
    archive = root / "recordings" / "archive"
    total = 0
    folder = root / "transcripts"
    if not folder.exists():
        return 0
    for path in folder.rglob("*.json"):
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meeting_id = str(data.get("meeting_id") or path.stem)
        try:
            duration = float(data.get("duration_s") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        kept = archive.exists() and any(archive.glob(f"{meeting_id}__*"))
        total += bytes_not_stored(duration, audio_kept=kept)
    return total
