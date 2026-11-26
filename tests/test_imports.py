"""Imported audio joins the same room pipeline as a recording made in Yarn."""

from __future__ import annotations

import struct
import wave

from meeting_archive import audio
from meeting_archive.config import Config
from meeting_archive.imports import import_audio
from meeting_archive.models import ROOM


def test_a_multichannel_wav_imports_as_one_room(tmp_path):
    src = tmp_path / "Large meeting test file.wav"
    rate = 16_000
    frames = rate // 2
    channels = 3
    with wave.open(str(src), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        # Speech on the last channel only. ffmpeg guesses this layout as
        # 2.1 and a plain mono downmix throws that channel away.
        samples = []
        for i in range(frames):
            sample = 8000 if (i // 40) % 2 == 0 else -8000
            samples.extend([0, 0, sample])
        handle.writeframes(struct.pack("<" + "h" * len(samples), *samples))

    cfg = Config(root=tmp_path / "archive")
    cfg.ensure_dirs()
    manifest = import_audio(cfg, src)

    assert manifest.source == "import-audio"
    assert manifest.title == "Large meeting test file"
    assert list(manifest.channels) == [ROOM]
    room = cfg.inbox_dir / manifest.channels[ROOM]
    info = audio.probe(room)
    assert info["channels"] == 1
    assert info["sample_rate"] == cfg.audio.sample_rate
    assert info["duration"] > 0.2
    assert audio.peak_dbfs(room) > -40
