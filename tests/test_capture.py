"""Tests for the CoreAudio capture that replaced ffmpeg.

The bug these guard against was invisible: ffmpeg's avfoundation input wrote
roughly four samples in every five, at the correct pitch and with no warning,
so a meeting came back a fifth shorter with a fifth of its words missing. The
recorder now counts frames itself, and the wav header is kept current so a
recording survives being killed.
"""

from __future__ import annotations

import struct
import threading
import wave
from pathlib import Path
from queue import Queue

import pytest

from meeting_archive import pipeline
from meeting_archive.capture import (
    SAMPLE_WIDTH,
    CaptureError,
    Request,
    WavWriter,
    _collect_preflight,
    _wait_for_signal,
    resolve_device,
)
from meeting_archive.config import Config


# ---------------------------------------------------------------------------
# the request file
# ---------------------------------------------------------------------------

def write_request(path: Path, raw="/tmp/x__raw.wav", device="Aggregate Device",
                  rate=16000, channels=3, logs="/tmp/logs") -> Path:
    path.write_text(f"{raw}\n{device}\n{rate}\n{channels}\n{logs}\n", encoding="utf-8")
    return path


def test_request_round_trips_every_field(tmp_path):
    request = Request.read(write_request(tmp_path / "req"))
    assert request.raw == Path("/tmp/x__raw.wav")
    assert request.device == "Aggregate Device"
    assert request.sample_rate == 16000
    assert request.required_channels == 3
    assert request.logs == Path("/tmp/logs")


def test_request_rejects_a_short_file(tmp_path):
    path = tmp_path / "req"
    path.write_text("only\ntwo\n", encoding="utf-8")
    with pytest.raises(CaptureError, match="malformed"):
        Request.read(path)


def test_request_rejects_a_non_numeric_channel_count(tmp_path):
    path = tmp_path / "req"
    path.write_text("/tmp/a.wav\nDevice\n16000\nthree\n/tmp/logs\n", encoding="utf-8")
    with pytest.raises(CaptureError, match="malformed"):
        Request.read(path)


# ---------------------------------------------------------------------------
# the wav writer
# ---------------------------------------------------------------------------

def test_writer_produces_a_file_the_wave_module_agrees_with(tmp_path):
    writer = WavWriter(tmp_path / "out.wav", channels=3, sample_rate=48000)
    frames = 48000  # one second
    writer.write(b"\x01\x00" * 3 * frames)
    writer.close()

    assert writer.frames == frames
    assert writer.seconds == pytest.approx(1.0)
    with wave.open(str(tmp_path / "out.wav")) as handle:
        assert handle.getnchannels() == 3
        assert handle.getframerate() == 48000
        assert handle.getsampwidth() == SAMPLE_WIDTH
        assert handle.getnframes() == frames


def test_writer_keeps_the_header_current_while_recording(tmp_path):
    """A killed capture must still leave a readable file.

    `wave` only writes its sizes on close, so a SIGKILL used to leave a file
    every tool refused to open. The header is refreshed as the recording runs,
    so reading it mid-capture has to work.
    """
    path = tmp_path / "out.wav"
    writer = WavWriter(path, channels=1, sample_rate=16000)
    # Three seconds, which is past the refresh interval.
    writer.write(b"\x02\x00" * 16000 * 3)

    data_size = struct.unpack("<I", path.read_bytes()[40:44])[0]
    assert data_size > 0, "header still claims an empty file mid-recording"
    with wave.open(str(path)) as handle:
        assert handle.getnframes() >= 16000 * 2
    writer.close()


def test_writer_header_is_exact_after_close(tmp_path):
    path = tmp_path / "out.wav"
    writer = WavWriter(path, channels=2, sample_rate=8000)
    payload = b"\x03\x00" * 2 * 1234
    writer.write(payload)
    writer.close()

    raw = path.read_bytes()
    assert struct.unpack("<I", raw[4:8])[0] == 36 + len(payload)
    assert struct.unpack("<I", raw[40:44])[0] == len(payload)
    assert len(raw) == 44 + len(payload)


def test_writer_close_is_idempotent(tmp_path):
    writer = WavWriter(tmp_path / "out.wav", channels=1, sample_rate=16000)
    writer.write(b"\x00\x00" * 100)
    writer.close()
    writer.close()


# ---------------------------------------------------------------------------
# device resolution
# ---------------------------------------------------------------------------

class FakeSoundDevice:
    """Stands in for the sounddevice module's device listing."""

    def __init__(self, devices):
        self._devices = devices

    def query_devices(self):
        return self._devices


def install_fake_devices(monkeypatch, devices):
    import sys
    monkeypatch.setitem(sys.modules, "sounddevice", FakeSoundDevice(devices))


AGGREGATE = {"name": "Aggregate Device", "max_input_channels": 3,
             "default_samplerate": 48000.0}
MIC = {"name": "MacBook Pro Microphone", "max_input_channels": 1,
       "default_samplerate": 48000.0}
SPEAKERS = {"name": "MacBook Pro Speakers", "max_input_channels": 0,
            "default_samplerate": 48000.0}


def test_resolve_device_finds_an_exact_name(monkeypatch):
    install_fake_devices(monkeypatch, [SPEAKERS, MIC, AGGREGATE])
    assert resolve_device("Aggregate Device", 3)["max_input_channels"] == 3


def test_resolve_device_refuses_too_few_channels(monkeypatch):
    """A channel layout wider than the device is a broken aggregate, not a headset."""
    install_fake_devices(monkeypatch, [MIC, AGGREGATE])
    with pytest.raises(CaptureError, match="needs 3") as caught:
        resolve_device("MacBook Pro Microphone", 3)
    assert "Start Room" in str(caught.value)


def test_resolve_device_lists_what_is_available_when_the_name_is_wrong(monkeypatch):
    install_fake_devices(monkeypatch, [MIC, AGGREGATE])
    with pytest.raises(CaptureError) as caught:
        resolve_device("Loopback 4ch", 2)
    message = str(caught.value)
    assert "MacBook Pro Microphone" in message
    assert "Aggregate Device" in message
    # An output-only device is not a candidate and must not be suggested.
    assert "Speakers" not in message


def test_resolve_device_ignores_output_only_devices(monkeypatch):
    install_fake_devices(monkeypatch, [SPEAKERS])
    with pytest.raises(CaptureError, match="no audio input device"):
        resolve_device("MacBook Pro Speakers", 1)


def test_resolve_device_falls_back_to_a_prefix(monkeypatch):
    """PortAudio and ffmpeg sometimes disagree on trailing detail in a name."""
    install_fake_devices(monkeypatch, [{"name": "Aggregate Device (2)",
                                        "max_input_channels": 3,
                                        "default_samplerate": 48000.0}])
    assert resolve_device("Aggregate Device", 3)["name"] == "Aggregate Device (2)"


# ---------------------------------------------------------------------------
# the silence preflight
# ---------------------------------------------------------------------------

def test_preflight_returns_none_when_the_device_sends_nothing(monkeypatch):
    """No audio at all is a broken device, and must not be called silence."""
    monkeypatch.setattr("meeting_archive.capture.PREFLIGHT_TIMEOUT", 0.2)
    assert _collect_preflight(Queue(), 16000, 1, threading.Event()) is None


def test_preflight_collects_enough_to_judge():
    blocks: Queue[bytes] = Queue()
    # Well over the half second the check wants.
    blocks.put(b"\x05\x00" * 16000)
    collected = _collect_preflight(blocks, 16000, 1, threading.Event())
    assert collected is not None
    assert any(collected), "a tone must not read as silence"


def test_preflight_recognises_digital_silence():
    """All-zero samples mean macOS withheld the microphone, not a quiet room."""
    blocks: Queue[bytes] = Queue()
    blocks.put(bytes(2 * 16000))
    collected = _collect_preflight(blocks, 16000, 1, threading.Event())
    assert collected is not None
    assert not any(collected)


def test_a_headset_that_wakes_after_the_opening_silence_is_kept():
    """AirPods are digital silence for the first moment, then a real signal."""
    blocks: Queue[bytes] = Queue()
    blocks.put(b"\x01\x00\x00\x00")
    collected = _wait_for_signal(blocks, bytearray(32000), threading.Event())
    assert any(collected)


def test_silence_that_never_ends_is_still_refused(monkeypatch):
    """A missing microphone permission does not start producing samples later."""
    monkeypatch.setattr("meeting_archive.capture.SIGNAL_WAIT_SECONDS", 0.2)
    collected = _wait_for_signal(Queue(), bytearray(32000), threading.Event())
    assert not any(collected)


def test_preflight_gives_up_when_asked_to_stop():
    stop = threading.Event()
    stop.set()
    assert _collect_preflight(Queue(), 16000, 1, stop) is None


# ---------------------------------------------------------------------------
# stranded captures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(root=tmp_path / "archive")
    config.ensure_dirs()
    return config


def test_no_stranded_captures_in_a_clean_archive(cfg):
    assert pipeline.stranded_captures(cfg) == []


def test_a_refused_capture_is_reported_as_stranded(cfg):
    """finalize leaves what it refuses in place, so something must point at it."""
    raw = cfg.in_progress_dir / "2026-09-11T17-22-07__raw.wav"
    raw.write_bytes(b"RIFF")
    assert pipeline.stranded_captures(cfg) == [raw]


def test_the_live_recording_is_not_stranded(cfg):
    """The file being recorded into lives here too, and is not a problem."""
    raw = cfg.in_progress_dir / "2026-09-11T18-03-58__raw.wav"
    raw.write_bytes(b"RIFF")
    (cfg.root / ".recording").write_text(
        f"1234\n2026-09-11T18-03-58\n{raw}\n2026-09-11T18:03:58+1000\ncall\n99\n",
        encoding="utf-8",
    )
    assert pipeline.stranded_captures(cfg) == []


def test_an_older_capture_is_stranded_even_while_recording(cfg):
    """One live recording must not hide a previous meeting that was lost."""
    live = cfg.in_progress_dir / "2026-09-11T18-03-58__raw.wav"
    lost = cfg.in_progress_dir / "2026-09-11T17-22-07__raw.wav"
    live.write_bytes(b"RIFF")
    lost.write_bytes(b"RIFF")
    (cfg.root / ".recording").write_text(
        f"1234\n2026-09-11T18-03-58\n{live}\n2026-09-11T18:03:58+1000\ncall\n99\n",
        encoding="utf-8",
    )
    assert pipeline.stranded_captures(cfg) == [lost]


def test_a_truncated_state_file_does_not_hide_stranded_captures(cfg):
    raw = cfg.in_progress_dir / "2026-09-11T17-22-07__raw.wav"
    raw.write_bytes(b"RIFF")
    (cfg.root / ".recording").write_text("1234\n", encoding="utf-8")
    assert pipeline.stranded_captures(cfg) == [raw]


def test_split_channel_files_are_not_mistaken_for_captures(cfg):
    """Only the raw multi-channel file represents an unpublished meeting."""
    (cfg.in_progress_dir / "2026-09-11T17-22-07__mic.wav").write_bytes(b"RIFF")
    assert pipeline.stranded_captures(cfg) == []


def test_a_short_mute_inside_speech_is_filled_and_a_pause_is_not(tmp_path):
    """Identify playback was cutting in and out on holes a few tens of ms long."""
    import array
    import math

    from meeting_archive.audio import conceal_dropouts

    rate = 16000
    seconds = 1.0
    samples = array.array(
        "h",
        (
            int(8000 * math.sin(2 * math.pi * 180 * i / rate))
            for i in range(int(rate * seconds))
        ),
    )
    # 15ms of digital silence in the middle of the tone.
    hole = int(0.40 * rate)
    width = int(0.015 * rate)
    for index in range(hole, hole + width):
        samples[index] = 0
    # A 200ms pause must stay quiet.
    pause = int(0.70 * rate)
    pause_width = int(0.20 * rate)
    for index in range(pause, pause + pause_width):
        samples[index] = 0
    path = tmp_path / "clip.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    assert conceal_dropouts(path) == 1
    with wave.open(str(path), "rb") as handle:
        restored = array.array("h")
        restored.frombytes(handle.readframes(handle.getnframes()))
    assert len(restored) == len(samples)
    middle = restored[hole + width // 2]
    assert abs(middle) > 1000
    assert max(abs(restored[i]) for i in range(pause, pause + pause_width)) == 0
