"""Yarn V2: queue, retention settings, imports, capture-under-load, speakers."""

from __future__ import annotations

import math
import os
import threading
import time
import wave
from pathlib import Path

from types import SimpleNamespace

import pytest

from meeting_archive import cli, jobs, library, pipeline, records, resources, retention, watcher
from meeting_archive.capture import WavWriter
from meeting_archive.cleanup import _parse_cleaned
from meeting_archive.config import Config, retention_days, write_setting
from meeting_archive.imports import ImportFailed, import_transcript
from meeting_archive.models import MIC, ROOM, SYS, Segment, SpeakerProfile, SpeakerSpan, Turn
from meeting_archive.quips import apply_guesses, profile_speakers
from meeting_archive.records import Manifest, Transcript
from meeting_archive.speakers import (
    KnownSpeaker,
    capture_identity,
    clips_dir,
    embeddings_for_profiles,
    forget_speaker,
    leftover_assignment,
    load_library,
    meeting_banner,
    name_needs_detail,
    remember_confirmation,
    render_voices_html,
    snippet_window,
    names_for_roster,
    suggest_matches,
    dialog_prediction,
    usable_dialog_guess,
)
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
    job = jobs.load(cfg, "2026-09-17T13-00-00")
    assert job is not None
    # Simulate the watcher dying after begin_stage stamped its pid.
    job.worker_pid = 2_000_000_001  # not a live process
    jobs.save(cfg, job)
    assert jobs.recover_stale(cfg) == 1
    loaded = jobs.load(cfg, "2026-09-17T13-00-00")
    assert loaded.state == jobs.QUEUED
    assert loaded.worker_pid == 0


def test_recover_stale_leaves_a_live_worker_alone(cfg):
    job = jobs.enqueue(cfg, "2026-09-17T13-30-00")
    jobs.begin_stage(cfg, job, "diarize")
    assert jobs.recover_stale(cfg) == 0
    loaded = jobs.load(cfg, "2026-09-17T13-30-00")
    assert loaded.state == jobs.RUNNING


def test_recover_stale_reclaims_processing_and_keeps_whisper_checkpoint(cfg):
    meeting_id = "2026-09-17T13-45-00"
    room = cfg.processing_dir / f"{meeting_id}__room.wav"
    room.write_bytes(b"RIFF")
    Manifest(
        meeting_id=meeting_id,
        channels={ROOM: room.name},
        started_at="2026-09-17T13:45:00",
        ended_at="2026-09-17T14:00:00",
        duration_s=900.0,
    ).write(cfg.processing_dir / f"{meeting_id}.json")
    job = jobs.enqueue(cfg, meeting_id, duration_s=900.0)
    jobs.begin_stage(cfg, job, "transcribe")
    jobs.finish_stage(cfg, job, "transcribe")
    pipeline._write_whisper_checkpoint(
        cfg,
        meeting_id,
        [Segment(ROOM, 0.0, 1.0, "hello")],
        model="tiny",
        engine="test",
    )
    jobs.begin_stage(cfg, job, "diarize")
    job = jobs.load(cfg, meeting_id)
    job.worker_pid = 2_000_000_002
    jobs.save(cfg, job)

    assert jobs.recover_stale(cfg) >= 1
    loaded = jobs.load(cfg, meeting_id)
    assert loaded.state == jobs.QUEUED
    assert loaded.stage_done("transcribe")
    assert loaded.stages["diarize"].status == jobs.PENDING
    assert (cfg.inbox_dir / f"{meeting_id}.json").exists()
    assert (cfg.inbox_dir / room.name).exists()
    assert not (cfg.processing_dir / f"{meeting_id}.json").exists()
    assert jobs.whisper_checkpoint_path(cfg, meeting_id).exists()


def test_a_partial_whisper_checkpoint_is_not_treated_as_finished(cfg):
    meeting_id = "2026-09-17T13-50-00"
    pipeline._write_whisper_checkpoint(
        cfg,
        meeting_id,
        [Segment(ROOM, 0.0, 1.0, "hello")],
        model="tiny",
        engine="test",
        partial=True,
        channels={
            ROOM: {
                "complete": False,
                "through_s": 30.0,
                "segments": [Segment(ROOM, 0.0, 1.0, "hello").to_dict()],
            }
        },
    )
    path = jobs.whisper_checkpoint_path(cfg, meeting_id)
    assert jobs.whisper_checkpoint_is_complete(path) is False
    job = jobs.enqueue(cfg, meeting_id)
    jobs.begin_stage(cfg, job, "transcribe")
    job = jobs.load(cfg, meeting_id)
    job.worker_pid = 2_000_000_003
    jobs.save(cfg, job)
    assert jobs.recover_stale(cfg) >= 1
    loaded = jobs.load(cfg, meeting_id)
    assert loaded.stage_done("transcribe") is False
    assert loaded.state == jobs.QUEUED
    assert path.exists()


def test_recover_stale_completes_a_queued_job_that_already_has_a_transcript(cfg):
    write_transcript(cfg, "2026-09-17T14-00-00", [Turn(ROOM, 0.0, 1.0, "Hi")])
    jobs.enqueue(cfg, "2026-09-17T14-00-00", title="stale")
    assert jobs.recover_stale(cfg) == 1
    loaded = jobs.load(cfg, "2026-09-17T14-00-00")
    assert loaded.state == jobs.COMPLETE


def test_library_treats_deferred_summary_as_pending_not_a_failure(cfg):
    transcript = write_transcript(
        cfg, "2026-09-17T15-00-00", [Turn(ROOM, 0.0, 1.0, "Hi")]
    )
    transcript.summary_error = "deferred: Ollama was not running"
    transcript.write(cfg.transcript_json(transcript.meeting_id))
    html = library.render_html(cfg).read_text(encoding="utf-8")
    assert "Summary pending" in html
    page = cfg.transcript_html(transcript.meeting_id)
    assert page.as_uri() in html
    assert "Listen" in page.read_text(encoding="utf-8")
    assert 'class="note"' in html
    assert "class=err" not in html


def test_sweep_summaries_retries_an_interrupted_summary(cfg):
    meeting_id = "2026-09-25T10-00-00"
    transcript = write_transcript(cfg, meeting_id, [Turn(ROOM, 0.0, 1.0, "We should ship.")])
    transcript.summary = ""
    transcript.summary_error = "deferred: local summary timed out"
    transcript.write(cfg.transcript_json(meeting_id))

    class Later:
        enabled = True

        def is_running(self):
            return True

        def summarize(self, turns, label_fn, language=""):
            return "Ship it."

    assert watcher.sweep_summaries(cfg, Later(), FakeStore()) == 1
    filled = Transcript.read(cfg.transcript_json(meeting_id))
    assert filled.summary == "Ship it."
    assert filled.summary_error == ""


def test_sweep_summaries_respects_manual_schedule(cfg):
    meeting_id = "2026-09-25T11-00-00"
    write_transcript(cfg, meeting_id, [Turn(ROOM, 0.0, 1.0, "Hi")])
    cfg.summary.when = "manual"

    class Boom:
        enabled = True

        def is_running(self):
            return True

        def summarize(self, turns, label_fn, language=""):
            raise AssertionError("manual mode must not auto-summarize")

    assert watcher.sweep_summaries(cfg, Boom(), FakeStore()) == 0


def test_summary_setting_accepts_fast_and_accurate_names(cfg):
    from meeting_archive.cli import cmd_setting

    args = SimpleNamespace(key="summary_model", value="fast")
    assert cmd_setting(cfg, args) == 0
    from meeting_archive.config import _apply_settings_overlay

    other = Config(root=cfg.root)
    _apply_settings_overlay(other)
    assert other.summary.model == "qwen3.5:4b"


def test_summary_model_setting_overlay(cfg):
    write_setting(cfg, "summary_model", "qwen3.5:4b")
    other = Config(root=cfg.root)
    from meeting_archive.config import _apply_settings_overlay

    _apply_settings_overlay(other)
    assert other.summary.model == "qwen3.5:4b"


def test_summarize_when_setting_overlay(cfg):
    write_setting(cfg, "summarize_when", "evening")
    other = Config(root=cfg.root)
    from meeting_archive.config import _apply_settings_overlay

    _apply_settings_overlay(other)
    assert other.summary.when == "evening"


def test_a_settings_file_with_no_voice_switch_names_returning_voices(cfg):
    from meeting_archive.config import _apply_settings_overlay

    write_setting(cfg, "summarize_when", "evening")
    other = Config(root=cfg.root)
    _apply_settings_overlay(other)
    assert other.speakers_memory.recognise_known is True
    write_setting(other, "recognise_known", False)
    again = Config(root=cfg.root)
    _apply_settings_overlay(again)
    assert again.speakers_memory.recognise_known is False


@needs_ffmpeg
def test_discard_aborted_starts_leaves_real_stranded_captures(cfg):
    import struct
    import wave

    def write_pcm(path: Path, seconds: float) -> None:
        frames = int(16000 * seconds)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))

    stub = cfg.in_progress_dir / "2026-09-17T09-07-45__raw.wav"
    kept = cfg.in_progress_dir / "2026-09-17T09-20-00__raw.wav"
    write_pcm(stub, 6.0)
    write_pcm(kept, 40.0)
    removed = pipeline.discard_aborted_starts(cfg)
    assert stub.name in removed
    assert not stub.exists()
    assert kept.exists()


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


def test_a_trusted_voice_is_named_and_a_guess_from_the_words_is_not(cfg):
    from meeting_archive.models import SpeakerGuess

    meeting_id = "2026-10-21T09-00-00"
    guesses = [
        SpeakerGuess(
            speaker="room-1",
            name="Ada",
            basis="voice",
            confidence=0.5,
            evidence="Matched Ada based on 2 previously confirmed meeting(s).",
        ),
        SpeakerGuess(speaker="room-2", name="Maybe", basis="self", confidence=0.9),
        SpeakerGuess(speaker="room-3", name="unsure", basis="voice", confidence=0.5),
    ]
    assert names_for_roster(guesses) == {"room-1": "Ada"}
    cfg.speakers_memory.recognise_known = False
    pipeline._keep_known_voices(cfg, meeting_id, guesses)
    assert records.read_notes(cfg.notes_json(meeting_id)) == {}
    cfg.speakers_memory.recognise_known = True
    pipeline._keep_known_voices(cfg, meeting_id, guesses)
    notes = records.read_notes(cfg.notes_json(meeting_id))
    assert notes["speakers"] == {"room-1": "Ada"}
    assert notes["applied"] is True


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


def test_voice_match_fires_at_realistic_cosine():
    """Regression: the old 0.75 floor was unreachable, so this was dead code.

    Measured genuine same-speaker cosines with this extractor are ~0.41-0.53,
    nowhere near 0.75, so every known-speaker match was computed, written to
    speaker_guesses and then never offered. These vectors score ~0.50, which
    is what a real match actually looks like. If this test starts failing, the
    threshold has drifted back out of reach.
    """
    profiles = [
        SpeakerProfile(
            speaker="room-1", channel=ROOM, speaking_s=20.0, turns=4, first_at=0.0
        )
    ]
    people = [KnownSpeaker(name="Adam", confirmation_count=5, embedding=[1.0, 0.0])]
    # cos = 0.5 exactly.
    vector = [1.0, math.sqrt(3.0)]

    assert suggest_matches(profiles, people, embeddings={"room-1": vector}) != []
    # And the old default would have rejected it, which is the bug.
    assert suggest_matches(
        profiles, people, embeddings={"room-1": vector}, min_confidence=0.75
    ) == []


def test_near_tie_offers_no_name():
    """A low floor makes the runner-up dangerous, so a near-tie must abstain.

    Two library voices almost equidistant from this speaker is not an
    identification. Offering the marginally closer one would be a coin flip
    presented as a suggestion.
    """
    profiles = [
        SpeakerProfile(
            speaker="room-1", channel=ROOM, speaking_s=20.0, turns=4, first_at=0.0
        )
    ]
    people = [
        KnownSpeaker(name="Adam", confirmation_count=5, embedding=[1.0, 0.02]),
        KnownSpeaker(name="Alex", confirmation_count=3, embedding=[1.0, 0.0]),
    ]
    assert suggest_matches(profiles, people, embeddings={"room-1": [1.0, 0.0]}) == []


def test_a_name_goes_to_its_best_match_not_the_earliest_speaker():
    """Assignment is global best-first, not in profile order.

    Profiles arrive sorted by who spoke first, so the greedy version let an
    early weak match claim a name that fitted a later voice far better.
    """
    profiles = [
        SpeakerProfile(
            speaker="room-1", channel=ROOM, speaking_s=10.0, turns=2, first_at=0.0
        ),
        SpeakerProfile(
            speaker="room-2", channel=ROOM, speaking_s=10.0, turns=2, first_at=9.0
        ),
    ]
    people = [KnownSpeaker(name="Adam", confirmation_count=5, embedding=[1.0, 0.0])]
    guesses = suggest_matches(
        profiles,
        people,
        embeddings={
            # room-2 is a much better match for Adam than room-1.
            "room-1": [1.0, math.sqrt(3.0)],  # cos 0.50
            "room-2": [1.0, 0.1],             # cos ~0.995
        },
    )
    assert [(g.speaker, g.name) for g in guesses] == [("room-2", "Adam")]


def test_apply_guesses_restamps_without_blanking():
    """The ordering fix: a voice match has to reach the dialog's prefill.

    Profiles are built before recognition can run, so the guess they are
    constructed with is always pre-recognition.
    """
    profiles = profile_speakers(
        [
            Turn(ROOM, 0.0, 5.0, "I look after the billing integrations.", speaker="room-1"),
            Turn(ROOM, 6.0, 9.0, "I drove up from Brighton this morning.", speaker="room-2"),
        ],
        guesses={"room-1": "Ev"},
    )
    assert [p.guess for p in profiles] == ["Ev", ""]

    apply_guesses(profiles, {"room-2": "Adam"})
    # room-2 gains the voice match; room-1 keeps what it had rather than
    # being blanked by a map that simply doesn't mention it.
    assert [p.guess for p in profiles] == ["Ev", "Adam"]


def test_idle_queue_still_names_the_last_meeting(cfg):
    """Draining the queue resets the counter but must not erase `last_id`.

    It used to clear both, so the menu bar could only ever say "Idle" -- which
    looks identical whether the queue is empty or the watcher died hours ago.
    """
    from meeting_archive import queue

    queue.begin_work(cfg, "2026-09-29T10-31-30")
    queue.end_work(cfg, "2026-09-29T10-31-30")

    drained = queue.read(cfg)
    assert drained["working_id"] == ""
    assert drained["current"] == 0 and drained["total"] == 0
    assert drained["last_id"] == "2026-09-29T10-31-30"
    assert drained["last_finished_epoch"] > 0

    # The 5-second refresh tick is what used to wipe it.
    queue.refresh(cfg)
    assert queue.read(cfg)["last_id"] == "2026-09-29T10-31-30"


def test_reconcile_only_clears_jobs_whose_transcript_exists(cfg):
    """Stale bookkeeping gets repaired; genuinely outstanding work does not.

    A queued job with no transcript is real work waiting. Marking that
    complete would hide a meeting that never got processed, which is the
    failure this whole queue exists to make visible.
    """
    done = jobs.enqueue(cfg, "2026-09-17T09-21-10")
    Transcript(meeting_id=done.meeting_id, turns=[Turn(ROOM, 0.0, 1.0, "Hi.")]).write(
        cfg.transcript_json(done.meeting_id)
    )
    outstanding = jobs.enqueue(cfg, "2026-09-30T09-00-00")

    repaired = jobs.reconcile(cfg)
    assert repaired == [done.meeting_id]

    states = {j.meeting_id: j.state for j in jobs.list_jobs(cfg)}
    assert states[done.meeting_id] == jobs.COMPLETE
    assert states[outstanding.meeting_id] == jobs.QUEUED

    # Idempotent: a second run has nothing left to do.
    assert jobs.reconcile(cfg) == []


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


def _three_voices(cfg, meeting_id: str) -> list[SpeakerProfile]:
    turns = [
        Turn(ROOM, 0.0, 4.0, "I handle billing at Zuora.", speaker="room-1"),
        Turn(ROOM, 5.0, 8.0, "I came up from Brighton.", speaker="room-2"),
        Turn(ROOM, 9.0, 12.0, "Let's ship Friday.", speaker="room-3"),
    ]
    profiles = profile_speakers(turns)
    Transcript(
        meeting_id=meeting_id,
        turns=turns,
        speaker_profiles=profiles,
        title="Discovery with Acme",
        diarize_model="test",
    ).write(cfg.transcript_json(meeting_id))
    return profiles


def test_meeting_banner_includes_notes_and_title(cfg):
    meeting_id = "2026-09-25T10-00-00"
    _three_voices(cfg, meeting_id)
    records.write_notes(
        cfg.notes_json(meeting_id),
        meeting_id,
        notes="pricing follow-up after the demo",
        attendees="Adam, Ben, Marina",
    )
    banner = meeting_banner(cfg, meeting_id)
    assert "25 Sep 2026" in banner or "2026" in banner
    assert "Discovery with Acme" in banner
    assert "pricing follow-up after the demo" in banner
    assert "Adam, Ben, Marina" in banner


def test_leftover_assignment_names_the_last_listed_person():
    profiles = [
        SpeakerProfile(speaker="room-1", channel=ROOM),
        SpeakerProfile(speaker="room-2", channel=ROOM),
        SpeakerProfile(speaker="room-3", channel=ROOM),
    ]
    result = leftover_assignment(
        profiles,
        {"room-1": "Adam", "room-2": "Ben"},
        ["Adam", "Ben", "Marina"],
        ["Jacob"],
    )
    assert result == ("room-3", "Marina")


def test_leftover_assignment_does_not_guess_when_two_voices_remain():
    profiles = [
        SpeakerProfile(speaker="room-1", channel=ROOM),
        SpeakerProfile(speaker="room-2", channel=ROOM),
        SpeakerProfile(speaker="room-3", channel=ROOM),
    ]
    assert leftover_assignment(
        profiles, {"room-1": "Adam"}, ["Adam", "Ben", "Marina"], ["Jacob"]
    ) is None


def test_leftover_assignment_ignores_the_known_microphone():
    profiles = [
        SpeakerProfile(speaker="mic", channel=MIC, speaking_s=12.0),
        SpeakerProfile(speaker="sys-1", channel=SYS),
        SpeakerProfile(speaker="sys-2", channel=SYS),
    ]
    result = leftover_assignment(
        profiles,
        {"sys-1": "Adam"},
        ["Jacob", "Adam", "Marina"],
        ["Jacob"],
    )
    assert result == ("sys-2", "Marina")


def test_voice_match_does_not_rename_the_microphone():
    people = [KnownSpeaker(name="Adam", confirmation_count=5, embedding=[1.0, 0.0])]
    guesses = suggest_matches(
        [
            SpeakerProfile(speaker="mic", channel=MIC),
            SpeakerProfile(speaker="sys-1", channel=SYS),
        ],
        people,
        embeddings={"mic": [1.0, 0.0], "sys-1": [0.0, 1.0]},
    )
    assert [g.speaker for g in guesses] == []


def test_leftover_assignment_ignores_own_name_on_a_call():
    profiles = [
        SpeakerProfile(speaker="sys-1", channel=SYS),
        SpeakerProfile(speaker="sys-2", channel=SYS),
    ]
    result = leftover_assignment(
        profiles,
        {"sys-1": "Adam"},
        ["Jacob", "Adam", "Marina"],
        ["Jacob"],
    )
    assert result == ("sys-2", "Marina")


def test_leftover_assignment_needs_an_attendee_list():
    profiles = [
        SpeakerProfile(speaker="room-1", channel=ROOM),
        SpeakerProfile(speaker="room-2", channel=ROOM),
    ]
    assert leftover_assignment(profiles, {"room-1": "Adam"}, [], ["Jacob"]) is None


def test_naming_two_of_three_assumes_the_last(cfg, capsys):
    meeting_id = "2026-09-25T11-00-00"
    _three_voices(cfg, meeting_id)
    records.write_notes(
        cfg.notes_json(meeting_id), meeting_id, attendees="Adam, Ben, Marina"
    )
    args = SimpleNamespace(
        meeting_id=meeting_id,
        set=["room-1=Adam", "room-2=Ben"],
        pending_tsv=False,
        review_warning=False,
    )
    assert cli.cmd_speakers(cfg, args) == 0
    roster = records.speaker_roster(records.read_notes(cfg.notes_json(meeting_id)))
    assert roster["room-3"] == "Marina"
    assert "ASSUMED" in capsys.readouterr().out
    from meeting_archive.speakers import load_library

    names = {person.name for person in load_library(cfg)}
    assert names == {"Adam", "Ben", "Marina"}


def _assert_solo_clip(start, duration, blocked):
    end = start + duration
    assert duration >= 1.0
    for other_start, other_end in blocked:
        overlap = min(end, other_end) - max(start, other_start)
        assert overlap <= 0.0, (start, end, other_start, other_end, overlap)


def test_snippet_window_prefers_the_quip_turn():
    profile = SpeakerProfile(
        speaker="room-2",
        channel=ROOM,
        first_at=1.0,
        quip="I came up from Brighton.",
    )
    transcript = Transcript(
        meeting_id="m",
        turns=[
            Turn(ROOM, 1.0, 2.0, "Hi.", speaker="room-2"),
            Turn(ROOM, 10.0, 16.0, "I came up from Brighton.", speaker="room-2"),
        ],
    )
    start, duration = snippet_window(transcript, profile)
    assert 10.0 <= start < start + duration <= 16.0
    assert duration == pytest.approx(4.0)
    _assert_solo_clip(start, duration, [])


def test_snippet_window_skips_overlapping_speech():
    from meeting_archive.speakers import speaker_excerpt

    profile = SpeakerProfile(
        speaker="room-2",
        channel=ROOM,
        first_at=1.0,
        quip="I came up from Brighton.",
    )
    transcript = Transcript(
        meeting_id="m",
        turns=[
            Turn(ROOM, 10.0, 16.0, "I came up from Brighton.", speaker="room-2"),
            Turn(ROOM, 12.0, 14.0, "Hang on.", speaker="room-1"),
            Turn(ROOM, 20.0, 24.0, "I handle billing.", speaker="room-2"),
        ],
    )
    start, duration = snippet_window(transcript, profile)
    _assert_solo_clip(start, duration, [(12.0, 14.0)])
    assert 20.0 <= start < start + duration <= 24.0
    excerpt = speaker_excerpt(transcript, profile)
    assert "I came up from Brighton." in excerpt
    assert "I handle billing." in excerpt


def test_snippet_window_uses_spans_when_turns_look_isolated():
    """ASR turns can hide a second voice that diarization already split out."""
    profile = SpeakerProfile(speaker="room-2", channel=ROOM, first_at=10.0)
    transcript = Transcript(
        meeting_id="m",
        turns=[
            Turn(ROOM, 10.0, 16.0, "I handle billing.", speaker="room-2"),
            Turn(ROOM, 20.0, 24.0, "And the invoice lands on Friday.", speaker="room-2"),
        ],
        speaker_spans=[
            SpeakerSpan(ROOM, 10.0, 16.0, "room-2"),
            SpeakerSpan(ROOM, 12.5, 14.0, "room-1"),
            SpeakerSpan(ROOM, 20.0, 24.0, "room-2"),
        ],
    )
    start, duration = snippet_window(transcript, profile)
    _assert_solo_clip(start, duration, [(12.5, 14.0)])
    assert 20.0 <= start < start + duration <= 24.0


def test_an_overwhelming_match_is_prefilled_when_they_are_in_the_meeting():
    attendees = ["Jacob", "Shiv", "Jamie", "Betty", "Shaym"]
    # Shiv is clearly this voice, and Shiv is on the list.
    name, reason = dialog_prediction(
        [("Shiv", 0.70), ("Charles", 0.40), ("Jamie", 0.31)],
        attendees=attendees,
    )
    assert name == "Shiv"
    assert "in this meeting" in reason

    # A library match who was not in the meeting does not take the box,
    # even when their score is a little higher.
    name, _reason = dialog_prediction(
        [("Charles", 0.76), ("Shiv", 0.70), ("Jamie", 0.30)],
        attendees=attendees,
    )
    assert name == ""

    # Two people who were both there, and neither lead is overwhelming.
    name, _reason = dialog_prediction(
        [("Shiv", 0.66), ("Jamie", 0.62)],
        attendees=attendees,
    )
    assert name == ""

    # No attendee list keeps the older suggestion, so a meeting without a
    # roster still offers a strong voice match.
    name, _reason = dialog_prediction([("Shiv", 0.70), ("Jamie", 0.40)])
    assert name == "Shiv"


def test_usable_dialog_guess_rejects_transcript_and_locations():
    assert usable_dialog_guess("Sydney", attendees=["Lauren", "Ket", "Jacob"]) == ""
    assert usable_dialog_guess("A lot of flexibility in sandboxes") == ""
    assert usable_dialog_guess("Ket", attendees=["Lauren", "Ket", "Jacob"]) == "Ket"
    assert usable_dialog_guess("Jacob", known=["Jacob", "Adam"]) == "Jacob"
    assert usable_dialog_guess("Marina") == "Marina"


def test_jacob_and_Jacob_are_one_person(cfg):
    first = remember_confirmation(cfg, name="Jacob", meeting_id="2026-09-25T11-00-00")
    again = remember_confirmation(cfg, name="jacob", meeting_id="2026-09-26T11-00-00")
    assert first.name == "Jacob"
    assert again.name == "Jacob"
    assert again.confirmation_count == 2
    assert len(load_library(cfg)) == 1


def test_a_second_jacob_with_a_title_stays_a_second_person(cfg):
    remember_confirmation(cfg, name="Jacob", meeting_id="2026-09-25T11-00-00")
    remember_confirmation(
        cfg,
        name="jacob",
        meeting_id="2026-09-26T11-00-00",
        title="Finance director",
        distinct=True,
    )
    people = load_library(cfg)
    assert len(people) == 2
    assert {person.title for person in people} == {"", "Finance director"}
    assert name_needs_detail(people, "Jacob")
    assert not name_needs_detail(people, "Jacob Smith")
    assert not name_needs_detail(people, "Jacob", title="Engineer")
    assert not name_needs_detail(people, "Jacob", company="Northwind")


def test_company_is_stored_with_the_person(cfg):
    person = remember_confirmation(
        cfg,
        name="Jacob",
        meeting_id="2026-09-25T11-00-00",
        title="Finance director",
        company="Northwind",
    )
    assert person.title == "Finance director"
    assert person.company == "Northwind"
    again = remember_confirmation(cfg, name="jacob", meeting_id="2026-09-26T11-00-00")
    assert again.company == "Northwind"
    assert len(load_library(cfg)) == 1
    from meeting_archive.speakers import search_label
    assert search_label(again) == "Jacob (Finance director, Northwind)"


def test_transcript_page_can_play_a_kept_recording(cfg, tmp_path):
    from meeting_archive.records import render_transcript_html

    wav = tmp_path / "2026-09-25T11-00-00__room.wav"
    wav.write_bytes(b"RIFF")
    transcript = Transcript(
        meeting_id="2026-09-25T11-00-00",
        duration_s=65,
        title="Planning",
        summary="Jacob will send the note.",
        turns=[Turn(ROOM, 1, 4, "Hello there.", speaker="room-1")],
    )
    html = render_transcript_html(
        transcript,
        lambda turn: "Jacob",
        {},
        titles={"jacob": "Finance director"},
        companies={"jacob": "Northwind"},
        recordings=[("The recording", wav)],
    )
    assert "Who spoke" in html
    assert "What was said" in html
    assert "Finance director, Northwind" in html
    assert wav.as_uri() in html
    assert "room-1" not in html
    cleared = render_transcript_html(transcript, lambda turn: "Jacob", {})
    assert "recording has been cleared" in cleared


def test_known_speakers_html_lists_confirmed_voices(cfg):
    remember_confirmation(cfg, name="Marina", meeting_id="2026-09-25T11-00-00")
    path = render_voices_html(cfg)
    text = path.read_text(encoding="utf-8")
    assert "Marina" in text
    assert "Known speakers" in text
    assert "confirmation" in text


def test_embed_file_without_model_returns_empty(tmp_path):
    from meeting_archive.config import DiarizeConfig
    from meeting_archive.diarize import embed_file

    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"not-a-wav")
    config = DiarizeConfig(embedding_model=str(tmp_path / "missing.onnx"))
    assert embed_file(config, wav) == []


def _stub_clip_extract(monkeypatch, payload: bytes = b""):
    blob = payload or (b"RIFF" + b"identity-snippet" + b"\x00" * 40)

    def fake_extract(src, dest, start, duration):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        return dest

    monkeypatch.setattr("meeting_archive.audio.extract_clip", fake_extract)


def test_validated_identity_clip_survives_audio_prune(cfg, monkeypatch):
    meeting_id = "2026-08-01T10-00-00"
    profiles = _three_voices(cfg, meeting_id)
    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    src = cfg.audio_archive_dir / f"{meeting_id}__room.wav"
    src.write_bytes(b"x" * 1024)
    _stub_clip_extract(monkeypatch)
    monkeypatch.setattr(
        "meeting_archive.diarize.embed_file",
        lambda config, path: [0.2, 0.8],
    )

    clip, embedding = capture_identity(cfg, transcript, profiles[1], "Marina")
    assert clip == "marina.wav"
    # Stored unit-length so clips from different meetings average fairly.
    assert embedding == pytest.approx([0.2 / 0.8246211, 0.8 / 0.8246211], rel=1e-5)
    remember_confirmation(
        cfg, name="Marina", meeting_id=meeting_id, embedding=embedding, clip=clip
    )
    identity = clips_dir(cfg) / "marina.wav"
    assert identity.exists()

    now = time.time()
    os.utime(src, (now - 30 * 86400,) * 2)
    os.utime(identity, (now - 30 * 86400,) * 2)
    result = retention.prune(cfg, now=now)
    assert src.name in result.deleted
    assert not src.exists()
    assert identity.exists()
    people = load_library(cfg)
    assert people[0].embedding == pytest.approx(embedding, rel=1e-5)
    assert people[0].clip == "marina.wav"


def test_first_validated_clip_is_not_overwritten(cfg, monkeypatch):
    meeting_id = "2026-09-25T11-00-00"
    profiles = _three_voices(cfg, meeting_id)
    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    (cfg.audio_archive_dir / f"{meeting_id}__room.wav").write_bytes(b"x" * 100)
    payloads = iter(
        [
            b"first-validated-clip" + b"\x00" * 40,
            b"second-meeting-clipxx" + b"\x00" * 40,
        ]
    )

    def fake_extract(src, dest, start, duration):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(next(payloads))
        return dest

    monkeypatch.setattr("meeting_archive.audio.extract_clip", fake_extract)
    monkeypatch.setattr(
        "meeting_archive.diarize.embed_file", lambda *args, **kwargs: [1.0]
    )
    capture_identity(cfg, transcript, profiles[0], "Adam")
    first = (clips_dir(cfg) / "adam.wav").read_bytes()
    capture_identity(cfg, transcript, profiles[0], "Adam")
    assert (clips_dir(cfg) / "adam.wav").read_bytes() == first
    assert list(clips_dir(cfg).glob(".embed-*")) == []


def test_forget_speaker_deletes_identity_clip(cfg):
    folder = clips_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / "marina.wav"
    clip.write_bytes(b"snippet")
    remember_confirmation(
        cfg, name="Marina", meeting_id="2026-09-25T11-00-00", clip="marina.wav"
    )
    assert forget_speaker(cfg, "Marina")
    assert not clip.exists()
    assert load_library(cfg) == []


def test_embeddings_for_profiles_do_not_write_library_clips(cfg, monkeypatch):
    meeting_id = "2026-09-25T12-00-00"
    profiles = _three_voices(cfg, meeting_id)
    (cfg.audio_archive_dir / f"{meeting_id}__room.wav").write_bytes(b"x" * 100)
    _stub_clip_extract(monkeypatch)
    monkeypatch.setattr(
        "meeting_archive.diarize.embed_file",
        lambda config, path: [0.5, 0.5],
    )
    turns = Transcript.read(cfg.transcript_json(meeting_id)).turns
    vectors = embeddings_for_profiles(cfg, meeting_id, turns, profiles)
    assert set(vectors) == {p.speaker for p in profiles}
    assert list(clips_dir(cfg).glob("*.wav")) == []


def test_queue_counts_inbox_and_pending_summaries(cfg):
    from meeting_archive import queue as queue_mod

    _inbox_meeting(cfg, "2026-09-26T09-00-00")
    write_transcript(cfg, "2026-09-26T08-00-00", [Turn(ROOM, 0.0, 1.0, "Hi")])
    payload = queue_mod.refresh(cfg)
    assert payload["inbox"] == 1
    assert payload["summaries_pending"] == 1
    assert payload["remaining"] == 2


def test_queue_exclude_skips_the_meeting_currently_running(cfg):
    from meeting_archive import queue as queue_mod

    write_transcript(cfg, "2026-09-26T08-00-00", [Turn(ROOM, 0.0, 1.0, "Hi")])
    _inbox_meeting(cfg, "2026-09-26T09-00-00")
    payload = queue_mod.refresh(cfg, exclude="2026-09-26T08-00-00")
    assert payload["remaining"] == 1
    assert payload["summaries_pending"] == 0


def test_sweep_inbox_holds_today_until_evening(cfg, monkeypatch):
    from datetime import datetime as dt

    from meeting_archive import summarize as summarize_mod

    class Frozen(dt):
        @classmethod
        def now(cls, tz=None):
            return dt(2026, 9, 26, 11, 0, 0)

    monkeypatch.setattr(summarize_mod, "datetime", Frozen)
    cfg.summary.when = "evening"
    cfg.summary.evening_hour = 18
    _inbox_meeting(cfg, "2026-09-26T10-00-00")
    _inbox_meeting(cfg, "2026-09-25T16-00-00")
    count = watcher.sweep_inbox(cfg, FakeTranscriber(), FakeStore())
    assert count == 1
    assert (cfg.inbox_dir / "2026-09-26T10-00-00.json").exists()
    assert cfg.transcript_json("2026-09-25T16-00-00").exists()


def test_sweep_inbox_force_ignores_manual_schedule(cfg):
    cfg.summary.when = "manual"
    _inbox_meeting(cfg, "2026-09-26T11-00-00")
    assert watcher.sweep_inbox(cfg, FakeTranscriber(), FakeStore()) == 0
    assert watcher.sweep_inbox(
        cfg, FakeTranscriber(), FakeStore(), force=True
    ) == 1
    assert cfg.transcript_json("2026-09-26T11-00-00").exists()


def test_begin_work_counts_one_of_five_then_advances(cfg):
    from meeting_archive import queue as queue_mod

    ids = [f"2026-09-26T10-0{i}-00" for i in range(5)]
    for meeting_id in ids:
        _inbox_meeting(cfg, meeting_id)

    first = queue_mod.begin_work(cfg, ids[0])
    assert first["current"] == 1
    assert first["total"] == 5
    assert first["remaining"] == 4
    again = queue_mod.begin_work(cfg, ids[0])
    assert again["current"] == 1

    (cfg.inbox_dir / f"{ids[0]}.json").unlink()
    queue_mod.end_work(cfg, ids[0])
    after = queue_mod.begin_work(cfg, ids[0])
    assert after["current"] == 1

    queue_mod.end_work(cfg, ids[0])
    second = queue_mod.begin_work(cfg, ids[1])
    assert second["current"] == 2
    assert second["total"] == 5
    assert second["remaining"] == 3



def test_summary_uses_confirmed_names_for_numbered_speakers():
    from meeting_archive.models import SpeakerProfile
    from meeting_archive.records import name_speakers_in_text

    transcript = Transcript(meeting_id="2026-10-07T15-51-58")
    transcript.speaker_profiles = [
        SpeakerProfile(speaker="room-1", channel="room"),
        SpeakerProfile(speaker="room-2", channel="room"),
        SpeakerProfile(speaker="room-12", channel="room"),
    ]
    text = "Owner: Speaker 1. Owner: Speaker 2, for Speaker 1's use case. Speaker 12 and Speaker 3."
    named = name_speakers_in_text(text, transcript, {"room-1": "Jamie", "room-2": "Jacob"})
    assert named == "Owner: Jamie. Owner: Jacob, for Jamie's use case. Speaker 12 and Speaker 3."
