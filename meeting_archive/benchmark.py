"""Local benchmark harness. Never commits confidential audio.

Looks in `benchmark_audio/` at the repo root (gitignored). Each file is timed
through whichever engines the config enables. Quality notes are left for a
human; this does not invent a score.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import Config
from .metrics import hardware_label, record


def repo_benchmark_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "benchmark_audio"


def run_benchmark(cfg: Config) -> int:
    folder = repo_benchmark_dir()
    print(f"hardware: {hardware_label()}")
    print(f"corpus:   {folder}")
    if not folder.is_dir():
        print(
            "no benchmark_audio/ directory. Add gitignored representative "
            "recordings there, then re-run.",
            file=sys.stderr,
        )
        return 1
    files = sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in {".wav", ".m4a", ".mp3", ".flac"}
    )
    if not files:
        print("benchmark_audio/ is empty", file=sys.stderr)
        return 1
    print("files:")
    for path in files:
        print(f"  {path.name}  {path.stat().st_size} bytes")
        record(
            cfg,
            meeting_id=path.stem,
            stage="benchmark-list",
            engine=cfg.whisper.engine,
            model=cfg.whisper.model,
            extra={"file": path.name, "bytes": path.stat().st_size},
        )
    print(
        "Timing a full transcribe/diarize pass is opt-in: drop files here and "
        "process them with `ma import-audio` so the normal pipeline records "
        "stage metrics in logs/performance.jsonl."
    )
    return 0
