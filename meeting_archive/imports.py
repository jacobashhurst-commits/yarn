"""Import existing recordings and transcripts into the same archive pipeline."""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import audio, jobs, pipeline, records
from .config import Config
from .models import ROOM, Segment, Turn
from .records import Manifest, Transcript, new_meeting_id

AUDIO_SUFFIXES = {".wav", ".m4a", ".mp3", ".aac", ".mp4", ".mov", ".flac", ".ogg"}
TRANSCRIPT_SUFFIXES = {".txt", ".md", ".srt", ".vtt"}

_TS_LINE = re.compile(
    r"^\[(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?\]\s*"
    r"(?:(?P<who>[^:]{1,40}):\s*)?(?P<text>.+)$"
)
_SPEAKER_LINE = re.compile(r"^(?P<who>[A-Za-z][A-Za-z0-9 .'-]{0,39}):\s+(?P<text>.+)$")
_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


class ImportFailed(RuntimeError):
    """The file could not be brought into the archive."""


def import_audio(cfg: Config, src: Path, *, title: str = "") -> Manifest:
    """Convert a file with ffmpeg and enqueue it as a room-mode meeting.

    Imports are one mixed channel: we do not know which device produced them,
    so they must not be labelled as Jacob's mic.
    """
    src = Path(src).expanduser()
    if not src.is_file():
        raise ImportFailed(f"no such file: {src}")
    if src.suffix.lower() not in AUDIO_SUFFIXES:
        raise ImportFailed(
            f"unsupported audio type {src.suffix}; expected "
            + ", ".join(sorted(AUDIO_SUFFIXES))
        )
    cfg.ensure_dirs()
    meeting_id = new_meeting_id()
    wav = cfg.in_progress_dir / f"{meeting_id}__import.wav"
    _ffmpeg_to_wav(src, wav)
    info = audio.probe(wav)
    if audio.is_silent(wav):
        wav.unlink(missing_ok=True)
        raise ImportFailed(f"{src.name} contains no audible audio")

    room_path = cfg.inbox_dir / f"{meeting_id}__room.wav"
    # Already mono 16 kHz from the conversion; copy so the inbox contract holds.
    if info["channels"] == 1 and info["sample_rate"] == cfg.audio.sample_rate:
        shutil.move(str(wav), str(room_path))
    else:
        audio.extract_mono(wav, room_path, [0], cfg.audio.sample_rate)
        wav.unlink(missing_ok=True)

    manifest = Manifest(
        meeting_id=meeting_id,
        channels={ROOM: room_path.name},
        started_at=datetime.now().isoformat(timespec="seconds"),
        ended_at=datetime.now().isoformat(timespec="seconds"),
        duration_s=info["duration"],
        raw_file="",
        source="import-audio",
        title=title or src.stem,
    )
    manifest.write(cfg.inbox_dir / f"{meeting_id}.json")
    jobs.enqueue(
        cfg,
        meeting_id,
        source="import-audio",
        duration_s=info["duration"],
        title=manifest.title,
    )
    return manifest


def import_transcript(
    cfg: Config,
    src: Path,
    *,
    title: str = "",
    store=None,
) -> Transcript:
    """Parse a coworker transcript and index it without running Whisper."""
    src = Path(src).expanduser()
    if not src.is_file():
        raise ImportFailed(f"no such file: {src}")
    suffix = src.suffix.lower()
    if suffix not in TRANSCRIPT_SUFFIXES:
        raise ImportFailed(
            f"unsupported transcript type {src.suffix}; expected "
            + ", ".join(sorted(TRANSCRIPT_SUFFIXES))
        )
    text = src.read_text(encoding="utf-8")
    if suffix == ".srt":
        turns = _parse_srt(text)
    elif suffix == ".vtt":
        turns = _parse_vtt(text)
    else:
        turns = _parse_text(text)
    if not turns:
        raise ImportFailed(f"{src.name} contained no usable speech lines")

    cfg.ensure_dirs()
    meeting_id = new_meeting_id()
    duration = max((turn.end for turn in turns), default=0.0)
    transcript = Transcript(
        meeting_id=meeting_id,
        segments=[
            Segment(turn.channel, turn.start, turn.end, turn.text, speaker=turn.speaker)
            for turn in turns
        ],
        turns=turns,
        duration_s=duration,
        started_at=datetime.now().isoformat(timespec="seconds"),
        ended_at=datetime.now().isoformat(timespec="seconds"),
        model="imported",
        created_at=datetime.now().isoformat(timespec="seconds"),
        title=title or src.stem,
        source="import-transcript",
        whisper_engine="none",
    )
    transcript.md_name = records.markdown_filename(meeting_id, transcript.title)
    transcript.write(cfg.transcript_json(meeting_id))
    job = jobs.enqueue(
        cfg,
        meeting_id,
        source="import-transcript",
        duration_s=duration,
        title=transcript.title,
    )
    jobs.finish_stage(cfg, job, "transcribe", status=jobs.SKIPPED, detail="imported")
    jobs.finish_stage(cfg, job, "diarize", status=jobs.SKIPPED, detail="imported labels")
    if store is not None:
        pipeline.ingest(cfg, meeting_id, store)
        jobs.finish_stage(cfg, job, "index", status=jobs.DONE)
        jobs.complete(cfg, job, title=transcript.title)
    return transcript


def _ffmpeg_to_wav(src: Path, dst: Path) -> Path:
    audio.require_tool("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".partial")
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            "-f", "wav",
            str(tmp),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ImportFailed(f"ffmpeg could not read {src.name}: {proc.stderr.strip()}")
    tmp.replace(dst)
    return dst


def _hms(h: int, m: int, s: int, ms: int = 0) -> float:
    return h * 3600 + m * 60 + s + ms / 1000.0


def _parse_text(text: str) -> list[Turn]:
    turns: list[Turn] = []
    cursor = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        stamp = _TS_LINE.match(line)
        if stamp:
            h = int(stamp.group("h"))
            m = int(stamp.group("m"))
            s = int(stamp.group("s") or 0)
            start = _hms(h, m, s) if stamp.group("s") is not None or h > 3 else _hms(0, h, m)
            who = (stamp.group("who") or "").strip()
            body = stamp.group("text").strip()
            turns.append(_named_turn(who, start, start + 2.0, body))
            cursor = start + 2.0
            continue
        named = _SPEAKER_LINE.match(line)
        if named:
            who = named.group("who").strip()
            body = named.group("text").strip()
            turns.append(_named_turn(who, cursor, cursor + 2.0, body))
            cursor += 2.0
            continue
        turns.append(Turn(ROOM, cursor, cursor + 2.0, line))
        cursor += 2.0
    return turns


def _named_turn(who: str, start: float, end: float, text: str) -> Turn:
    speaker = None
    if who.lower().startswith("speaker"):
        suffix = who.split()[-1]
        if suffix.isdigit():
            speaker = f"room-{suffix}"
    elif who:
        speaker = who
    return Turn(ROOM, start, end, text, speaker=speaker)


def _parse_srt(text: str) -> list[Turn]:
    turns: list[Turn] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        match = _SRT_TIME.search(lines[0] if "-->" in lines[0] else lines[1])
        if not match:
            continue
        start = _hms(int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4)))
        end = _hms(int(match.group(5)), int(match.group(6)), int(match.group(7)), int(match.group(8)))
        body = " ".join(lines[2:] if "-->" in lines[1] else lines[1:])
        who, _, rest = body.partition(":")
        if rest and len(who) <= 40:
            turns.append(_named_turn(who.strip(), start, end, rest.strip()))
        else:
            turns.append(Turn(ROOM, start, end, body))
    return turns


def _parse_vtt(text: str) -> list[Turn]:
    cleaned = re.sub(r"^WEBVTT.*\n", "", text, count=1, flags=re.IGNORECASE)
    # VTT uses the same cue shape as SRT with '.' milliseconds.
    return _parse_srt(cleaned)
