"""Yarn V2: queue, retention settings, imports, capture-under-load, speakers."""

from __future__ import annotations

import os
import threading
import time
import wave
from pathlib import Path

import pytest

from meeting_archive import jobs, library, pipeline, resources, retention, watcher
from meeting_archive.capture import WavWriter
from meeting_archive.cleanup import _parse_cleaned
from meeting_archive.config import Config, retention_days, write_setting
from meeting_archive.imports import ImportFailed, import_transcript
from meeting_archive.models import MIC, ROOM, Segment, SpeakerProfile, Turn
from meeting_archive.quips import profile_speakers
from meeting_archive.records import Manifest, Transcript
from meeting_archive.speakers import KnownSpeaker, suggest_matches
from tests.test_pipeline import FakeStore, FakeTranscriber, make_wav, write_transcript

FFMPEG = __import__("shutil").which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(root=tmp_path / "archive")
    config.audio.device_index = 0
    config.ensure_dirs()
    return config


def _inbox_meeting(cfg: Config, meeting_id: str, text: str = "Hello there.") -> Path:
    wav = cfg.inbox_dir / f"{meeting_id}__room.wav"
    wav.write_bytes(b"RIFF____WAVEfmt ")
    Manifest(
        meeting_id=meeting_id,
        channels={ROOM: wav.name},
        duration_s=2.0,
        source="native",
    ).write(cfg.inbox_dir / f"{meeting_id}.json")
    return cfg.inbox_dir / f"{meeting_id}.json"


@needs_ffmpeg
def test_finalize_enqueues_a_job(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-17T09-00-00__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw, mode="room")
    job = jobs.load(cfg, manifest.meeting_id)
    assert job is not None
    assert job.state == jobs.QUEUED
    assert job.current_stage == "transcribe"


@needs_ffmpeg
def test_process_completes_job_and_does_not_block_a_second_inbox_item(cfg):
    first = pipeline.finalize(
        cfg,
        make_wav(cfg.in_progress_dir / "2026-09-17T09-00-00__raw.wav", [True, False, False]),
        mode="room",
    )
    second = pipeline.finalize(
        cfg,
        make_wav(cfg.in_progress_dir / "2026-09-17T10-00-00__raw.wav", [True, False, False]),
        mode="room",
    )
    transcriber = FakeTranscriber({ROOM: [Segment(ROOM, 0.0, 1.0, "First meeting.")]})
    pipeline.process(
        cfg, cfg.inbox_dir / f"{first.meeting_id}.json", transcriber, FakeStore()
    )

    job_a = jobs.load(cfg, first.meeting_id)
    assert job_a is not None
    assert job_a.state == jobs.COMPLETE
    assert job_a.stage_done("transcribe")
    assert job_a.stage_done("index")

    # Meeting B is still only recorded/queued. Recording would be a separate
    # process; here the inbox still holds it while A finished.
    assert (cfg.inbox_dir / f"{second.meeting_id}.json").exists()
    job_b = jobs.load(cfg, second.meeting_id)
    assert job_b is not None
    assert job_b.state == jobs.QUEUED


def test_sweep_inbox_leaves_work_when_paused(cfg):
    _inbox_meeting(cfg, "2026-09-17T11-00-00")
    jobs.set_paused(cfg, True, reason="test")
    assert watcher.sweep_inbox(cfg, FakeTranscriber(), FakeStore()) == 0
    assert (cfg.inbox_dir / "2026-09-17T11-00-00.json").exists()


def test_sweep_inbox_defers_under_capture_pressure(cfg, monkeypatch):
    _inbox_meeting(cfg, "2026-09-17T11-30-00")
    monkeypatch.setattr(resources, "should_defer_heavy_stage", lambda _cfg: True)
    assert watcher.sweep_inbox(cfg, FakeTranscriber(), FakeStore()) == 0
    assert (cfg.inbox_dir / "2026-09-17T11-30-00.json").exists()


def test_process_still_runs_if_a_recording_is_active(cfg, monkeypatch):
    """An in-flight process() is not aborted just because rec start was pressed."""
    monkeypatch.setattr(resources, "recording_active", lambda _cfg: True)
    _inbox_meeting(cfg, "2026-09-17T12-00-00")
    pipeline.process(
        cfg,
        cfg.inbox_dir / "2026-09-17T12-00-00.json",
        FakeTranscriber({ROOM: [Segment(ROOM, 0.0, 1.0, "Still processed.")]}),
        FakeStore(),
    )
    assert cfg.transcript_json("2026-09-17T12-00-00").exists()


def test_failed_process_marks_job_failed(cfg):
    _inbox_meeting(cfg, "2026-09-17T12-30-00")

    class Boom:
        def transcribe_file(self, path, channel, duration_s=0.0, progress=None):
            raise RuntimeError("model exploded")

    with pytest.raises(RuntimeError):
        pipeline.process(
            cfg, cfg.inbox_dir / "2026-09-17T12-30-00.json", Boom(), FakeStore()
        )
    job = jobs.load(cfg, "2026-09-17T12-30-00")
    assert job is not None
    assert job.state == jobs.FAILED
    assert job.stages["transcribe"].status == jobs.STAGE_FAILED


def test_recover_stale_requeues_a_running_job(cfg):
    job = jobs.enqueue(cfg, "2026-09-17T13-00-00")
    jobs.begin_stage(cfg, job, "transcribe")
    assert jobs.recover_stale(cfg) == 1
    loaded = jobs.load(cfg, "2026-09-17T13-00-00")
    assert loaded.state == jobs.QUEUED


def test_wav_writer_stays_accurate_under_cpu_load(tmp_path):
    stop = threading.Event()

    def burn() -> None:
        n = 1
        while not stop.is_set():
            n = (n * 1103515245 + 12345) & 0x7FFFFFFF
            n = (n * n) & 0x7FFFFFFF

    workers = [threading.Thread(target=burn, daemon=True) for _ in range(4)]
    for worker in workers:
        worker.start()
    try:
        writer = WavWriter(tmp_path / "busy.wav", channels=2, sample_rate=48000)
        frames = 48000  # one second at native capture rate
        chunk = 480
        frame = b"\x01\x00\x02\x00"  # two int16 samples
        for _ in range(frames // chunk):
            writer.write(frame * chunk)
        writer.close()
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=1)

    assert writer.frames == frames
    assert writer.seconds == pytest.approx(1.0)
    with wave.open(str(tmp_path / "busy.wav")) as handle:
        assert handle.getnframes() == frames
        assert handle.getframerate() == 48000


def test_process_under_load_writes_expected_transcript(cfg):
    """Heavy CPU during process() must not corrupt the durable transcript."""
    stop = threading.Event()

    def burn() -> None:
        n = 1
        while not stop.is_set():
            n = (n * 1103515245 + 12345) & 0x7FFFFFFF

    workers = [threading.Thread(target=burn, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        _inbox_meeting(cfg, "2026-09-17T14-00-00")
        pipeline.process(
            cfg,
            cfg.inbox_dir / "2026-09-17T14-00-00.json",
            FakeTranscriber({ROOM: [Segment(ROOM, 0.0, 1.5, "Keep this phrase.")]}),
            FakeStore(),
        )
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=1)

    transcript = Transcript.read(cfg.transcript_json("2026-09-17T14-00-00"))
    assert transcript.turns[0].text == "Keep this phrase."
    assert transcript.duration_s == pytest.approx(2.0)


def test_prune_never_policy_keeps_old_audio(cfg):
    cfg.audio_retention = "never"
    cfg.audio_retention_days = 0
    now = time.time()
    write_transcript(cfg, "2026-08-01T10-00-00", [Turn(MIC, 0.0, 1.0, "old")])
    path = cfg.audio_archive_dir / "2026-08-01T10-00-00__mic.wav"
    path.write_bytes(b"x")
    os.utime(path, (now - 40 * 86400,) * 2)
    result = retention.prune(cfg, now=now)
    assert result.deleted == []
    assert path.exists()


def test_prune_one_month_keeps_three_week_old_audio(cfg):
    cfg.audio_retention = "1_month"
    cfg.audio_retention_days = 30
    now = time.time()
    write_transcript(cfg, "2026-08-20T10-00-00", [Turn(MIC, 0.0, 1.0, "kept")])
    path = cfg.audio_archive_dir / "2026-08-20T10-00-00__mic.wav"
    path.write_bytes(b"x")
    os.utime(path, (now - 21 * 86400,) * 2)
    assert retention.prune(cfg, now=now).deleted == []
    os.utime(path, (now - 40 * 86400,) * 2)
    assert retention.prune(cfg, now=now).deleted == [path.name]


def test_prune_respects_keep_audio_override(cfg):
    now = time.time()
    write_transcript(cfg, "2026-08-01T10-00-00", [Turn(MIC, 0.0, 1.0, "keep")])
    retention.set_keep_audio(cfg, "2026-08-01T10-00-00", True)
    path = cfg.audio_archive_dir / "2026-08-01T10-00-00__mic.wav"
    path.write_bytes(b"x")
    os.utime(path, (now - 30 * 86400,) * 2)
    result = retention.prune(cfg, now=now)
    assert result.deleted == []
    assert result.kept_override == [path.name]
    assert path.exists()


def test_prune_keeps_audio_when_transcription_failed(cfg):
    now = time.time()
    path = cfg.audio_archive_dir / "2026-08-01T10-00-00__mic.wav"
    path.write_bytes(b"x")
    os.utime(path, (now - 30 * 86400,) * 2)
    result = retention.prune(cfg, now=now)
    assert result.deleted == []
    assert result.kept_no_transcript == [path.name]


def test_prune_proceeds_when_summary_is_deferred(cfg):
    now = time.time()
    transcript = write_transcript(cfg, "2026-08-01T10-00-00", [Turn(MIC, 0.0, 1.0, "said")])
    transcript.summary_error = "deferred: Ollama was not running"
    transcript.write(cfg.transcript_json(transcript.meeting_id))
    path = cfg.audio_archive_dir / "2026-08-01T10-00-00__mic.wav"
    path.write_bytes(b"x")
    os.utime(path, (now - 30 * 86400,) * 2)
    result = retention.prune(cfg, now=now)
    assert result.deleted == [path.name]


def test_delete_audio_now_refuses_without_transcript(cfg):
    path = cfg.audio_archive_dir / "2026-08-01T10-00-00__mic.wav"
    path.write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="no transcript"):
        retention.delete_meeting_audio(cfg, "2026-08-01T10-00-00")
    assert path.exists()


def test_delete_audio_now_removes_wav_keeps_transcript(cfg):
    write_transcript(cfg, "2026-08-01T10-00-00", [Turn(MIC, 0.0, 1.0, "said")])
    path = cfg.audio_archive_dir / "2026-08-01T10-00-00__mic.wav"
    path.write_bytes(b"x")
    removed = retention.delete_meeting_audio(cfg, "2026-08-01T10-00-00")
    assert removed == [path.name]
    assert not path.exists()
    assert cfg.transcript_json("2026-08-01T10-00-00").exists()
    assert pipeline.ingest(cfg, "2026-08-01T10-00-00", FakeStore()) >= 1


def test_retention_setting_overlay(cfg):
    write_setting(cfg, "audio_retention", "never")
    other = Config(root=cfg.root)
    from meeting_archive.config import _apply_settings_overlay

    _apply_settings_overlay(other)
    assert other.audio_retention == "never"
    assert other.audio_retention_days == 0
    assert retention_days("1_month") == 30


def test_import_transcript_preserves_names(cfg):
    src = cfg.root / "coworker.md"
    src.write_text("Adam: Hello.\nSpeaker 2: We should ship Friday.\n", encoding="utf-8")
    transcript = import_transcript(cfg, src, title="Imported")
    assert transcript.source == "import-transcript"
    labels = {turn.speaker for turn in transcript.turns}
    assert "Adam" in labels
    job = jobs.load(cfg, transcript.meeting_id)
    assert job is not None
    assert job.stage_done("transcribe")


def test_import_transcript_rejects_empty(cfg):
    src = cfg.root / "empty.txt"
    src.write_text("# just a heading\n", encoding="utf-8")
    with pytest.raises(ImportFailed):
        import_transcript(cfg, src)


def test_library_lists_jobs_and_transcripts(cfg):
    write_transcript(cfg, "2026-09-17T15-00-00", [Turn(ROOM, 0.0, 1.0, "Hi")])
    jobs.enqueue(cfg, "2026-09-17T16-00-00", title="Queued call")
    rows = {row.meeting_id: row for row in library.list_meetings(cfg)}
    assert rows["2026-09-17T15-00-00"].has_transcript
    assert rows["2026-09-17T16-00-00"].state == jobs.QUEUED
    html = library.render_html(cfg)
    assert html.exists()
    assert "Queued call" in html.read_text(encoding="utf-8")


def test_no_arbitrary_speaker_cap():
    turns = [
        Turn(ROOM, float(i), float(i) + 0.8, f"line {i}", speaker=f"room-{i + 1}")
        for i in range(8)
    ]
    profiles = profile_speakers(turns)
    assert len(profiles) == 8
    assert {p.speaker for p in profiles} == {f"room-{i}" for i in range(1, 9)}


def test_known_speaker_suggestions_are_never_silent_assignments():
    profiles = [
        SpeakerProfile(
            speaker="room-2", channel=ROOM, speaking_s=12.0, turns=3, first_at=1.0
        )
    ]
    library_people = [
        KnownSpeaker(name="Adam", confirmation_count=5, embedding=[1.0, 0.0])
    ]
    guesses = suggest_matches(
        profiles, library_people, embeddings={"room-2": [1.0, 0.0]}
    )
    assert len(guesses) == 1
    assert guesses[0].name == "Adam"
    assert guesses[0].basis == "voice"
    assert "previously confirmed" in guesses[0].evidence


def test_cleanup_maps_lines_without_inventing_turns():
    original = [
        Turn(ROOM, 0.0, 1.0, "hello hello hello there", speaker="room-1"),
        Turn(ROOM, 1.0, 2.0, "we should meet tuesday", speaker="room-2"),
    ]
    cleaned = _parse_cleaned(
        "[00:00:00] Speaker 1: Hello there.\n[00:00:01] Speaker 2: We should meet Tuesday.\n",
        original,
    )
    assert len(cleaned) == 2
    assert cleaned[0].speaker == "room-1"
    assert cleaned[0].text == "Hello there."
    assert cleaned[1].text == "We should meet Tuesday."
