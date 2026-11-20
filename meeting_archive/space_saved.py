"""What Yarn did not cost: disk space it never used, tokens nobody was billed for.

Space. A video of a meeting is about 1 GB an hour. Yarn never records that.
The audio it does record is about 320 KB a second (the raw capture plus
the mono track), and that is deleted once the transcript exists and the
retention window has passed. Audio still on disk is not counted: it is
still there. Pages removed by Space saver are counted exactly, from what
was actually deleted.

Tokens. The local model's own counts, priced at a hosted model's list
price from config. It is a comparison with something that did not happen,
so every figure built on it says "approx."

This module is the only place the arithmetic lives. The Dock used to carry
a second copy in Swift, and two copies of a formula agree only until one
of them is edited.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

# A plain video recording of the meeting is 1 GB an hour. The figure used at
# run time is Config.savings.video_gb_per_hour; this is its default.
VIDEO_BYTES_PER_HOUR = 1_000_000_000
# Measured from Yarn's own archive: raw.wav is about 288 KB/s and the
# mono track is 32 KB/s.
AUDIO_BYTES_PER_SECOND = 320_000

SPACE_SAVER_FILE = ".space-saver.json"


def bytes_not_stored(
    duration_s: float,
    *,
    audio_kept: bool,
    video_bytes_per_hour: float = VIDEO_BYTES_PER_HOUR,
) -> int:
    seconds = max(0.0, float(duration_s))
    video = round(seconds * video_bytes_per_hour / 3600)
    if audio_kept:
        return video
    return video + round(seconds * AUDIO_BYTES_PER_SECOND)


def format_bytes(num_bytes: int) -> str:
    """840 MB, 4.2 GB, 86 GB, 1.4 TB. The unit steps up at each thousand."""
    size = max(0, int(num_bytes))
    if size >= 1_000_000_000_000:
        return _unit(size / 1_000_000_000_000, "TB")
    if size >= 1_000_000_000:
        return _unit(size / 1_000_000_000, "GB")
    if size >= 1_000_000:
        return _unit(size / 1_000_000, "MB")
    if size >= 1_000:
        return _unit(size / 1_000, "KB")
    return f"{size} bytes"


def format_saved(num_bytes: int) -> str:
    size = max(0, int(num_bytes))
    # Megabytes at the smallest, so a new archive reads "0.0 MB saved"
    # rather than switching units under the reader.
    if size < 1_000_000:
        return _unit(size / 1_000_000, "MB") + " saved"
    return format_bytes(size) + " saved"


def _unit(value: float, name: str) -> str:
    if value >= 10:
        return f"{round(value):.0f} {name}"
    return f"{value:.1f} {name}"


def pages_freed_bytes(root: Path) -> int:
    """Bytes Space saver has removed so far. Exact, not estimated."""
    try:
        data = json.loads((root / SPACE_SAVER_FILE).read_text(encoding="utf-8"))
        return max(0, int(data.get("freed_bytes") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def space_breakdown(
    root: Path, video_bytes_per_hour: float = VIDEO_BYTES_PER_HOUR
) -> dict:
    """Where the saved space comes from, in bytes."""
    archive = root / "recordings" / "archive"
    folder = root / "transcripts"
    video = audio = 0
    seconds = 0.0
    meetings = 0
    kept_names = (
        {path.name.split("__")[0] for path in archive.glob("*__*")}
        if archive.exists()
        else set()
    )
    if folder.exists():
        for path in folder.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            meeting_id = str(data.get("meeting_id") or path.stem)
            try:
                duration = max(0.0, float(data.get("duration_s") or 0))
            except (TypeError, ValueError):
                duration = 0.0
            meetings += 1
            seconds += duration
            video += round(duration * video_bytes_per_hour / 3600)
            if meeting_id not in kept_names:
                audio += round(duration * AUDIO_BYTES_PER_SECOND)
    pages = pages_freed_bytes(root)
    return {
        "meetings": meetings,
        "recorded_s": round(seconds, 1),
        "video_bytes": video,
        "audio_bytes": audio,
        "pages_bytes": pages,
        "total_bytes": video + audio + pages,
    }


def saved_bytes(root: Path, video_bytes_per_hour: float = VIDEO_BYTES_PER_HOUR) -> int:
    """Video never recorded, audio no longer on disk, and pages Space saver removed."""
    return space_breakdown(root, video_bytes_per_hour)["total_bytes"]


def token_cost_usd(
    read: int,
    written: int,
    unsplit: int,
    *,
    input_usd_per_million: float,
    output_usd_per_million: float,
) -> float:
    """What these tokens would cost at a hosted model's list price.

    Tokens counted before Yarn kept read and written apart are priced as
    read, the cheaper of the two. Summaries are nearly all reading, so that
    is close, and where it is wrong it understates.
    """
    return (
        (read + unsplit) * input_usd_per_million
        + written * output_usd_per_million
    ) / 1_000_000


def format_usd(amount: float) -> str:
    if amount < 0.01:
        return "under $0.01"
    if amount < 100:
        return f"${amount:,.2f}"
    return f"${amount:,.0f}"


_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def earliest_recording(root: Path) -> date | None:
    """The day of the oldest recording or transcript still in the archive.

    The id starts with the day the recording began. File dates move when
    Yarn copies a meeting, so they are not the start.
    """
    found: date | None = None
    for folder in (root / "transcripts", root / "recordings"):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            match = _STAMP.match(path.name)
            if match is None:
                continue
            try:
                day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            if found is None or day < found:
                found = day
    return found


def months_between(start: date, today: date) -> int:
    """Whole months from start to today. A part month does not count.

    Rounding up would bill a paid service for a month Yarn had barely started.
    """
    if today <= start:
        return 0
    months = (today.year - start.year) * 12 + (today.month - start.month)
    if today.day < start.day:
        months -= 1
    return max(0, months)


def _plain_usd(amount: float) -> str:
    if float(amount) == int(amount):
        return f"${int(amount):,}"
    return format_usd(amount)


def subscription_sentence(start: date, today: date, monthly: float) -> str:
    """What a paid meeting product would have charged since the first recording."""
    when = f"{start.day} {start.strftime('%B %Y')}"
    rate = _plain_usd(monthly)
    months = months_between(start, today)
    if months <= 0:
        return (
            f"A paid meeting service is about {rate} a month. "
            f"The first recording was {when}, less than a month ago, "
            f"so that is under {rate}."
        )
    unit = "month" if months == 1 else "months"
    total = _plain_usd(months * monthly)
    return (
        f"A paid meeting service is about {rate} a month. "
        f"Since {when}, about {months} {unit}, that is about {total}."
    )


def _hours(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        minutes = max(1, round(seconds / 60))
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    if hours < 10:
        return f"{hours:.1f} hours"
    return f"{round(hours):,} hours"


def report(cfg, today: date | None = None) -> dict:
    """Everything the Dock footer shows about savings, with its explanations."""
    from .summarize import saved_token_split

    savings = cfg.savings
    today = today or date.today()
    read, written, unsplit = saved_token_split(cfg.root)
    cost = token_cost_usd(
        read,
        written,
        unsplit,
        input_usd_per_million=savings.input_usd_per_million,
        output_usd_per_million=savings.output_usd_per_million,
    )
    total_tokens = read + written + unsplit
    space = space_breakdown(cfg.root, savings.video_gb_per_hour * 1_000_000_000)

    if total_tokens:
        tokens_tip = (
            f"Approx. {format_usd(cost)} if this had run on a hosted AI model.\n"
            f"Priced as {savings.reference_model}: "
            f"${savings.input_usd_per_million:g} per million tokens read, "
            f"${savings.output_usd_per_million:g} per million written.\n"
            "List prices change. On your Mac it cost nothing, and nothing left it."
        )
    else:
        tokens_tip = (
            "Tokens are counted when Yarn writes a summary, a brief or an answer "
            "on this Mac. Nothing yet."
        )
    started = earliest_recording(cfg.root)
    if started is not None and savings.subscription_usd_per_month > 0:
        tokens_tip += "\n" + subscription_sentence(
            started, today, savings.subscription_usd_per_month
        )

    if space["meetings"]:
        lines = [
            f"Compared with keeping {savings.video_label} of the same "
            f"{_hours(space['recorded_s'])} of meetings, at about "
            f"{savings.video_gb_per_hour:g} GB an hour: "
            f"{format_bytes(space['video_bytes'])}."
        ]
        if space["audio_bytes"]:
            lines.append(
                f"Audio already removed after its transcript was made: "
                f"{format_bytes(space['audio_bytes'])}."
            )
        if space["pages_bytes"]:
            lines.append(
                f"Pages removed by Space saver: {format_bytes(space['pages_bytes'])}."
            )
        lines.append("The video figure is an estimate. Yarn never records video.")
        space_tip = "\n".join(lines)
    else:
        space_tip = "Counted once there is a meeting. Yarn never records video."

    return {
        "tokens": {
            "total": total_tokens,
            "read": read,
            "written": written,
            "unsplit": unsplit,
            "cost_usd": round(cost, 4),
            "reference_model": savings.reference_model,
            "tooltip": tokens_tip,
        },
        "space": {
            **space,
            "label": format_saved(space["total_bytes"]),
            "tooltip": space_tip,
        },
    }
