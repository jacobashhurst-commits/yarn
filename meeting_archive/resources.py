"""Capture-always-wins resource checks.

The recorder is a separate process. This module only answers: should the
watcher start another heavy AI stage right now?
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
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


# Fast and Accurate only. A name that merely contains "4b" is some other
# model the user installed, and must not be rewritten into qwen3.5:4b.
_BUILTIN_SUMMARY_NAMES = {
    "",
    QUALITY_FAST,
    QUALITY_ACCURATE,
    "4b",
    "9b",
    SUMMARY_MODEL_LOW_MEMORY,
    SUMMARY_MODEL_FULL,
}
_OLLAMA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def is_builtin_summary_model(model: str) -> bool:
    """True for Fast, Accurate, and the two qwen models Yarn ships."""
    return str(model or "").strip().lower() in _BUILTIN_SUMMARY_NAMES


def selectable_ollama_models(installed: list[str]) -> list[str]:
    """Models already on this Mac, apart from Fast, Accurate, and embed models.

    Embed models answer a search, not a summary. The two Yarn models stay
    on the Fast and Accurate choices so this list cannot replace them.
    """
    known = {SUMMARY_MODEL_FULL.lower(), SUMMARY_MODEL_LOW_MEMORY.lower()}
    chosen: list[str] = []
    for name in installed:
        text = str(name or "").strip()
        lowered = text.lower()
        if not text or lowered in known or "embed" in lowered:
            continue
        if text not in chosen:
            chosen.append(text)
    return chosen


def stored_summary_model(
    value: str, installed: list[str], *, allow_accurate: bool
) -> tuple[str, str]:
    """The model name to save, and an error when the choice is refused.

    Fast and Accurate go through model_for_quality, including the 8 GB
    refusal of Accurate. Any other name is kept only when Ollama already
    has it. Nothing here downloads a model.
    """
    raw = str(value or "").strip()
    if is_builtin_summary_model(raw):
        model = model_for_quality(raw)
        if model == SUMMARY_MODEL_FULL and not allow_accurate:
            return "", "this Mac can only run Fast summaries"
        return model, ""
    if not _OLLAMA_NAME.match(raw) or "embed" in raw.lower():
        return "", "that model cannot write a Yarn summary"
    have = {str(name).lower(): str(name) for name in installed}
    found = have.get(raw.lower())
    if not found or found.lower() in {
        SUMMARY_MODEL_FULL.lower(),
        SUMMARY_MODEL_LOW_MEMORY.lower(),
    }:
        return "", "that model is not installed in Ollama on this Mac"
    return found, ""


def ollama_model_names(host: str) -> list[str]:
    """Models the running Ollama is serving. A closed Ollama returns nothing.

    `ollama list` starts the daemon. This asks the one that is already up.
    """
    url = str(host or "").rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=0.4) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return []
    names: list[str] = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def tidy_model_for(cfg: Config) -> str:
    """The model that proofreads a transcript.

    A summary may use another model already on this Mac. Tidy-up stays on
    Fast or Accurate: the one chosen there, or that choice remembered from
    before a different summary model was picked.
    """
    current = str(cfg.summary.model or "")
    if is_builtin_summary_model(current) and current.strip():
        model = model_for_quality(current)
    else:
        remembered = str(getattr(cfg.summary, "builtin", "") or "")
        if is_builtin_summary_model(remembered) and remembered.strip():
            model = model_for_quality(remembered)
        else:
            model = summary_model_choice()[0]
    if model == SUMMARY_MODEL_FULL and not can_run_accurate():
        return SUMMARY_MODEL_LOW_MEMORY
    return model


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
