"""Local per-stage performance history.

Append-only JSONL under `{archive}/logs/performance.jsonl`. Nothing leaves
the machine.
"""

from __future__ import annotations

import json
import os
import platform
from datetime import datetime
from pathlib import Path

from .config import Config


def hardware_label() -> str:
    machine = platform.machine()
    system = platform.system()
    try:
        from .resources import unified_memory_gb

        mem = unified_memory_gb()
    except Exception:
        mem = 0.0
    mem_s = f"{mem:.0f}GB" if mem else "?"
    return f"{system} {machine} {mem_s}"


def record(
    cfg: Config,
    *,
    meeting_id: str,
    stage: str,
    engine: str = "",
    model: str = "",
    meeting_duration_s: float = 0.0,
    wall_s: float = 0.0,
    success: bool = True,
    error: str = "",
    extra: dict | None = None,
) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.logs_dir / "performance.jsonl"
    row = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "meeting_id": meeting_id,
        "stage": stage,
        "engine": engine,
        "model": model,
        "hardware": hardware_label(),
        "meeting_duration_s": round(meeting_duration_s, 3),
        "wall_s": round(wall_s, 3),
        "realtime_factor": (
            round(wall_s / meeting_duration_s, 3) if meeting_duration_s else 0.0
        ),
        "rss_mb": _rss_mb(),
        "success": success,
        "error": error,
    }
    if extra:
        row.update(extra)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rss_mb() -> float:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux reports kilobytes.
        if usage > 10_000_000:
            return round(usage / (1024 * 1024), 1)
        return round(usage / 1024, 1)
    except Exception:
        return 0.0
