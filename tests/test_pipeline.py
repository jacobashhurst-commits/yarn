"""Integration tests for everything except Whisper and Chroma themselves.

The two heavy dependencies are stubbed, which leaves plenty worth testing for
real: the channel split runs through actual ffmpeg on a synthetic multi-channel
file, and the manifest/transcript/notes handoff runs on a real filesystem.

The channel-split test matters more than it looks. If mic_channels and
sys_channels are wrong, everything downstream still works -- transcripts get
written, search returns hits -- but every speaker label is backwards. That is
the sort of bug you don't notice for a month.
"""

from __future__ import annotations

import array
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_archive import audio, cli, pipeline, records, retention, watcher
from meeting_archive.audio import AudioError, pan_expression
from meeting_archive.config import Config
from meeting_archive.diarize import DiarizationUnavailable
from meeting_archive.models import (
    MIC,
    ROOM,
    SYS,
    NameCandidate,
    Segment,
    SpeakerSpan,
    Turn,
)
from meeting_archive.quips import profile_speakers
from meeting_archive.records import Manifest, Transcript
from meeting_archive.store import chunk_id, sanitize_metadata

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


# ---------------------------------------------------------------------------
# fixtures and stubs
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(root=tmp_path / "archive")
    config.audio.device_index = 0
    config.ensure_dirs()
    return config


class FakeStore:
    """Stands in for MeetingStore, recording what would have been indexed."""

    def __init__(self):
        self.meetings: dict[str, tuple[list[str], list[dict]]] = {}
        self.deletes: list[str] = []

    def delete_meeting(self, meeting_id):
        self.deletes.append(meeting_id)
        self.meetings.pop(meeting_id, None)

    def replace_meeting(self, meeting_id, documents, metadatas):
        self.deletes.append(meeting_id)
        self.meetings[meeting_id] = (documents, [sanitize_metadata(m) for m in metadatas])
        return len(documents)


class FakeTranscriber:
    """Returns canned segments per channel instead of running Whisper."""

    def __init__(self, per_channel: dict[str, list[Segment]] | None = None):
        self.per_channel = per_channel or {}
        self.calls: list[tuple[str, str]] = []

    def transcribe_file(
        self, path: Path, channel: str, duration_s=0.0, progress=None
    ) -> list[Segment]:
        assert path.exists(), f"transcriber handed a missing file: {path}"
        self.calls.append((path.name, channel))
        if progress:
            progress(1.0)
        return list(self.per_channel.get(channel, []))


def make_wav(
    path: Path,
    channel_signals: list[bool],
    duration: float = 2.0,
    rate: int = 16000,
) -> Path:
    """Write a multi-channel wav where each channel is a tone or silence.

    The PCM frames are interleaved by hand rather than assembled with ffmpeg's
    `join` filter. That filter maps mono inputs onto a multi-channel layout by
    guessing, and the guess is not positional -- an earlier version of this
    fixture put the tone in a different channel than intended and made the
    channel-split code look broken when it was fine. Interleaving the samples
    directly makes channel order independent ground truth.
    """
    assert sys.byteorder == "little", "wav is little-endian; this fixture assumes LE"

    frames = int(duration * rate)
    samples = array.array("h")
    for i in range(frames):
        value = int(20000 * math.sin(2 * math.pi * 440 * i / rate))
        for has_tone in channel_signals:
            samples.append(value if has_tone else 0)

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(len(channel_signals))
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())
    return path


def mean_volume_db(path: Path) -> float:
    """Mean volume in dB via ffmpeg's volumedetect. Silence returns -inf."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", proc.stderr)
    if not match:
        return float("-inf")
    value = match.group(1)
    return float("-inf") if value == "-inf" else float(value)


# ---------------------------------------------------------------------------
# pan expressions
# ---------------------------------------------------------------------------

def test_pan_expression_single_channel_is_passthrough():
    assert pan_expression([0]) == "pan=mono|c0=c0"


def test_pan_expression_averages_multiple_channels():
    expr = pan_expression([1, 2])
    assert expr.startswith("pan=mono|c0=")
    assert "c1" in expr and "c2" in expr
    weights = [float(w) for w in re.findall(r"([\d.]+)\*c", expr)]
    assert weights == [0.5, 0.5]
    assert sum(weights) == pytest.approx(1.0)


def test_pan_expression_rejects_empty():
    with pytest.raises(AudioError):
        pan_expression([])


# ---------------------------------------------------------------------------
# channel splitting, against real ffmpeg
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_probe_reports_channels_and_duration(tmp_path):
    src = make_wav(tmp_path / "three.wav", [True, False, False])
    info = audio.probe(src)
    assert info["channels"] == 3
    assert info["duration"] == pytest.approx(2.0, abs=0.1)


@needs_ffmpeg
def test_extract_mono_takes_the_requested_channel(tmp_path):
    # Tone on channel 0 only.
    src = make_wav(tmp_path / "raw.wav", [True, False, False])

    loud = audio.extract_mono(src, tmp_path / "mic.wav", [0], 16000)
    quiet = audio.extract_mono(src, tmp_path / "sys.wav", [1, 2], 16000)

    assert mean_volume_db(loud) > -40
    assert mean_volume_db(quiet) < -60, "channel 0's tone leaked into the sys mix"

    info = audio.probe(loud)
    assert info["channels"] == 1
    assert info["sample_rate"] == 16000


@needs_ffmpeg
def test_extract_mono_mapping_is_not_accidentally_identity(tmp_path):
    # Tone on channel 1 this time, so a broken mapping shows up as a swap.
    src = make_wav(tmp_path / "raw.wav", [False, True, False])
    mic = audio.extract_mono(src, tmp_path / "mic.wav", [0], 16000)
    sys_ = audio.extract_mono(src, tmp_path / "sys.wav", [1, 2], 16000)
    assert mean_volume_db(mic) < -60
    assert mean_volume_db(sys_) > -50


@needs_ffmpeg
def test_extract_mono_gives_a_useful_error_for_a_bad_channel_index(tmp_path):
    src = make_wav(tmp_path / "raw.wav", [True, False])
    with pytest.raises(AudioError) as exc:
        audio.extract_mono(src, tmp_path / "out.wav", [7], 16000)
    message = str(exc.value)
    assert "2 channel" in message
    assert "ma devices" in message  # tells Jacob what to actually do


@needs_ffmpeg
def test_extract_mono_leaves_no_partial_file_on_failure(tmp_path):
    src = make_wav(tmp_path / "raw.wav", [True, False])
    dst = tmp_path / "out.wav"
    with pytest.raises(AudioError):
        audio.extract_mono(src, dst, [9], 16000)
    assert not dst.exists()
    assert not list(tmp_path.glob("*.partial"))


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_finalize_publishes_manifest_last(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T14-03-22__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw, started_at="2026-09-11T14:03:22+1000")

    assert manifest.meeting_id == "2026-09-11T14-03-22"
    mic = cfg.inbox_dir / manifest.channels[MIC]
    sys_ = cfg.inbox_dir / manifest.channels[SYS]
    assert mic.exists() and sys_.exists()

    # The manifest is what the watcher triggers on, so it must be newer than
    # the audio it points at.
    manifest_path = cfg.inbox_dir / f"{manifest.meeting_id}.json"
    assert manifest_path.exists()
    assert manifest_path.stat().st_mtime >= min(mic.stat().st_mtime, sys_.stat().st_mtime)

    # The raw capture is kept until processing, so a wrong channel map is
    # recoverable rather than fatal.
    assert (cfg.inbox_dir / manifest.raw_file).exists()
    assert not raw.exists()
    assert manifest.duration_s == pytest.approx(2.0, abs=0.2)


@needs_ffmpeg
def test_finalize_manifest_round_trips(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T09-00-00__raw.wav", [True, False, False])
    pipeline.finalize(cfg, raw)
    loaded = Manifest.read(cfg.inbox_dir / "2026-09-11T09-00-00.json")
    assert loaded.channels[MIC].endswith("__mic.wav")
    assert loaded.channels[SYS].endswith("__sys.wav")


def test_finalize_missing_file(cfg):
    with pytest.raises(FileNotFoundError):
        pipeline.finalize(cfg, cfg.in_progress_dir / "nope.wav")


def test_markdown_explains_an_empty_transcript(cfg):
    """A header with nothing under it should say why, not look broken."""
    transcript = Transcript(
        meeting_id="2026-09-11T15-39-19",
        segments=[],
        turns=[],
        duration_s=204.5,
        model="large-v3-turbo",
    )
    markdown = records.render_markdown(transcript, cfg.turn_labeller({}))
    assert "No speech was recognised" in markdown


@needs_ffmpeg
def test_peak_dbfs_separates_digital_silence_from_signal(tmp_path):
    loud = make_wav(tmp_path / "loud.wav", [True])
    quiet = make_wav(tmp_path / "quiet.wav", [False])
    assert audio.peak_dbfs(loud) > -20
    assert audio.peak_dbfs(quiet) == float("-inf")
    assert audio.is_silent(quiet)
    assert not audio.is_silent(loud)


@needs_ffmpeg
def test_finalize_refuses_a_capture_that_is_entirely_silent(cfg):
    """An all-zero capture means the audio never arrived, not a quiet meeting.

    macOS gives an app without microphone permission silence instead of an
    error, so without this check the only symptom is an empty transcript
    produced long after the meeting ended.
    """
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T15-39-19__raw.wav", [False, False, False])
    with pytest.raises(AudioError) as exc:
        pipeline.finalize(cfg, raw, mode="room")

    message = str(exc.value)
    assert "no audio at all" in message
    assert "Microphone" in message  # points at the actual cause

    # The capture is left exactly where it was, and nothing reaches the
    # watcher: a rejected recording must not look like a processed one.
    assert raw.exists()
    assert not list(cfg.inbox_dir.glob("*"))


@needs_ffmpeg
def test_finalize_accepts_a_capture_where_only_one_channel_is_silent(cfg):
    """Nobody speaking on the far end of a call is normal, not a fault."""
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T16-00-00__raw.wav", [False, False, True])
    manifest = pipeline.finalize(cfg, raw)
    assert (cfg.inbox_dir / f"{manifest.meeting_id}.json").exists()


@needs_ffmpeg
def test_finalize_rejects_audio_duration_that_disagrees_with_wall_time(cfg):
    raw = make_wav(
        cfg.in_progress_dir / "2026-09-11T16-21-31__raw.wav",
        [False, False, True],
        duration=10.0,
    )
    with pytest.raises(AudioError) as exc:
        pipeline.finalize(
            cfg, raw, mode="room", expected_duration_s=36.0
        )
    assert "10.0s of audio" in str(exc.value)
    assert "36.0s" in str(exc.value)
    assert "sped-up, incomplete transcript" in str(exc.value)
    assert raw.exists()
    assert not list(cfg.inbox_dir.glob("*"))


@needs_ffmpeg
def test_finalize_allows_small_start_stop_timing_difference(cfg):
    raw = make_wav(
        cfg.in_progress_dir / "2026-09-11T16-30-00__raw.wav",
        [False, False, True],
        duration=10.0,
    )
    manifest = pipeline.finalize(
        cfg, raw, mode="room", expected_duration_s=11.0
    )
    assert manifest.duration_s == pytest.approx(10.0, abs=0.2)


# ---------------------------------------------------------------------------
# process
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_process_writes_transcript_indexes_and_archives(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T14-03-22__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw)
    meeting_id = manifest.meeting_id

    transcriber = FakeTranscriber({
        MIC: [Segment(MIC, 0.0, 3.0, "Hey John, thanks for making time.")],
        SYS: [Segment(SYS, 3.5, 6.0, "No problem. I'm John, by the way.")],
    })
    store = FakeStore()

    assert pipeline.process(cfg, cfg.inbox_dir / f"{meeting_id}.json", transcriber, store) == meeting_id

    # Both channels transcribed.
    assert {c[1] for c in transcriber.calls} == {MIC, SYS}

    # Transcript on disk, in both formats.
    assert cfg.transcript_json(meeting_id).exists()
    assert cfg.transcript_md(meeting_id).exists()

    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    assert [t.channel for t in transcript.turns] == [MIC, SYS]
    assert len(transcript.segments) == 2  # raw segments kept for v2 diarization
    assert [c.name for c in transcript.detected_names if c.attributed_to == "other"] == ["John"]

    # Indexed.
    assert meeting_id in store.meetings

    # Audio moved out of the inbox and into the retention window.
    assert not list(cfg.inbox_dir.glob("*"))
    assert not list(cfg.processing_dir.glob("*"))
    archived = sorted(p.name for p in cfg.audio_archive_dir.glob("*"))
    assert any(name.endswith("__mic.wav") for name in archived)
    assert any(name.endswith("__raw.wav") for name in archived)
    # The manifest has served its purpose and is not kept.
    assert not any(name.endswith(".json") for name in archived)


@needs_ffmpeg
def test_process_puts_local_summary_above_transcript(cfg):
    raw = make_wav(
        cfg.in_progress_dir / "2026-09-11T14-30-00__raw.wav",
        [True, False, False],
    )
    manifest = pipeline.finalize(cfg, raw, mode="room")
    transcriber = FakeTranscriber({
        ROOM: [Segment(ROOM, 0.0, 3.0, "We agreed to ship on Friday.")]
    })

    class FakeSummarizer:
        enabled = True

        def is_running(self):
            return True

        def summarize(self, turns, label_fn):
            assert "ship on Friday" in turns[0].text
            return "The team agreed on a Friday release.\n\n**Decisions**\n- Ship Friday."

    pipeline.process(
        cfg,
        cfg.inbox_dir / f"{manifest.meeting_id}.json",
        transcriber,
        FakeStore(),
        summarizer=FakeSummarizer(),
    )

    transcript = Transcript.read(cfg.transcript_json(manifest.meeting_id))
    assert "Friday release" in transcript.summary
    markdown = cfg.transcript_md(manifest.meeting_id).read_text()
    assert markdown.index("## Summary") < markdown.index("---")
    assert "**Decisions**" in markdown


@needs_ffmpeg
def test_process_defers_summary_when_ollama_is_off(cfg):
    from meeting_archive.summarize import DEFERRED_SUMMARY

    raw = make_wav(
        cfg.in_progress_dir / "2026-09-11T14-36-00__raw.wav",
        [True, False, False],
    )
    manifest = pipeline.finalize(cfg, raw, mode="room")

    class OffSummarizer:
        enabled = True

        def is_running(self):
            return False

    pipeline.process(
        cfg,
        cfg.inbox_dir / f"{manifest.meeting_id}.json",
        FakeTranscriber({ROOM: [Segment(ROOM, 0, 2, "We agreed to ship on Friday.")]}),
        FakeStore(),
        summarizer=OffSummarizer(),
    )
    transcript = Transcript.read(cfg.transcript_json(manifest.meeting_id))
    assert transcript.summary == ""
    assert transcript.summary_error == DEFERRED_SUMMARY
    assert "We agreed to ship on Friday" in cfg.transcript_md(manifest.meeting_id).read_text()


@needs_ffmpeg
def test_apply_summary_fills_a_deferred_meeting(cfg):
    from meeting_archive.summarize import DEFERRED_SUMMARY

    meeting_id = "2026-09-11T14-36-00"
    turns = [Turn(ROOM, 0, 3, "We agreed to ship on Friday.")]
    Transcript(
        meeting_id=meeting_id,
        turns=turns,
        summary="",
        summary_error=DEFERRED_SUMMARY,
    ).write(cfg.transcript_json(meeting_id))

    class LaterSummarizer:
        enabled = True

        def is_running(self):
            return True

        def summarize(self, turns, label_fn):
            return "Ship Friday."

    assert pipeline.apply_summary(cfg, meeting_id, LaterSummarizer(), FakeStore())
    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    assert transcript.summary == "Ship Friday."
    assert transcript.summary_error == ""
    assert "Ship Friday" in cfg.transcript_md(meeting_id).read_text()


@needs_ffmpeg
def test_process_reports_progress_phases_and_clears_when_done(cfg, monkeypatch):
    raw = make_wav(
        cfg.in_progress_dir / "2026-09-11T14-35-00__raw.wav",
        [True, False, False],
    )
    manifest = pipeline.finalize(cfg, raw, mode="room")
    events = []
    monkeypatch.setattr(
        pipeline,
        "_processing_status",
        lambda cfg, meeting_id, phase, percent, detail="", **_kwargs: events.append(
            (phase, percent, detail)
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_clear_processing_status",
        lambda cfg, meeting_id: events.append(("cleared", 100, "")),
    )

    pipeline.process(
        cfg,
        cfg.inbox_dir / f"{manifest.meeting_id}.json",
        FakeTranscriber({ROOM: [Segment(ROOM, 0, 2, "A short meeting.")]}),
        FakeStore(),
    )

    phases = [phase for phase, _, _ in events]
    assert phases[0] == "Preparing audio"
    assert "Transcribing" in phases
    assert "Identifying speakers" in phases
    assert "Saving transcript" in phases
    assert "Indexing" in phases
    assert phases[-1] == "cleared"
    assert all(0 <= percent <= 100 for _, percent, _ in events)
    # Honest progress: no invented global 65/82 waypoints.
    speaker_events = [percent for phase, percent, _ in events if phase == "Identifying speakers"]
    assert speaker_events
    assert all(p == 0 for p in speaker_events)


@needs_ffmpeg
def test_process_quarantines_a_failed_meeting_and_keeps_its_audio(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T15-00-00__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw)

    class Exploding:
        def transcribe_file(self, path, channel, duration_s=0.0, progress=None):
            raise RuntimeError("model failed to load")

    with pytest.raises(RuntimeError):
        pipeline.process(cfg, cfg.inbox_dir / f"{manifest.meeting_id}.json", Exploding(), FakeStore())

    # Nothing is lost: the audio is in failed/, not deleted, and not left in a
    # state where the watcher would retry it forever.
    failed = sorted(p.name for p in cfg.failed_dir.glob("*"))
    assert any(n.endswith("__mic.wav") for n in failed)
    assert any(n.endswith("__raw.wav") for n in failed)
    assert not list(cfg.inbox_dir.glob("*"))
    assert not cfg.transcript_json(manifest.meeting_id).exists()
    assert not cfg.processing_status.exists()


# ---------------------------------------------------------------------------
# diarization, end to end through the pipeline
# ---------------------------------------------------------------------------

class FakeDiarizer:
    """Stands in for pyannote, returning canned spans per channel.

    Mirrors the real Diarizer's interface, including `channel_enabled`, so the
    test exercises the same guard that keeps the mic channel out.
    """

    def __init__(self, per_channel=None, channels=(SYS, ROOM), fail=None):
        self.per_channel = per_channel or {}
        self.channels = set(channels)
        self.fail = fail
        self.calls = []

    @property
    def enabled(self):
        return True

    def channel_enabled(self, channel):
        return channel in self.channels

    def diarize_file(self, path, channel, expected_speakers=None):
        assert Path(path).exists(), f"diarizer handed a missing file: {path}"
        self.calls.append((Path(path).name, channel, expected_speakers))
        if self.fail:
            raise self.fail
        return list(self.per_channel.get(channel, []))


@needs_ffmpeg
def test_room_meeting_separates_the_voices_and_never_names_them(cfg):
    """Three people on one microphone come out as three numbered speakers.

    The names said in the room are detected and offered, but no turn is
    labelled with one until Jacob confirms it.
    """
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T09-00-00__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw, mode="room")
    meeting_id = manifest.meeting_id

    transcriber = FakeTranscriber({
        ROOM: [
            Segment(ROOM, 0.0, 4.0, "Morning. I'm Marina, I run the data team."),
            Segment(ROOM, 5.0, 9.0, "I drove up from Brighton this morning."),
            Segment(ROOM, 10.0, 13.0, "Shall we start with the pricing model?"),
        ],
    })
    diarizer = FakeDiarizer({
        ROOM: [
            SpeakerSpan(ROOM, 0.0, 4.5, "room-1"),
            SpeakerSpan(ROOM, 4.8, 9.5, "room-2"),
            SpeakerSpan(ROOM, 9.8, 13.5, "room-3"),
        ],
    })
    store = FakeStore()

    pipeline.process(
        cfg, cfg.inbox_dir / f"{meeting_id}.json", transcriber, store, diarizer
    )

    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    assert [t.speaker for t in transcript.turns] == ["room-1", "room-2", "room-3"]
    assert [p.speaker for p in transcript.speaker_profiles] == [
        "room-1", "room-2", "room-3"
    ]

    # Each voice has something quotable to identify it by.
    quips = {p.speaker: p.quip for p in transcript.speaker_profiles}
    assert "Brighton" in quips["room-2"]

    # Marina's self-introduction is offered for her voice, not applied to it.
    assert {g.speaker: g.name for g in transcript.speaker_guesses} == {"room-1": "Marina"}
    markdown = cfg.transcript_md(meeting_id).read_text(encoding="utf-8")
    assert "Speaker 1:" in markdown
    assert "Marina:" not in markdown
    # And the honest room label is gone now that the voices are distinguished.
    assert f"{cfg.room_label}:" not in markdown


@needs_ffmpeg
def test_listed_voices_constrain_room_diarization(cfg):
    raw = make_wav(
        cfg.in_progress_dir / "2026-09-11T09-15-00__raw.wav",
        [True, False, False],
    )
    manifest = pipeline.finalize(cfg, raw, mode="room")
    records.write_notes(
        cfg.notes_json(manifest.meeting_id),
        meeting_id=manifest.meeting_id,
        attendees="Jacob, Bert, Leanne",
    )
    diarizer = FakeDiarizer({
        ROOM: [SpeakerSpan(ROOM, 0.0, 2.0, "room-1")],
    })

    pipeline.process(
        cfg,
        cfg.inbox_dir / f"{manifest.meeting_id}.json",
        FakeTranscriber({ROOM: [Segment(ROOM, 0.0, 2.0, "Testing.")]}),
        FakeStore(),
        diarizer,
    )

    assert diarizer.calls == [
        (f"{manifest.meeting_id}__room.wav", ROOM, 3),
    ]


@needs_ffmpeg
def test_mic_channel_is_never_diarized(cfg):
    """The mic is one person by construction; the diarizer must not see it."""
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T09-30-00__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw)
    meeting_id = manifest.meeting_id

    transcriber = FakeTranscriber({
        MIC: [Segment(MIC, 0.0, 3.0, "Morning both.")],
        SYS: [Segment(SYS, 4.0, 7.0, "Morning.")],
    })
    diarizer = FakeDiarizer({SYS: [SpeakerSpan(SYS, 3.5, 7.5, "sys-1")]})

    pipeline.process(
        cfg, cfg.inbox_dir / f"{meeting_id}.json", transcriber, store := FakeStore(), diarizer
    )

    assert [c[1] for c in diarizer.calls] == [SYS]
    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    by_channel = {t.channel: t for t in transcript.turns}
    assert by_channel[MIC].speaker is None
    assert by_channel[SYS].speaker == "sys-1"

    markdown = cfg.transcript_md(meeting_id).read_text(encoding="utf-8")
    assert f"{cfg.own_label}:" in markdown
    assert store.meetings


@needs_ffmpeg
def test_diarization_failure_still_produces_a_full_transcript(cfg):
    """A missing local model file must not cost a meeting.

    This is the one place the pipeline degrades instead of quarantining, so
    the transcript, the index and the archived audio all have to survive, with
    the reason recorded where Jacob will see it.
    """
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T11-00-00__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw, mode="room")
    meeting_id = manifest.meeting_id

    transcriber = FakeTranscriber({
        ROOM: [Segment(ROOM, 0.0, 4.0, "We should talk about the renewal.")],
    })
    diarizer = FakeDiarizer(fail=DiarizationUnavailable("local model missing"))
    store = FakeStore()

    assert pipeline.process(
        cfg, cfg.inbox_dir / f"{meeting_id}.json", transcriber, store, diarizer
    ) == meeting_id

    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    assert transcript.turns  # the transcript is complete
    assert transcript.speaker_spans == []
    assert "model missing" in transcript.diarize_error
    assert meeting_id in store.meetings  # and it was indexed

    # Falls back to the honest channel label rather than inventing speakers.
    markdown = cfg.transcript_md(meeting_id).read_text(encoding="utf-8")
    assert f"{cfg.room_label}:" in markdown
    assert "Diarization unavailable" in markdown

    # Audio archived normally, not quarantined.
    assert not list(cfg.failed_dir.glob("*"))
    assert list(cfg.audio_archive_dir.glob("*__room.wav"))


@needs_ffmpeg
def test_process_without_a_diarizer_behaves_as_before(cfg):
    """Passing no diarizer must reproduce the pre-diarization pipeline."""
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T12-00-00__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw, mode="room")
    meeting_id = manifest.meeting_id

    transcriber = FakeTranscriber({ROOM: [Segment(ROOM, 0.0, 4.0, "Anything at all.")]})
    pipeline.process(cfg, cfg.inbox_dir / f"{meeting_id}.json", transcriber, FakeStore())

    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    assert transcript.speaker_spans == []
    assert transcript.diarize_error == ""
    assert transcript.turns[0].speaker is None
    markdown = cfg.transcript_md(meeting_id).read_text(encoding="utf-8")
    assert f"{cfg.room_label}:" in markdown


def test_naming_a_voice_relabels_the_transcript_on_reindex(cfg):
    """Identifying Speaker 2 an hour later costs a reindex, not a re-transcribe.

    This is the whole reason the roster lives in the notes file: the watcher's
    existing notes sweep already does exactly this.
    """
    meeting_id = "2026-09-11T16-00-00"
    turns = [
        Turn(ROOM, 0.0, 4.0, "I'm Marina and I run the data team.", speaker="room-1"),
        Turn(ROOM, 5.0, 9.0, "I came up from Brighton this morning.", speaker="room-2"),
    ]
    transcript = Transcript(
        meeting_id=meeting_id,
        segments=[],
        turns=turns,
        duration_s=600.0,
        speaker_profiles=profile_speakers(turns),
        diarize_model="sherpa-onnx-pyannote-segmentation-3.0",
    )
    transcript.write(cfg.transcript_json(meeting_id))
    store = FakeStore()

    # Before: numbered, unnamed.
    pipeline.ingest(cfg, meeting_id, store)
    assert "Speaker 1:" in cfg.transcript_md(meeting_id).read_text(encoding="utf-8")

    # Jacob names them, exactly as the dialog would.
    records.write_notes(
        cfg.notes_json(meeting_id),
        meeting_id=meeting_id,
        speakers={"room-1": "Marina", "room-2": "Ben"},
        applied=False,
    )
    pipeline.ingest(cfg, meeting_id, store)

    markdown = cfg.transcript_md(meeting_id).read_text(encoding="utf-8")
    assert "Marina:" in markdown
    assert "Ben:" in markdown
    assert "Speaker 1:" not in markdown

    # The chunk text and its speakers metadata agree, so a search filtered on
    # a name cannot return a chunk that never mentions them.
    documents, metadatas = store.meetings[meeting_id]
    assert "Marina:" in documents[0]
    assert "Marina" in metadatas[0]["speakers"]
    assert "Marina" in metadatas[0]["identified_speakers"]

    # The notes sweep marked them applied, so it won't reindex forever.
    assert records.read_notes(cfg.notes_json(meeting_id))["applied"] is True


def test_speaker_review_warns_when_attendees_outnumber_detected_voices(
    cfg, capsys
):
    meeting_id = "2026-09-11T16-40-00"
    turns = [Turn(ROOM, 0, 5, "We discussed pricing.", speaker="room-1")]
    Transcript(
        meeting_id=meeting_id,
        turns=turns,
        speaker_profiles=profile_speakers(turns),
        diarize_model="test",
    ).write(cfg.transcript_json(meeting_id))
    records.write_notes(
        cfg.notes_json(meeting_id),
        meeting_id,
        attendees="AJ, Tony",
    )

    args = SimpleNamespace(
        meeting_id=meeting_id,
        set=None,
        pending_tsv=False,
        review_warning=True,
    )
    assert cli.cmd_speakers(cfg, args) == 0
    message = capsys.readouterr().out
    assert "AJ, Tony" in message
    assert "detected only 1 distinct voice" in message
    assert "will not be assigned automatically" in message


def test_watcher_asks_who_each_voice_was(cfg, monkeypatch):
    """The identification dialog fires once the transcript exists."""
    meeting_id = "2026-09-11T17-00-00"
    turns = [
        Turn(ROOM, 0.0, 4.0, "I handle the billing integrations here.", speaker="room-1"),
        Turn(ROOM, 5.0, 9.0, "I came up from Brighton this morning.", speaker="room-2"),
    ]
    Transcript(
        meeting_id=meeting_id,
        turns=turns,
        speaker_profiles=profile_speakers(turns),
        diarize_model="sherpa-onnx-pyannote-segmentation-3.0",
    ).write(cfg.transcript_json(meeting_id))

    launched = []
    monkeypatch.setattr(
        watcher.popup, "launch_speakers",
        lambda mid, profiles: launched.append((mid, [p.speaker for p in profiles])),
    )

    watcher._maybe_identify_speakers(cfg, meeting_id)
    assert launched == [(meeting_id, ["room-1", "room-2"])]

    # Already-named voices are not asked about again.
    launched.clear()
    records.write_notes(
        cfg.notes_json(meeting_id), meeting_id=meeting_id,
        speakers={"room-1": "Ana", "room-2": "Ben"}, applied=False,
    )
    watcher._maybe_identify_speakers(cfg, meeting_id)
    assert launched == []


def test_identification_dialogs_have_no_six_speaker_ceiling(cfg, monkeypatch):
    """A real 6-8 person meeting must still offer speaker identification."""
    meeting_id = "2026-09-11T18-00-00"
    turns = [
        Turn(ROOM, float(i), float(i) + 1.0, f"I am person number {i} of many.",
             speaker=f"room-{i + 1}")
        for i in range(8)
    ]
    Transcript(
        meeting_id=meeting_id,
        turns=turns,
        speaker_profiles=profile_speakers(turns),
        diarize_model="sherpa-onnx-pyannote-segmentation-3.0",
    ).write(cfg.transcript_json(meeting_id))

    launched = []
    monkeypatch.setattr(
        watcher.popup, "launch_speakers", lambda mid, profiles: launched.append(mid)
    )
    watcher._maybe_identify_speakers(cfg, meeting_id)
    assert launched == [meeting_id]


def test_no_identification_dialog_when_nothing_was_diarized(cfg, monkeypatch):
    meeting_id = "2026-09-11T19-00-00"
    Transcript(
        meeting_id=meeting_id,
        turns=[Turn(SYS, 0.0, 4.0, "Just one undifferentiated channel.")],
    ).write(cfg.transcript_json(meeting_id))

    launched = []
    monkeypatch.setattr(
        watcher.popup, "launch_speakers", lambda mid, profiles: launched.append(mid)
    )
    watcher._maybe_identify_speakers(cfg, meeting_id)
    assert launched == []


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def write_transcript(cfg: Config, meeting_id: str, turns: list[Turn], names=None) -> Transcript:
    transcript = Transcript(
        meeting_id=meeting_id,
        segments=[],
        turns=turns,
        detected_names=names or [],
        duration_s=600.0,
        model="large-v3-turbo",
    )
    transcript.write(cfg.transcript_json(meeting_id))
    return transcript


def test_ingest_metadata_is_chroma_safe(cfg):
    write_transcript(cfg, "2026-09-11T14-03-22", [
        Turn(MIC, 0.0, 5.0, "Let's go through the pricing."),
        Turn(SYS, 5.0, 9.0, "Sure, the current model is per seat."),
    ])
    store = FakeStore()
    assert pipeline.ingest(cfg, "2026-09-11T14-03-22", store) == 1

    _, metadatas = store.meetings["2026-09-11T14-03-22"]
    for metadata in metadatas:
        for key, value in metadata.items():
            assert isinstance(value, (str, int, float, bool)), f"{key} is {type(value)}"
            assert value is not None
    meta = metadatas[0]
    assert meta["meeting_id"] == "2026-09-11T14-03-22"
    assert meta["date"] == "2026-09-11"
    assert meta["epoch"] > 0
    # A list of speakers must arrive as a joined string, not a list.
    assert meta["speakers"] == "Jacob, Other"
    assert meta["has_notes"] is False


def test_ingest_includes_context_header_in_the_embedded_text(cfg):
    write_transcript(cfg, "2026-09-11T14-03-22", [Turn(MIC, 0.0, 5.0, "Pricing.")])
    store = FakeStore()
    pipeline.ingest(cfg, "2026-09-11T14-03-22", store)
    documents, _ = store.meetings["2026-09-11T14-03-22"]
    assert documents[0].startswith("Meeting Fri 11 Sep 2026 14:03")
    assert "Jacob: Pricing." in documents[0]


def test_ingest_picks_up_notes_written_before_processing_finished(cfg):
    meeting_id = "2026-09-11T14-03-22"
    # Notes arrive first -- Jacob typed fast, or Whisper was slow.
    records.write_notes(
        cfg.notes_json(meeting_id),
        meeting_id,
        notes="they pushed back on price",
        attendees="John, Marina",
        applied=False,
    )
    write_transcript(cfg, meeting_id, [Turn(MIC, 0.0, 5.0, "Pricing.")])

    store = FakeStore()
    pipeline.ingest(cfg, meeting_id, store)

    documents, metadatas = store.meetings[meeting_id]
    assert "John, Marina" in documents[0]
    assert metadatas[0]["notes"] == "they pushed back on price"
    assert metadatas[0]["has_notes"] is True
    # And the notes are marked applied so the watcher stops reindexing.
    assert records.read_notes(cfg.notes_json(meeting_id))["applied"] is True


def test_ingest_is_idempotent_and_deletes_before_adding(cfg):
    meeting_id = "2026-09-11T14-03-22"
    write_transcript(cfg, meeting_id, [Turn(MIC, 0.0, 5.0, "x" * 3000)])
    store = FakeStore()
    first = pipeline.ingest(cfg, meeting_id, store)
    second = pipeline.ingest(cfg, meeting_id, store)
    assert first == second
    # Delete-then-add, so a changing chunk count can't orphan old chunks.
    assert store.deletes == [meeting_id, meeting_id]


def test_ingest_rewrites_markdown_with_notes(cfg):
    meeting_id = "2026-09-11T14-03-22"
    write_transcript(
        cfg,
        meeting_id,
        [Turn(MIC, 0.0, 5.0, "Hey John.")],
        names=[NameCandidate(name="John", count=1, attributed_to="other")],
    )
    pipeline.ingest(cfg, meeting_id, FakeStore())
    text = cfg.transcript_md(meeting_id).read_text()
    assert "Names heard in conversation: John" in text
    assert "[0:00:00] Jacob:" in text

    records.write_notes(cfg.notes_json(meeting_id), meeting_id, notes="good call", applied=False)
    pipeline.ingest(cfg, meeting_id, FakeStore())
    assert "good call" in cfg.transcript_md(meeting_id).read_text()
    assert cfg.transcript_md(meeting_id).name == "2026-09-11 good call.md"
    assert not (cfg.transcripts_dir / "2026" / "09" / f"{meeting_id}.md").exists()


def test_ingest_without_a_transcript_fails_clearly(cfg):
    with pytest.raises(FileNotFoundError):
        pipeline.ingest(cfg, "2026-01-01T00-00-00", FakeStore())


def test_chunk_ids_are_stable_and_sortable():
    assert chunk_id("2026-09-11T14-03-22", 7) == "2026-09-11T14-03-22#0007"
    assert sorted([chunk_id("m", 10), chunk_id("m", 2)]) == ["m#0002", "m#0010"]


# ---------------------------------------------------------------------------
# metadata sanitisation
# ---------------------------------------------------------------------------

def test_sanitize_metadata_flattens_lists_and_none():
    out = sanitize_metadata({
        "speakers": ["Jacob", "Other"],
        "notes": None,
        "count": 3,
        "ratio": 0.5,
        "flag": True,
        "path": Path("/tmp/x.md"),
    })
    assert out["speakers"] == "Jacob, Other"
    assert out["notes"] == ""
    assert out["count"] == 3
    assert out["ratio"] == 0.5
    assert out["flag"] is True
    assert out["path"] == "/tmp/x.md"


def test_sanitize_metadata_keeps_bools_as_bools():
    # bool is a subclass of int, so the order of isinstance checks matters.
    out = sanitize_metadata({"has_notes": False})
    assert out["has_notes"] is False
    assert not isinstance(out["has_notes"], int) or out["has_notes"] is False


# ---------------------------------------------------------------------------
# notes handling
# ---------------------------------------------------------------------------

def test_write_notes_does_not_wipe_earlier_answers(cfg):
    path = cfg.notes_json("m1")
    records.write_notes(path, "m1", notes="first thoughts", attendees="John")
    records.write_notes(path, "m1", notes="", attendees="John, Marina")
    data = records.read_notes(path)
    assert data["notes"] == "first thoughts"
    assert data["attendees"] == "John, Marina"


def test_read_notes_survives_a_corrupt_file(cfg):
    path = cfg.notes_json("m1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert records.read_notes(path) == {}


def test_read_notes_missing_file(cfg):
    assert records.read_notes(cfg.notes_json("nope")) == {}


# ---------------------------------------------------------------------------
# watcher sweeps
# ---------------------------------------------------------------------------

def test_sweep_notes_reindexes_only_unapplied_notes(cfg):
    applied_id, pending_id = "2026-09-11T10-00-00", "2026-09-11T11-00-00"
    for meeting_id in (applied_id, pending_id):
        write_transcript(cfg, meeting_id, [Turn(MIC, 0.0, 1.0, "Hello.")])
    records.write_notes(cfg.notes_json(applied_id), applied_id, notes="done", applied=True)
    records.write_notes(cfg.notes_json(pending_id), pending_id, notes="new", applied=False)

    store = FakeStore()
    assert watcher.sweep_notes(cfg, store) == 1
    assert pending_id in store.meetings
    assert applied_id not in store.meetings

    # Second sweep is a no-op: the note is now marked applied.
    assert watcher.sweep_notes(cfg, FakeStore()) == 0


def test_sweep_notes_waits_for_a_transcript_that_does_not_exist_yet(cfg):
    meeting_id = "2026-09-11T12-00-00"
    records.write_notes(cfg.notes_json(meeting_id), meeting_id, notes="quick", applied=False)
    store = FakeStore()
    assert watcher.sweep_notes(cfg, store) == 0
    # Still pending, so it gets picked up once transcription finishes.
    assert records.read_notes(cfg.notes_json(meeting_id))["applied"] is False


def test_sweep_notes_ignores_partial_writes(cfg):
    (cfg.notes_dir / "x.json.partial").write_text('{"applied": false}', encoding="utf-8")
    assert watcher.sweep_notes(cfg, FakeStore()) == 0


@needs_ffmpeg
def test_sweep_inbox_processes_everything_waiting(cfg):
    for stamp in ("2026-09-11T08-00-00", "2026-09-11T09-00-00"):
        raw = make_wav(cfg.in_progress_dir / f"{stamp}__raw.wav", [True, False, False])
        pipeline.finalize(cfg, raw)

    transcriber = FakeTranscriber({MIC: [Segment(MIC, 0.0, 1.0, "Morning.")]})
    assert watcher.sweep_inbox(cfg, transcriber, FakeStore()) == 2
    assert not list(cfg.inbox_dir.glob("*.json"))


@needs_ffmpeg
def test_sweep_inbox_keeps_going_after_one_meeting_fails(cfg):
    stamps = ["2026-09-11T08-00-00", "2026-09-11T09-00-00"]
    for stamp in stamps:
        raw = make_wav(cfg.in_progress_dir / f"{stamp}__raw.wav", [True, False, False])
        pipeline.finalize(cfg, raw)

    class FailsOnce:
        def __init__(self):
            self.seen = 0

        def transcribe_file(self, path, channel, duration_s=0.0, progress=None):
            self.seen += 1
            if self.seen == 1:
                raise RuntimeError("boom")
            return [Segment(channel, 0.0, 1.0, "Fine.")]

    # One meeting is quarantined, the other still gets processed.
    assert watcher.sweep_inbox(cfg, FailsOnce(), FakeStore()) == 1
    assert list(cfg.failed_dir.glob("*__raw.wav"))


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def test_prune_deletes_only_old_audio_with_a_transcript(cfg):
    now = time.time()
    old = now - 30 * 86400
    recent = now - 1 * 86400

    write_transcript(cfg, "2026-08-01T10-00-00", [Turn(MIC, 0.0, 1.0, "old")])
    write_transcript(cfg, "2026-09-10T10-00-00", [Turn(MIC, 0.0, 1.0, "recent")])

    files = {
        "2026-08-01T10-00-00__mic.wav": old,     # old, transcribed -> delete
        "2026-09-10T10-00-00__mic.wav": recent,  # inside window -> keep
        "2026-08-02T10-00-00__mic.wav": old,     # old, NOT transcribed -> keep
    }
    for name, mtime in files.items():
        path = cfg.audio_archive_dir / name
        path.write_bytes(b"x" * 1024)
        os.utime(path, (mtime, mtime))

    result = retention.prune(cfg, now=now)
    assert result.deleted == ["2026-08-01T10-00-00__mic.wav"]
    assert result.kept_no_transcript == ["2026-08-02T10-00-00__mic.wav"]
    assert not (cfg.audio_archive_dir / "2026-08-01T10-00-00__mic.wav").exists()
    assert (cfg.audio_archive_dir / "2026-08-02T10-00-00__mic.wav").exists()
    assert (cfg.audio_archive_dir / "2026-09-10T10-00-00__mic.wav").exists()
    assert result.bytes_freed == 1024


def test_prune_dry_run_deletes_nothing(cfg):
    now = time.time()
    write_transcript(cfg, "2026-08-01T10-00-00", [Turn(MIC, 0.0, 1.0, "old")])
    path = cfg.audio_archive_dir / "2026-08-01T10-00-00__mic.wav"
    path.write_bytes(b"x" * 10)
    os.utime(path, (now - 30 * 86400,) * 2)

    result = retention.prune(cfg, dry_run=True, now=now)
    assert result.deleted == ["2026-08-01T10-00-00__mic.wav"]
    assert path.exists()


def test_prune_on_an_empty_archive(cfg):
    result = retention.prune(cfg)
    assert result.deleted == []
    assert result.bytes_freed == 0


# ---------------------------------------------------------------------------
# record round-trips
# ---------------------------------------------------------------------------

def test_transcript_round_trip_preserves_everything(cfg):
    original = Transcript(
        meeting_id="2026-09-11T14-03-22",
        segments=[Segment(MIC, 0.0, 1.5, "Hello.")],
        turns=[Turn(MIC, 0.0, 1.5, "Hello.")],
        detected_names=[
            NameCandidate(name="John", count=2, attributed_to="other",
                          patterns={"greeting_vocative"}, evidence="Hey John")
        ],
        duration_s=95.5,
        model="large-v3-turbo",
    )
    path = cfg.transcript_json(original.meeting_id)
    original.write(path)
    loaded = Transcript.read(path)

    assert loaded.meeting_id == original.meeting_id
    assert loaded.turns[0].text == "Hello."
    assert loaded.segments[0].channel == MIC
    assert loaded.duration_s == 95.5
    assert loaded.detected_names[0].patterns == {"greeting_vocative"}


def test_transcripts_are_sharded_by_year_and_month(cfg):
    path = cfg.transcript_json("2026-09-11T14-03-22")
    assert path.parent.name == "09"
    assert path.parent.parent.name == "2026"


def test_atomic_write_leaves_no_partial_files(cfg):
    path = cfg.notes_dir / "x.json"
    records.write_json_atomic(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert not list(cfg.notes_dir.glob("*.partial"))


def test_date_label_falls_back_for_an_unparseable_id():
    assert records.date_label("not-a-timestamp") == "not-a-timestamp"


def test_new_meeting_id_round_trips():
    meeting_id = records.new_meeting_id()
    assert records.parse_meeting_id(meeting_id)
    assert len(meeting_id) == len("2026-09-11T14-03-22")


# ---------------------------------------------------------------------------
# room mode
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_finalize_room_mode_produces_one_channel(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T16-00-00__raw.wav", [False, False, True])
    cfg.audio.mic_channels = [2]
    cfg.audio.sys_channels = [0, 1]

    manifest = pipeline.finalize(cfg, raw, mode="room")

    assert list(manifest.channels) == [ROOM]
    room_file = cfg.inbox_dir / manifest.channels[ROOM]
    assert room_file.exists()
    # The mic content is what landed in it, not the silent system channels.
    assert mean_volume_db(room_file) > -40
    # No silent sys channel was created for Whisper to hallucinate into.
    assert not list(cfg.inbox_dir.glob("*__sys.wav"))


@needs_ffmpeg
def test_finalize_rejects_an_unknown_mode(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T16-00-00__raw.wav", [True, False, False])
    with pytest.raises(ValueError):
        pipeline.finalize(cfg, raw, mode="hybrid")


@needs_ffmpeg
def test_room_recording_is_labelled_unknown_not_jacob(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T16-00-00__raw.wav", [False, False, True])
    cfg.audio.mic_channels = [2]
    manifest = pipeline.finalize(cfg, raw, mode="room")

    transcriber = FakeTranscriber({
        ROOM: [
            Segment(ROOM, 0.0, 4.0, "Hey Marina, where did we land on the renewal?"),
            Segment(ROOM, 6.0, 9.0, "We agreed to push it to next quarter."),
        ]
    })
    store = FakeStore()
    pipeline.process(cfg, cfg.inbox_dir / f"{manifest.meeting_id}.json", transcriber, store)

    text = cfg.transcript_md(manifest.meeting_id).read_text()
    # The critical assertion: nothing said in a room is attributed to Jacob,
    # because there is no way to know who said it.
    assert "Jacob:" not in text
    assert "Unknown:" in text

    _, metadatas = store.meetings[manifest.meeting_id]
    assert metadatas[0]["speakers"] == "Unknown"


@needs_ffmpeg
def test_room_recording_detects_a_name_a_call_recording_would_discard(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T16-00-00__raw.wav", [False, False, True])
    cfg.audio.mic_channels = [2]
    cfg.names.own_names = ["Jacob"]
    manifest = pipeline.finalize(cfg, raw, mode="room")

    transcriber = FakeTranscriber({
        ROOM: [Segment(ROOM, 0.0, 4.0, "Morning everyone. I'm Marina, I run billing.")]
    })
    pipeline.process(cfg, cfg.inbox_dir / f"{manifest.meeting_id}.json", transcriber, FakeStore())

    transcript = Transcript.read(cfg.transcript_json(manifest.meeting_id))
    assert [c.name for c in transcript.detected_names if c.attributed_to == "other"] == ["Marina"]


@needs_ffmpeg
def test_process_handles_a_manifest_with_no_channels(cfg):
    raw = make_wav(cfg.in_progress_dir / "2026-09-11T16-00-00__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw)
    manifest_path = cfg.inbox_dir / f"{manifest.meeting_id}.json"
    manifest.channels = {}
    manifest.write(manifest_path)

    with pytest.raises(ValueError):
        pipeline.process(cfg, manifest_path, FakeTranscriber(), FakeStore())
    assert list(cfg.failed_dir.glob("*"))


# ---------------------------------------------------------------------------
# device listing
# ---------------------------------------------------------------------------

# A real listing from Jacob's machine, including the trailing error ffmpeg
# always emits after listing devices.
SAMPLE_LISTING = """[AVFoundation indev @ 0x123f05300] AVFoundation video devices:
[AVFoundation indev @ 0x123f05300] [0] FaceTime HD Camera
[AVFoundation indev @ 0x123f05300] [1] Capture screen 0
[AVFoundation indev @ 0x123f05300] [2] Capture screen 1
[AVFoundation indev @ 0x123f05300] AVFoundation audio devices:
[AVFoundation indev @ 0x123f05300] [0] Jacob's AirPods
[AVFoundation indev @ 0x123f05300] [1] ZoomAudioDevice
[AVFoundation indev @ 0x123f05300] [2] BlackHole 2ch
[AVFoundation indev @ 0x123f05300] [3] MacBook Pro Microphone
[AVFoundation indev @ 0x123f05300] [4] Jump Desktop Microphone
[AVFoundation indev @ 0x123f05300] [5] Jump Desktop Audio
[AVFoundation indev @ 0x123f05300] [6] Aggregate Device
[in#0 @ 0x123f04800] Error opening input: Input/output error
Error opening input file .
"""


def test_parse_audio_devices_ignores_the_video_block():
    devices = audio.parse_audio_devices(SAMPLE_LISTING)
    # Video devices share the same numbering, so index 0 must be the AirPods
    # and not the FaceTime camera.
    assert devices[0] == "Jacob's AirPods"
    assert devices[6] == "Aggregate Device"
    assert "FaceTime HD Camera" not in devices.values()
    assert "Capture screen 0" not in devices.values()


def test_parse_audio_devices_handles_the_trailing_ffmpeg_error():
    devices = audio.parse_audio_devices(SAMPLE_LISTING)
    assert len(devices) == 7
    assert all(isinstance(k, int) for k in devices)


def test_audio_device_index_resolves_stable_name(monkeypatch):
    monkeypatch.setattr(audio, "list_avfoundation_devices", lambda: SAMPLE_LISTING)
    assert audio.audio_device_index("Aggregate Device") == 6
    assert audio.audio_device_index("Missing Device") is None


def test_parse_audio_devices_on_empty_input():
    assert audio.parse_audio_devices("") == {}
    assert audio.parse_audio_devices("no devices here at all") == {}


def test_parse_audio_devices_with_no_audio_block():
    listing = "[AVFoundation indev @ 0x1] AVFoundation video devices:\n[x] [0] FaceTime HD Camera\n"
    assert audio.parse_audio_devices(listing) == {}
