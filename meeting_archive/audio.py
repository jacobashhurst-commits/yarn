"""ffmpeg wrappers for device discovery, probing and channel splitting.

Recording captures ONE file from a macOS aggregate device that combines the
microphone and BlackHole. That matters: two separate ffmpeg processes reading
two separate devices drift apart over a long meeting, and since the whole
transcript is merged on timestamps, drift would slowly scramble the ordering of
who said what. One device, one clock, sample-aligned channels.

The channels are split apart only after recording stops.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


class AudioError(RuntimeError):
    pass


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AudioError(
            f"{name} not found on PATH. Install with: brew install ffmpeg"
        )
    return path


def list_avfoundation_devices() -> str:
    """Return ffmpeg's device list. It writes this to stderr and exits non-zero."""
    require_tool("ffmpeg")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    return proc.stderr


_AUDIO_HEADER = re.compile(r"AVFoundation audio devices:")
_DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")


def parse_audio_devices(listing: str) -> dict[int, str]:
    """Extract {index: name} for audio devices from ffmpeg's device listing.

    ffmpeg lists video devices first, with its own independent numbering, so
    only lines after the audio header count. Getting this wrong would resolve
    an index to a camera.
    """
    devices: dict[int, str] = {}
    in_audio = False
    for line in listing.splitlines():
        if _AUDIO_HEADER.search(line):
            in_audio = True
            continue
        if not in_audio:
            continue
        # Strip ffmpeg's own "[AVFoundation indev @ 0x...] " prefix.
        body = line.split("] ", 1)[-1] if "] " in line else line
        match = _DEVICE_LINE.match(body.strip())
        if match:
            devices[int(match.group(1))] = match.group(2).strip()
        elif body.strip() and not body.strip().startswith("["):
            # A non-device line means the audio block has ended.
            break
    return devices


def audio_device_name(index: int) -> str | None:
    """Name currently at the given audio device index, or None."""
    try:
        return parse_audio_devices(list_avfoundation_devices()).get(index)
    except AudioError:
        return None


def audio_device_index(name: str) -> int | None:
    """Current index for an exact audio device name, or None."""
    try:
        devices = parse_audio_devices(list_avfoundation_devices())
    except AudioError:
        return None
    return next((index for index, current in devices.items() if current == name), None)


def probe(path: Path) -> dict:
    """Channel count, sample rate and duration of an audio file."""
    require_tool("ffprobe")
    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=channels,sample_rate:format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffprobe failed on {path}: {proc.stderr.strip()}")

    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or [{}]
    stream = streams[0]
    duration = data.get("format", {}).get("duration")
    return {
        "channels": int(stream.get("channels", 0)),
        "sample_rate": int(stream.get("sample_rate", 0)),
        "duration": float(duration) if duration else 0.0,
    }


_MAX_VOLUME = re.compile(r"max_volume:\s*(-?[\d.]+) dB")


def peak_dbfs(path: Path) -> float:
    """Peak level of a file in dBFS. Returns -inf for an all-zero signal.

    Used to tell a quiet recording apart from no recording at all. macOS hands
    an unauthorised process digital silence rather than an error, so a capture
    of literal zeros is the signature of a missing microphone permission, not
    of a quiet room -- even an empty room reads somewhere around -60 dB.
    """
    require_tool("ffmpeg")
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-i", str(path),
            "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    peaks = [float(m) for m in _MAX_VOLUME.findall(proc.stderr)]
    if not peaks:
        raise AudioError(f"could not measure level of {path}: {proc.stderr.strip()}")
    peak = max(peaks)
    # ffmpeg reports -91 dB for 16-bit digital silence, and never lower.
    return float("-inf") if peak <= -91.0 else peak


def is_silent(path: Path) -> bool:
    """True when a file contains no signal whatsoever."""
    try:
        return peak_dbfs(path) == float("-inf")
    except AudioError:
        return False


def pan_expression(channels: list[int]) -> str:
    """Build an ffmpeg pan filter that downmixes the given channels to mono.

    One channel is taken as-is; several are averaged, which is what we want for
    a stereo system-audio capture where the same speech sits in both sides.
    """
    if not channels:
        raise AudioError("no channels specified")
    if len(channels) == 1:
        return f"pan=mono|c0=c{channels[0]}"
    weight = 1.0 / len(channels)
    terms = "+".join(f"{weight:.6f}*c{c}" for c in channels)
    return f"pan=mono|c0={terms}"


def extract_mono(
    src: Path,
    dst: Path,
    channels: list[int],
    sample_rate: int = 16000,
) -> Path:
    """Extract and downmix the given channels of src into a mono wav at dst."""
    require_tool("ffmpeg")
    info = probe(src)
    available = info["channels"]
    bad = [c for c in channels if c >= available]
    if bad:
        raise AudioError(
            f"{src.name} has {available} channel(s), but the config asks for "
            f"channel index {bad[0]}. Run `ma devices` and check the channel "
            f"layout of your aggregate device, then fix mic_channels / "
            f"sys_channels in the config."
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    # Written to a temp name and renamed, so the watcher can never see a
    # half-extracted channel. The temp suffix hides the .wav extension, so the
    # output format has to be stated explicitly -- ffmpeg otherwise infers it
    # from the filename and fails.
    tmp = dst.with_suffix(dst.suffix + ".partial")
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-af", pan_expression(channels),
        "-ac", "1",
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg channel extraction failed: {proc.stderr.strip()}")
    tmp.replace(dst)
    return dst


def extract_clip(
    src: Path,
    dst: Path,
    start_s: float,
    duration_s: float = 6.0,
) -> Path:
    """Cut a short wav snippet. Offline ffmpeg only — not live capture."""
    require_tool("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".partial")
    start_s = max(0.0, float(start_s))
    duration_s = max(1.0, float(duration_s))
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start_s:.3f}",
            "-t", f"{duration_s:.3f}",
            "-i", str(src),
            "-c:a", "pcm_s16le",
            "-f", "wav",
            str(tmp),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg clip failed: {proc.stderr.strip()}")
    tmp.replace(dst)
    return dst
