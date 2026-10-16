"""Local transcription via faster-whisper.

Each channel is transcribed independently. The "everyone else" channel is
mostly silence whenever Jacob is the one talking, and long silence is exactly
where Whisper invents text ("Thanks for watching!" and similar). Two settings
hold that back:

  vad_filter                  drop non-speech before it reaches the decoder
  condition_on_previous_text  stop one bad segment seeding a repeat loop

faster-whisper's default VAD only strips silence longer than two seconds, which
is far too permissive for a half-empty channel, so the threshold is lowered in
config.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from .config import WhisperConfig
from .models import Segment

log = logging.getLogger(__name__)

# Everything downstream -- the channel wavs, the diarizer, Silero -- is 16 kHz.
SAMPLE_RATE = 16000

# whisper.cpp prints one line per segment as it goes. The end timestamp is the
# only progress signal it offers, and it is enough to drive the menu bar.
_CLI_TIMESTAMP = re.compile(
    r"\[\d\d:\d\d:\d\d\.\d+\s+-->\s+(\d\d):(\d\d):(\d\d)\.(\d+)\]"
)


class Transcriber:
    """Holds one loaded Whisper model and reuses it for every file.

    Loading the model costs seconds and a lot of memory, so the watcher creates
    one Transcriber and keeps it for the life of the process.
    """

    def __init__(self, config: WhisperConfig):
        self.config = config
        self._model = None

    @property
    def model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - environment issue
                raise RuntimeError(
                    "faster-whisper is not installed. Run: pip install -r requirements.txt"
                ) from exc

            log.info(
                "loading whisper model=%s device=%s compute_type=%s",
                self.config.model,
                self.config.device,
                self.config.compute_type,
            )
            self._model = WhisperModel(
                self.config.model,
                device=self.config.device,
                compute_type=self.config.compute_type,
                local_files_only=self.config.local_files_only,
            )
        return self._model

    def unload(self) -> None:
        """Drop the in-process faster-whisper weights. whisper.cpp is a subprocess."""
        self._model = None

    def transcribe_file(
        self,
        path: Path,
        channel: str,
        duration_s: float = 0.0,
        progress: Callable[[float], None] | None = None,
    ) -> list[Segment]:
        """Transcribe one mono wav into segments tagged with the channel."""
        if self.config.engine == "whisper_cpp":
            return self._transcribe_whisper_cpp(
                path, channel, duration_s=duration_s, progress=progress
            )
        if self.config.engine != "faster_whisper":
            raise RuntimeError(
                f"unknown whisper engine {self.config.engine!r}; expected "
                "'whisper_cpp' or 'faster_whisper'"
            )

        log.info("transcribing %s (channel=%s)", path.name, channel)

        vad_parameters = (
            {"min_silence_duration_ms": self.config.vad_min_silence_ms}
            if self.config.vad_filter
            else None
        )

        prompt = (self.config.initial_prompt or "").strip() or None
        segments_iter, info = self.model.transcribe(
            str(path),
            language=self.config.language,
            beam_size=self.config.beam_size,
            vad_filter=self.config.vad_filter,
            vad_parameters=vad_parameters,
            condition_on_previous_text=self.config.condition_on_previous_text,
            initial_prompt=prompt,
        )

        segments: list[Segment] = []
        for s in segments_iter:
            if progress is not None and duration_s > 0:
                progress(min(max(float(s.end) / duration_s, 0.0), 1.0))
            if not (s.text and s.text.strip()):
                continue
            segments.append(
                Segment(
                    channel=channel,
                    start=float(s.start),
                    end=float(s.end),
                    text=s.text.strip(),
                )
            )
        if progress is not None:
            progress(1.0)

        log.info(
            "channel=%s segments=%d detected_language=%s",
            channel,
            len(segments),
            getattr(info, "language", "?"),
        )
        return segments

    def _transcribe_whisper_cpp(
        self,
        path: Path,
        channel: str,
        duration_s: float = 0.0,
        progress: Callable[[float], None] | None = None,
    ) -> list[Segment]:
        """Run the same turbo model through Metal-enabled whisper.cpp.

        Silero VAD remains the faster-whisper implementation already installed
        locally. It removes sparse-channel silence before whisper.cpp sees the
        audio, then restores every result to the original meeting clock.
        """
        command = shutil.which(self.config.command) or self.config.command
        if not Path(command).exists():
            raise RuntimeError(
                f"whisper.cpp command not found at {self.config.command}; "
                "install it with `brew install whisper-cpp`"
            )
        model_path = Path(self.config.model_path).expanduser()
        if not model_path.is_file():
            raise RuntimeError(
                f"whisper.cpp model not found at {model_path}; "
                "run the local model setup before processing"
            )

        log.info(
            "transcribing %s (channel=%s engine=whisper.cpp model=%s)",
            path.name,
            channel,
            self.config.model,
        )

        with tempfile.TemporaryDirectory(prefix="yarn-whisper-") as tmp:
            tmp_dir = Path(tmp)
            input_path = Path(path)
            timestamp_map = None
            transcribed_duration = duration_s
            if self.config.vad_filter:
                input_path, timestamp_map, transcribed_duration = _vad_audio(
                    Path(path),
                    tmp_dir / "speech.wav",
                    min_silence_ms=self.config.vad_min_silence_ms,
                )
                if transcribed_duration <= 0:
                    if progress:
                        progress(1.0)
                    log.info(
                        "channel=%s segments=0 detected_language=%s",
                        channel,
                        self.config.language,
                    )
                    return []

            output_base = tmp_dir / "result"
            cmd = [
                str(command),
                "-m", str(model_path),
                "-f", str(input_path),
                "-l", self.config.language or "auto",
                "-t", str(self.config.num_threads),
                "-bs", str(self.config.beam_size),
                "-oj",
                "-of", str(output_base),
            ]
            prompt = (self.config.initial_prompt or "").strip()
            if prompt:
                cmd.extend(["--prompt", prompt])
            stderr_path = tmp_dir / "stderr.log"
            with stderr_path.open("w", encoding="utf-8") as stderr:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    text=True,
                    bufsize=1,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    if progress and transcribed_duration > 0:
                        end = _cli_line_end_seconds(line)
                        if end is not None:
                            progress(
                                min(max(end / transcribed_duration, 0.0), 1.0)
                            )
                returncode = proc.wait()

            if returncode != 0:
                error = stderr_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                detail = error.strip().splitlines()
                raise RuntimeError(
                    f"whisper.cpp failed on {Path(path).name}: "
                    f"{detail[-1] if detail else 'unknown error'}"
                )

            data = json.loads(
                output_base.with_suffix(".json").read_text(encoding="utf-8")
            )
            segments: list[Segment] = []
            for item in data.get("transcription", []):
                offsets = item.get("offsets") or {}
                start = float(offsets.get("from", 0)) / 1000.0
                end = float(offsets.get("to", 0)) / 1000.0
                if timestamp_map is not None:
                    start = timestamp_map.get_original_time(start)
                    end = timestamp_map.get_original_time(end, is_end=True)
                text = str(item.get("text") or "").strip()
                if text:
                    segments.append(Segment(channel, start, end, text))

        if progress:
            progress(1.0)
        log.info(
            "channel=%s segments=%d detected_language=%s",
            channel,
            len(segments),
            data.get("result", {}).get("language", "?"),
        )
        return segments


def _cli_line_end_seconds(line: str) -> float | None:
    """End time of a whisper.cpp progress line, or None if it isn't one."""
    match = _CLI_TIMESTAMP.search(line)
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(fraction) / (10 ** len(fraction))
    )


def _load_audio(src: Path) -> "np.ndarray":
    """16 kHz mono float32. Our channel wavs never need PyAV.

    faster-whisper's decode_audio goes through PyAV and passes
    metadata_errors=, which PyAV 19 dropped. A fresh Setup then failed every
    meeting in VAD before Whisper ran. Read our own 16 kHz wavs with `wave`.
    """
    if src.suffix.lower() == ".wav":
        try:
            return _read_wav(src)
        except Exception:
            pass
    from faster_whisper.audio import decode_audio

    return decode_audio(str(src), sampling_rate=SAMPLE_RATE)


def _read_wav(src: Path) -> "np.ndarray":
    with wave.open(str(src), "rb") as inp:
        channels = inp.getnchannels()
        width = inp.getsampwidth()
        rate = inp.getframerate()
        raw = inp.readframes(inp.getnframes())
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        raise ValueError(f"expected {SAMPLE_RATE} Hz, got {rate}")
    return samples


def _write_pcm16(path: Path, samples: "np.ndarray", sample_rate: int) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())


def _vad_audio(
    src: Path,
    dst: Path,
    min_silence_ms: int,
) -> tuple[Path, "object | None", float]:
    """Write a speech-only copy of src, plus the map back to real time.

    whisper.cpp has its own Silero option, but its model is only published on
    Hugging Face. faster-whisper already ships one inside the virtualenv, so
    the silence trimming keeps running locally on a model that is on disk.

    Returns (audio to transcribe, timestamp map, seconds of speech). A file
    with no speech at all returns 0.0 and the caller skips it -- the sys
    channel is silent whenever Jacob is the one talking, and handing that to
    Whisper is how "Thanks for watching!" ends up in a transcript.
    """
    from faster_whisper.vad import (
        SpeechTimestampsMap,
        VadOptions,
        get_speech_timestamps,
    )

    audio = _load_audio(src)
    chunks = get_speech_timestamps(
        audio, VadOptions(min_silence_duration_ms=min_silence_ms)
    )
    if not chunks:
        return dst, None, 0.0

    speech = np.concatenate([audio[c["start"]:c["end"]] for c in chunks])
    _write_pcm16(dst, speech, SAMPLE_RATE)
    removed = (len(audio) - len(speech)) / SAMPLE_RATE
    if removed > 0:
        log.info("VAD filter removed %.2fs of audio from %s", removed, src.name)
    return dst, SpeechTimestampsMap(chunks, SAMPLE_RATE), len(speech) / SAMPLE_RATE
