"""Persistent processing jobs.

A recording is valid the moment finalize writes the inbox manifest. Everything
after that is a job that can wait, pause, fail a later stage, and resume after
a reboot without re-running Whisper.

Jobs are JSON files under `{archive}/jobs/`. They are the queue, not a second
source of truth: transcripts still live in `transcripts/`, audio in
`recordings/`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .records import write_json_atomic

# Pipeline order. Optional stages are skipped when their feature is off, and
# that skip is recorded so a later toggle-on can still run them.
STAGE_ORDER = (
    "transcribe",
    "diarize",
    "recognise",
    "cleanup",
    "summarize",
    "index",
)

QUEUED = "queued"
RUNNING = "running"
PAUSED = "paused"
COMPLETE = "complete"
FAILED = "failed"

PENDING = "pending"
DONE = "done"
SKIPPED = "skipped"
STAGE_FAILED = "failed"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class StageResult:
    name: str
    status: str = PENDING
    started_at: str = ""
    ended_at: str = ""
    duration_s: float = 0.0
    error: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.duration_s, 3),
            "error": self.error,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StageResult":
        return cls(
            name=d.get("name", ""),
            status=d.get("status", PENDING),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            duration_s=float(d.get("duration_s") or 0.0),
            error=d.get("error", ""),
            detail=d.get("detail", ""),
        )


@dataclass
class Job:
    meeting_id: str
    source: str = "native"  # native | import-audio | import-transcript
    state: str = QUEUED
    current_stage: str = "transcribe"
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    duration_s: float = 0.0
    title: str = ""
    stages: dict[str, StageResult] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now()
        if not self.updated_at:
            self.updated_at = self.created_at
        for name in STAGE_ORDER:
            self.stages.setdefault(name, StageResult(name=name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "source": self.source,
            "state": self.state,
            "current_stage": self.current_stage,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "duration_s": round(self.duration_s, 3),
            "title": self.title,
            "stages": {name: self.stages[name].to_dict() for name in STAGE_ORDER},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        stages = {
            name: StageResult.from_dict(payload)
            for name, payload in (d.get("stages") or {}).items()
        }
        job = cls(
            meeting_id=d["meeting_id"],
            source=d.get("source", "native"),
            state=d.get("state", QUEUED),
            current_stage=d.get("current_stage", "transcribe"),
            error=d.get("error", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            duration_s=float(d.get("duration_s") or 0.0),
            title=d.get("title", ""),
            stages=stages,
        )
        return job

    def stage_done(self, name: str) -> bool:
        stage = self.stages.get(name)
        return bool(stage and stage.status in (DONE, SKIPPED))

    def next_pending(self) -> str | None:
        for name in STAGE_ORDER:
            stage = self.stages[name]
            if stage.status in (PENDING, RUNNING, STAGE_FAILED):
                return name
        return None


def job_path(cfg: Config, meeting_id: str) -> Path:
    return cfg.jobs_dir / f"{meeting_id}.json"


def load(cfg: Config, meeting_id: str) -> Job | None:
    path = job_path(cfg, meeting_id)
    if not path.exists():
        return None
    try:
        return Job.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def save(cfg: Config, job: Job) -> Path:
    job.updated_at = _now()
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
    return write_json_atomic(job_path(cfg, job.meeting_id), job.to_dict())


def enqueue(
    cfg: Config,
    meeting_id: str,
    *,
    source: str = "native",
    duration_s: float = 0.0,
    title: str = "",
) -> Job:
    """Create a queued job, or return the existing one unharmed.

    Finalize calls this. Re-finalizing the same id must not wipe a job that
    already transcribed.
    """
    existing = load(cfg, meeting_id)
    if existing and existing.state != FAILED:
        return existing
    job = Job(
        meeting_id=meeting_id,
        source=source,
        state=QUEUED,
        duration_s=duration_s,
        title=title,
    )
    save(cfg, job)
    return job


def begin_stage(cfg: Config, job: Job, name: str) -> Job:
    stage = job.stages[name]
    stage.status = RUNNING
    stage.started_at = _now()
    stage.error = ""
    job.state = RUNNING
    job.current_stage = name
    save(cfg, job)
    return job


def finish_stage(
    cfg: Config,
    job: Job,
    name: str,
    *,
    status: str = DONE,
    error: str = "",
    detail: str = "",
    duration_s: float = 0.0,
) -> Job:
    stage = job.stages[name]
    stage.status = status
    stage.ended_at = _now()
    stage.error = error
    stage.detail = detail
    if duration_s:
        stage.duration_s = duration_s
    elif stage.started_at:
        try:
            start = datetime.fromisoformat(stage.started_at)
            stage.duration_s = max(0.0, (datetime.now() - start).total_seconds())
        except ValueError:
            pass
    save(cfg, job)
    return job


def complete(cfg: Config, job: Job, title: str = "") -> Job:
    job.state = COMPLETE
    job.current_stage = "complete"
    job.error = ""
    if title:
        job.title = title
    save(cfg, job)
    return job


def fail(cfg: Config, job: Job, error: str, stage: str | None = None) -> Job:
    if stage:
        finish_stage(cfg, job, stage, status=STAGE_FAILED, error=error)
    job.state = FAILED
    job.error = error
    save(cfg, job)
    return job


def list_jobs(cfg: Config) -> list[Job]:
    if not cfg.jobs_dir.exists():
        return []
    jobs: list[Job] = []
    for path in sorted(cfg.jobs_dir.glob("*.json")):
        if path.name.endswith(".partial"):
            continue
        try:
            jobs.append(Job.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError):
            continue
    return jobs


def queued(cfg: Config) -> list[Job]:
    return [job for job in list_jobs(cfg) if job.state in (QUEUED, RUNNING, PAUSED)]


CONTROL_PAUSED = "paused"
CONTROL_REASON = "reason"


def control_path(cfg: Config) -> Path:
    return cfg.root / ".processing-control.json"


def read_control(cfg: Config) -> dict:
    path = control_path(cfg)
    if not path.exists():
        return {CONTROL_PAUSED: False, CONTROL_REASON: ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {CONTROL_PAUSED: False, CONTROL_REASON: ""}
    return {
        CONTROL_PAUSED: bool(data.get(CONTROL_PAUSED)),
        CONTROL_REASON: str(data.get(CONTROL_REASON) or ""),
    }


def set_paused(cfg: Config, paused: bool, reason: str = "") -> dict:
    payload = {CONTROL_PAUSED: paused, CONTROL_REASON: reason}
    write_json_atomic(control_path(cfg), payload)
    return payload


def is_paused(cfg: Config) -> bool:
    return bool(read_control(cfg).get(CONTROL_PAUSED))


def recover_stale(cfg: Config) -> int:
    """Jobs left RUNNING after a crash become queued (or failed) again.

    A transcript already on disk is kept; Whisper is not repeated just because
    the watcher died during a later stage.
    """
    recovered = 0
    for job in list_jobs(cfg):
        if job.state != RUNNING:
            continue
        failed_manifest = cfg.failed_dir / f"{job.meeting_id}.json"
        if failed_manifest.exists():
            fail(cfg, job, "interrupted and quarantined")
        else:
            if cfg.transcript_json(job.meeting_id).exists() and not job.stage_done(
                "transcribe"
            ):
                finish_stage(cfg, job, "transcribe", status=DONE, detail="found on disk")
            job.state = QUEUED
            job.error = "requeued after interruption"
            save(cfg, job)
        recovered += 1
    return recovered
