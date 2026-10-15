"""Capture-always-wins resource checks.

The recorder is a separate process. This module only answers: should the
watcher start another heavy AI stage right now?
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import Config

NORMAL = "normal"
WARN = "warn"
CRITICAL = "critical"

# qwen3.5:9b needs ~7.1 GiB free. An 8 GB Neo with the OS and a few apps
# only has ~4.8 GiB, so Ollama 500s. Evening used to paper over this by
# waiting until other apps were closed. after_meeting on a used laptop does not.
SUMMARY_MODEL_FULL = "qwen3.5:9b"
SUMMARY_MODEL_LOW_MEMORY = "qwen3.5:4b"
QUALITY_FAST = "fast"
QUALITY_ACCURATE = "accurate"


def can_run_accurate(ram_gb: float | None = None) -> bool:
    size = unified_memory_gb() if ram_gb is None else ram_gb
    return size <= 0 or size > 10.0


def model_for_quality(quality: str) -> str:
    if str(quality).strip().lower() in {
        QUALITY_ACCURATE,
        "9b",
        SUMMARY_MODEL_FULL,
    }:
        return SUMMARY_MODEL_FULL
    return SUMMARY_MODEL_LOW_MEMORY


def quality_for_model(model: str) -> str:
    if model_for_quality(model) == SUMMARY_MODEL_FULL:
        return QUALITY_ACCURATE
    return QUALITY_FAST


def summary_model_choice(ram_gb: float | None = None) -> tuple[str, str]:
    """(model, user-facing reason). Reason is empty on a high-memory Mac."""
    size = unified_memory_gb() if ram_gb is None else ram_gb
    if 0 < size <= 10.0:
        return (
            SUMMARY_MODEL_LOW_MEMORY,
            (
                f"This Mac has {size:.0f} GB of memory, so Yarn will use "
                f"{SUMMARY_MODEL_LOW_MEMORY} for written summaries. "
                f"{SUMMARY_MODEL_FULL} needs about 8 GB free and will not "
                "fit while you are using the computer."
            ),
        )
    return SUMMARY_MODEL_FULL, ""


def apply_fit_summary_model(cfg: Config) -> str:
    """Downgrade the default 9B model on a low-memory Mac. Returns the notice."""
    model, notice = summary_model_choice()
    if not notice or cfg.summary.model == model:
        return ""
    if cfg.summary.model != SUMMARY_MODEL_FULL:
        return ""
    cfg.summary.model = model
    from .config import write_setting

    write_setting(cfg, "summary_model", model)
    return notice


def recording_active(cfg: Config) -> bool:
    """True when bin/rec has a live capture pid in `.recording`."""
    path = cfg.root / ".recording"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines:
        return False
    try:
        pid = int(lines[0])
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def unified_memory_gb() -> float:
    """Physical RAM in GiB, or 0 if we cannot tell."""
    try:
        raw = subprocess.check_output(
            ["sysctl", "-n", "hw.memsize"],
            text=True,
            timeout=1,
        ).strip()
        return int(raw) / (1024 ** 3)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def is_low_memory_machine(threshold_gb: float = 10.0) -> bool:
    size = unified_memory_gb()
    return 0 < size <= threshold_gb


def memory_pressure() -> str:
    """green/yellow/red-style pressure, mapped to normal/warn/critical.

    `memory_pressure` is macOS-specific. Anything unreadable is treated as
    normal so a missing diagnostic can never freeze the queue.
    """
    try:
        proc = subprocess.run(
            ["memory_pressure"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return NORMAL
    text = (proc.stdout or "") + (proc.stderr or "")
    lowered = text.lower()
    if "red" in lowered or "critical" in lowered:
        return CRITICAL
    if "yellow" in lowered or "warn" in lowered:
        return WARN
    return NORMAL


def should_defer_heavy_stage(cfg: Config) -> bool:
    """True when starting Whisper/Ollama now would risk the live capture.

    An already-running stage is left alone: this only gates *starting* work.
    Recording alone is not enough; we wait for actual memory pressure so a
    32 GB Mac can keep transcribing during the next meeting.
    """
    if not cfg.processing.pause_on_pressure:
        return False
    if not recording_active(cfg):
        return False
    return memory_pressure() in (WARN, CRITICAL)


def nice_background() -> None:
    """Lower this process so capture stays scheduled. Harmless if we can't."""
    try:
        os.nice(10)
    except OSError:
        pass
