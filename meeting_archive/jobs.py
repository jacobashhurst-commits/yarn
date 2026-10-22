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
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .records import write_json_atomic


def _pid_alive(pid: int) -> bool:
    """True when `pid` still refers to a running process on this machine."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, but we cannot signal it - treat as alive so we do not steal.
        return True
    return True


def _other_watcher_pids() -> list[int]:
    """PIDs of `ma watch` processes that are not this one.

    Used for job records written before worker_pid existed: if another watcher
    is alive, assume it still owns the RUNNING job and leave it alone.
    """
    import subprocess

    try:
        out = subprocess.check_output(["pgrep", "-f", r"/ma watch|ma watch"], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    pids: list[int] = []
    self_pid = os.getpid()
    for line in out.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != self_pid and _pid_alive(pid):
            pids.append(pid)
    return pids

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
    # PID of the watcher currently inside process(). Empty / dead means the
    # job was interrupted by sleep-kill, reboot, or a crash and can resume.
    worker_pid: int = 0
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
            "worker_pid": int(self.worker_pid or 0),
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
            worker_pid=int(d.get("worker_pid") or 0),
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


def whisper_checkpoint_path(cfg: Config, meeting_id: str) -> Path:
    """Durable Whisper segments written before the long diarize stage.

    Stored under jobs/ (not processing/) so reclaiming a stranded meeting
    back into the inbox does not misplace the checkpoint.
    """
    return cfg.jobs_dir / f"{meeting_id}__segments.json"


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
    job.worker_pid = os.getpid()
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
    job.worker_pid = 0
    if title:
        job.title = title
    save(cfg, job)
    return job


def fail(cfg: Config, job: Job, error: str, stage: str | None = None) -> Job:
    if stage:
        finish_stage(cfg, job, stage, status=STAGE_FAILED, error=error)
    job.state = FAILED
    job.worker_pid = 0
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
        if path.name.endswith("__segments.json"):
            continue
        try:
            jobs.append(Job.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError):
            continue
    return jobs


def queued(cfg: Config) -> list[Job]:
    return [job for job in list_jobs(cfg) if job.state in (QUEUED, RUNNING, PAUSED)]


def reconcile(cfg: Config) -> list[str]:
    """Mark queued jobs complete when their meeting is already transcribed.

    Job records predate stage tracking, and a job written before that existed
    sits at state="queued" with every stage "pending" forever -- even though
    the transcript was written and indexed days ago. The queue then reports
    work that does not exist, which is worse than useless: it trains you to
    ignore the one display that is supposed to tell you something is stuck.

    Only QUEUED jobs are touched. A RUNNING job may be genuinely in flight
    with its transcript already on disk and its index still to come, and
    `recover_stale` is what owns that case. The transcript on disk is the
    source of truth here, exactly as it is everywhere else.

    Returns the meeting ids repaired.
    """
    repaired: list[str] = []
    for job in list_jobs(cfg):
        if job.state != QUEUED:
            continue
        if not cfg.transcript_json(job.meeting_id).exists():
            continue  # genuinely outstanding -- leave it alone
        for name in STAGE_ORDER:
            stage = job.stages.get(name)
            if stage is not None and stage.status == PENDING:
                # The work happened; which stages ran is no longer knowable,
                # so say "done" rather than invent durations.
                stage.status = DONE
        complete(cfg, job)
        repaired.append(job.meeting_id)
    return repaired


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

    A live worker_pid means process() is still inside that meeting (including
    after the lid closed - macOS freezes the process, it does not die). Those
    are left alone. A dead pid, or a missing one from older job files, means
    the machine rebooted or the watcher was killed: requeue, keep any Whisper
    checkpoint, and put the manifest back in the inbox so the next sweep
    picks up at the unfinished stage rather than starting over.
    """
    recovered = 0
    for job in list_jobs(cfg):
        transcript_path = cfg.transcript_json(job.meeting_id)
        processing_manifest = cfg.processing_dir / f"{job.meeting_id}.json"
        # A job that never left "queued" even though the transcript exists is
        # leftover metadata, not work. Showing it as queued in the library
        # looks like the meeting never processed. Skip when the audio is still
        # mid-pipeline in processing/ - that meeting is genuinely unfinished.
        if job.state == QUEUED and transcript_path.exists() and not processing_manifest.exists():
            title = job.title
            try:
                from .records import Transcript

                title = Transcript.read(transcript_path).title or title
            except (OSError, ValueError):
                pass
            complete(cfg, job, title=title)
            recovered += 1
            continue
        if job.state != RUNNING:
            continue
        if job.worker_pid:
            if _pid_alive(job.worker_pid):
                continue
        elif _other_watcher_pids():
            # Pre-worker_pid job still owned by a living watcher (e.g. after
            # sleep). Do not steal its files out from under diarize.
            continue
        failed_manifest = cfg.failed_dir / f"{job.meeting_id}.json"
        if failed_manifest.exists():
            fail(cfg, job, "interrupted and quarantined")
        else:
            _requeue_interrupted(cfg, job, transcript_path)
        recovered += 1
    # QUEUED jobs stranded in processing/ never reach sweep_inbox otherwise.
    recovered += reclaim_processing(cfg)
    return recovered


def whisper_checkpoint_is_complete(path: Path) -> bool:
    """True when Whisper finished every channel, not merely saved a slice.

    A checkpoint written part-way through a long recording is how a closed lid
    or a reboot resumes. Treating that file as "transcription done" would
    drop the rest of the meeting.
    """
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if data.get("partial"):
        return False
    channels = data.get("channels") or {}
    if isinstance(channels, dict) and channels:
        for payload in channels.values():
            if not isinstance(payload, dict) or not payload.get("complete"):
                return False
    return "segments" in data


def _requeue_interrupted(cfg: Config, job: Job, transcript_path: Path) -> None:
    """Reset in-flight stages and mark Whisper done when it actually finished."""
    checkpoint = whisper_checkpoint_path(cfg, job.meeting_id)
    if whisper_checkpoint_is_complete(checkpoint):
        if not job.stage_done("transcribe"):
            finish_stage(
                cfg, job, "transcribe", status=DONE, detail="resumed from checkpoint"
            )
    elif transcript_path.exists() and not job.stage_done("transcribe"):
        finish_stage(cfg, job, "transcribe", status=DONE, detail="found on disk")

    for name in STAGE_ORDER:
        stage = job.stages[name]
        if stage.status == RUNNING:
            stage.status = PENDING
            stage.started_at = ""
            stage.ended_at = ""
            stage.duration_s = 0.0
            stage.error = ""
            stage.detail = ""
    job.state = QUEUED
    job.worker_pid = 0
    job.current_stage = job.next_pending() or "transcribe"
    job.error = "requeued after interruption"
    save(cfg, job)


def reclaim_processing(cfg: Config) -> int:
    """Move stranded processing/ manifests back to the inbox so they resume.

    process() only claims from the inbox. After a crash the audio sits in
    processing/ with a QUEUED job and nothing ever picks it up again.
    """
    import logging
    import shutil

    log = logging.getLogger(__name__)
    if not cfg.processing_dir.exists():
        return 0
    moved = 0
    for manifest_path in sorted(cfg.processing_dir.glob("*.json")):
        if manifest_path.name.endswith(".partial"):
            continue
        meeting_id = manifest_path.stem
        job = load(cfg, meeting_id)
        if job is None:
            continue
        if job.state != QUEUED:
            continue
        if job.worker_pid and _pid_alive(job.worker_pid):
            continue
        try:
            from .records import Manifest

            manifest = Manifest.read(manifest_path)
        except (OSError, ValueError):
            continue
        names = list(manifest.channels.values())
        if manifest.raw_file:
            names.append(manifest.raw_file)
        for name in names:
            src = cfg.processing_dir / name
            if src.exists():
                shutil.move(str(src), str(cfg.inbox_dir / name))
        shutil.move(str(manifest_path), str(cfg.inbox_dir / manifest_path.name))
        log.info("reclaimed interrupted meeting=%s back into the inbox", meeting_id)
        moved += 1
    return moved
