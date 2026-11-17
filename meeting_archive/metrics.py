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


_FACTOR_CACHE: dict[str, tuple[float, float]] = {}


def typical_realtime_factor(cfg: Config, stage: str) -> float:
    """Median wall/recording ratio for a stage on this machine, 0 if unknown.

    Only recordings over 10 minutes count: short tests are dominated by model
    load time and would make long meetings look faster than they are.
    """
    path = cfg.logs_dir / "performance.jsonl"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    cached = _FACTOR_CACHE.get(stage)
    if cached and cached[0] == mtime:
        return cached[1]
    factors: list[float] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if (
                    row.get("stage") == stage
                    and row.get("success")
                    and float(row.get("meeting_duration_s") or 0) > 600
                    and float(row.get("realtime_factor") or 0) > 0
                ):
                    factors.append(float(row["realtime_factor"]))
    except OSError:
        return 0.0
    factors = sorted(factors[-15:])
    value = factors[len(factors) // 2] if factors else 0.0
    _FACTOR_CACHE[stage] = (mtime, value)
    return value


# Where an estimate starts before this Mac has any history: (seconds of work
# per second of meeting, fixed seconds to load the model). They are guesses
# from a handful of machines and are replaced by this Mac's own timings as
# soon as there are any. The first few estimates are labelled rough.
STAGE_PRIORS: dict[str, tuple[float, float]] = {
    "transcribe": (0.30, 8.0),
    # Voice separation is the long step. 0.9 promised a finish that did not
    # come: a 3.5 minute meeting on this Mac took about 1.5x to separate.
    "diarize": (1.40, 8.0),
    "cleanup": (0.25, 15.0),
    "summarize": (0.12, 20.0),
    "index": (0.0, 10.0),
}
# A recording has to be this long before it says anything about speed. Under
# it, the time is nearly all loading the model: a 2.5s test recording took
# 12s to transcribe, which read as "4.8 times real time" and would have
# promised five hours for a one-hour meeting.
_RATE_FROM_S = 120.0
_OVERHEAD_UNDER_S = 300.0

_MODEL_CACHE: dict[str, tuple[float, tuple[float, float, bool]]] = {}


def stage_model(cfg: Config, stage: str) -> tuple[float, float, bool]:
    """(rate, overhead_s, learned) for one stage on this Mac.

    A stage takes a fixed moment to load its model plus time in proportion
    to the meeting. The two are learned separately: the rate only from
    recordings long enough to show it, the fixed part from the short ones.
    `learned` is False while either number is still the built-in guess.
    """
    prior_rate, prior_overhead = STAGE_PRIORS.get(stage, (0.0, 0.0))
    path = cfg.logs_dir / "performance.jsonl"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return prior_rate, prior_overhead, False
    cached = _MODEL_CACHE.get(f"{path}:{stage}")
    if cached and cached[0] == mtime:
        return cached[1]

    points: list[tuple[float, float]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row.get("stage") != stage or not row.get("success", True):
                        continue
                    duration = float(row.get("meeting_duration_s") or 0)
                    wall = float(row.get("wall_s") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
                if duration > 0 and wall > 0:
                    points.append((duration, wall))
    except OSError:
        return prior_rate, prior_overhead, False
    points = points[-30:]

    long_ones = [(d, w) for d, w in points if d >= _RATE_FROM_S]
    rate, learned_rate = prior_rate, False
    if long_ones:
        # Weighted towards the longest recordings, which measure rate best.
        rate = _median([w / d for d, w in sorted(long_ones)[-9:]])
        learned_rate = True
    short_ones = [(d, w) for d, w in points if d < _OVERHEAD_UNDER_S]
    overhead = prior_overhead
    if short_ones:
        overhead = min(180.0, _median([max(0.0, w - rate * d) for d, w in short_ones]))
    model = (max(0.0, rate), max(0.0, overhead), learned_rate)
    _MODEL_CACHE[f"{path}:{stage}"] = (mtime, model)
    return model


def expected_wall_s(cfg: Config, stage: str, meeting_s: float) -> tuple[float, bool]:
    """(seconds this stage should take for a meeting this long, learned?)."""
    if meeting_s <= 0:
        return 0.0, False
    rate, overhead, learned = stage_model(cfg, stage)
    if rate <= 0 and overhead <= 0:
        return 0.0, False
    return overhead + rate * meeting_s, learned


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


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
