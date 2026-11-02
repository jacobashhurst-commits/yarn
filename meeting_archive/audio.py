"""ffmpeg wrappers for device discovery, probing and channel splitting.

A room recording is one microphone file. A video call is two files that start
together: the headset microphone, and the audio being sent to the headphones.
Those are different devices. BlackHole never hears a call that is only playing
in the headphones, and the laptop microphone saved at setup is not the
microphone in the headset.

The channels are split apart only after recording stops.
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
import wave
from dataclasses import dataclass
from pathlib import Path


class AudioError(RuntimeError):
    pass


# A menu-bar click inherits the app's PATH, which is only the system
# directories. ffmpeg lives in Homebrew, or in Yarn's folder when Setup
# downloaded it. A stop that cannot find ffprobe leaves the recording unsaved.
def tool_directories() -> tuple[Path, ...]:
    return (
        Path.home() / "Library" / "Application Support" / "Yarn" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    )


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        for directory in tool_directories():
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                path = str(candidate)
                os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
                break
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


# A video call has to follow the devices in use right now. The name saved at
# setup stays the laptop microphone, which is the wrong place to listen once
# the headphones are on.
_HEADSET_WORDS = (
    "airpod",
    "headphone",
    "headset",
    "earpod",
    "earphone",
    "buds",
    "beats",
)


def shell_quote(value: str) -> str:
    """Single-quote a value for `eval` in bin/rec. Names contain apostrophes."""
    return "'" + value.replace("'", "'\\''") + "'"


@dataclass(frozen=True)
class CoreDevice:
    name: str
    transport: str
    input_channels: int = 0
    output_channels: int = 0
    default_input: bool = False
    default_output: bool = False
    output_source: str = ""


@dataclass(frozen=True)
class CallRoute:
    """Where a video call is recorded from, by device name.

    The far side is everything the Mac is playing, not one output device.
    `microphone` is the laptop microphone when the headphones are Bluetooth.
    Opening the AirPods microphone drops them from music quality to phone
    quality, and the audio that was already playing then never reaches the
    recording.
    """

    microphone: str
    playback: tuple[str, ...] = ()


def _transport_kind(raw: str) -> str:
    text = (raw or "").lower()
    for kind in (
        "bluetoothle",
        "bluetooth",
        "builtin",
        "usb",
        "virtual",
        "aggregate",
        "hdmi",
        "displayport",
        "thunderbolt",
        "unknown",
    ):
        if kind in text:
            return kind
    return "other"


def _is_headset(device: CoreDevice) -> bool:
    if device.transport in {"virtual", "aggregate", "unknown"}:
        return False
    blob = f"{device.name} {device.output_source}".lower()
    if any(word in blob for word in _HEADSET_WORDS):
        return True
    # AirPods and most Bluetooth headsets do not have "headphone" in the name.
    # A USB dock does, in the transport only, so USB alone is not enough.
    return device.transport in {"bluetooth", "bluetoothle"}


def parse_coreaudio_devices(payload: str) -> list[CoreDevice]:
    """Turn `system_profiler SPAudioDataType -json` into devices."""
    data = json.loads(payload or "{}")
    items: list[dict] = []
    for group in data.get("SPAudioDataType") or []:
        items.extend(group.get("_items") or [])
    devices: list[CoreDevice] = []
    for item in items:
        name = str(item.get("_name") or "").strip()
        if not name:
            continue
        devices.append(
            CoreDevice(
                name=name,
                transport=_transport_kind(str(item.get("coreaudio_device_transport") or "")),
                input_channels=int(item.get("coreaudio_device_input") or 0),
                output_channels=int(item.get("coreaudio_device_output") or 0),
                default_input=item.get("coreaudio_default_audio_input_device") == "spaudio_yes",
                default_output=item.get("coreaudio_default_audio_output_device") == "spaudio_yes",
                output_source=str(item.get("coreaudio_output_source") or ""),
            )
        )
    return devices


def _is_bluetooth(device: CoreDevice) -> bool:
    return device.transport in {"bluetooth", "bluetoothle"}


def _laptop_microphone(inputs: list[CoreDevice], configured_mic: str) -> str:
    """A microphone that does not pull Bluetooth headphones down to phone quality."""
    builtin = [device for device in inputs if device.transport == "builtin"]
    if builtin:
        preferred = next((device for device in builtin if device.default_input), builtin[0])
        return preferred.name
    for device in inputs:
        if device.name == configured_mic and not _is_bluetooth(device):
            return device.name
    for device in inputs:
        if not _is_bluetooth(device):
            return device.name
    if inputs:
        return inputs[0].name
    return configured_mic


def _headset_microphone(playback: CoreDevice, inputs: list[CoreDevice]) -> str:
    if playback.input_channels > 0:
        return playback.name
    headset_inputs = [device for device in inputs if _is_headset(device)]
    play = playback.name.casefold()
    for device in headset_inputs:
        name = device.name.casefold()
        if play in name or name in play:
            return device.name
    if len(headset_inputs) == 1:
        return headset_inputs[0].name
    return ""


def choose_call_route(devices: list[CoreDevice], configured_mic: str) -> CallRoute:
    """Pick the headset microphone and the headphone outputs for a video call."""
    outputs = [device for device in devices if device.output_channels > 0]
    inputs = [device for device in devices if device.input_channels > 0]
    headsets = [device for device in outputs if _is_headset(device)]
    if headsets:
        seen: list[str] = []
        for device in headsets:
            if device.name not in seen:
                seen.append(device.name)
        playback = tuple(seen)
        chosen = next((device for device in headsets if device.default_output), headsets[0])
    else:
        default_out = next((device for device in outputs if device.default_output), None)
        playback = (default_out.name,) if default_out is not None else ()
        chosen = default_out

    # Bluetooth first. Its microphone and its high-quality output cannot be
    # used together: opening the microphone retunes the headphones to 24 kHz
    # and a recording of what was playing comes back silent.
    microphone = ""
    wired = [device for device in headsets if not _is_bluetooth(device)]
    if wired:
        microphone = _headset_microphone(wired[0], inputs)
    if not microphone and any(_is_bluetooth(device) for device in headsets):
        microphone = _laptop_microphone(inputs, configured_mic)
    if not microphone and chosen is not None and _is_headset(chosen):
        microphone = _headset_microphone(chosen, inputs)
    if not microphone:
        default_in = next((device for device in inputs if device.default_input), None)
        if default_in is not None and not (
            any(_is_bluetooth(device) for device in headsets) and _is_bluetooth(default_in)
        ):
            microphone = default_in.name
    if not microphone:
        microphone = _laptop_microphone(inputs, configured_mic) if inputs else configured_mic
    return CallRoute(microphone=microphone, playback=playback)


def call_route(configured_mic: str) -> CallRoute:
    """Devices for a video call on this Mac. A profiler failure keeps the mic."""
    try:
        proc = subprocess.run(
            ["system_profiler", "SPAudioDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return CallRoute(microphone=configured_mic)
    if proc.returncode != 0:
        return CallRoute(microphone=configured_mic)
    try:
        devices = parse_coreaudio_devices(proc.stdout)
    except (ValueError, TypeError):
        return CallRoute(microphone=configured_mic)
    return choose_call_route(devices, configured_mic)


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


def write_silence(dst: Path, duration_s: float, sample_rate: int) -> Path:
    """Write a mono wav of digital silence.

    A video call whose far side never arrived still needs a sys channel, so
    the manifest shape does not change. Copying the microphone into that
    channel would label the wearer as everyone else.
    """
    require_tool("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".partial")
    duration = max(float(duration_s), 0.05)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r={int(sample_rate)}:cl=mono",
        "-t", f"{duration:.3f}",
        "-c:a", "pcm_s16le",
        "-f", "wav",
        str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"could not write silence: {proc.stderr.strip()}")
    tmp.replace(dst)
    return dst


# Whisper's segment starts at the last pause and ends at the next one, so a
# short sentence in the middle of a recording is credited with the whole quiet
# stretch around it. Speech here is whatever sits above the quiet parts of
# that same stretch. A fixed loudness gate does not work: this microphone
# idles around -40 dB, and a quiet room idles around -60 dB.
_FRAME_S = 0.02
_SPEECH_MARGIN_DB = 6.0
_SPEECH_FLOOR_DB = -50.0
_SPEECH_LOUD_DB = -26.0
_SPEECH_BRIDGE_S = 0.30
_SPEECH_MIN_S = 0.08
_LEVEL_CACHE: dict[tuple[str, int, int], list[float]] = {}


def _frame_levels(path: Path) -> list[float] | None:
    """RMS of each 20ms frame, in dBFS. None when the file is not 16-bit PCM."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _LEVEL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with wave.open(str(path), "rb") as inp:
            if inp.getsampwidth() != 2 or inp.getnchannels() < 1:
                return None
            rate = inp.getframerate() or 16000
            channels = inp.getnchannels()
            frame_n = max(1, int(rate * _FRAME_S))
            levels: list[float] = []
            while True:
                raw = inp.readframes(frame_n)
                count = len(raw) // (2 * channels)
                if count <= 0:
                    break
                samples = array.array("h")
                samples.frombytes(raw[: count * channels * 2])
                if sys.byteorder != "little":
                    samples.byteswap()
                if channels == 1:
                    energy = sum(sample * sample for sample in samples)
                else:
                    energy = 0.0
                    step = channels
                    for index in range(0, count * channels, step):
                        mixed = sum(samples[index : index + step]) / channels
                        energy += mixed * mixed
                rms = math.sqrt(energy / count) if energy > 0 else 0.0
                levels.append(20.0 * math.log10(rms / 32768.0) if rms else -120.0)
                if count < frame_n:
                    break
    except (wave.Error, OSError, EOFError):
        return None
    if not levels:
        return None
    if len(_LEVEL_CACHE) > 8:
        _LEVEL_CACHE.clear()
    _LEVEL_CACHE[key] = levels
    return levels


def _spoken_frames(levels: list[float]) -> float:
    """Seconds of frames that are speech, closing gaps shorter than a breath."""
    if not levels:
        return 0.0
    span = len(levels) * _FRAME_S
    quiet = sorted(levels)[len(levels) // 5]
    # The stretch is already speech from start to finish. Measuring it would
    # only trim the breaths inside a sentence the turn already covers.
    if quiet > _SPEECH_LOUD_DB:
        return span
    threshold = max(quiet + _SPEECH_MARGIN_DB, _SPEECH_FLOOR_DB)
    flags = [level >= threshold for level in levels]
    bridge = int(round(_SPEECH_BRIDGE_S / _FRAME_S))
    index = 0
    while index < len(flags):
        if flags[index]:
            index += 1
            continue
        end = index
        while end < len(flags) and not flags[end]:
            end += 1
        if index > 0 and flags[index - 1] and end < len(flags) and (end - index) <= bridge:
            for gap in range(index, end):
                flags[gap] = True
        index = end
    minimum = int(round(_SPEECH_MIN_S / _FRAME_S))
    index = 0
    while index < len(flags):
        if not flags[index]:
            index += 1
            continue
        end = index
        while end < len(flags) and flags[end]:
            end += 1
        if end - index < minimum:
            for blip in range(index, end):
                flags[blip] = False
        index = end
    return sum(flags) * _FRAME_S


def spoken_seconds(path: Path, ranges: list[tuple[float, float]]) -> float | None:
    """How long `ranges` actually contain speech, or None if the file can't be read.

    Each range is one transcript turn. The quiet padding Whisper leaves around
    the words does not count. A missing or unreadable file returns None so the
    caller can keep the turn span.
    """
    levels = _frame_levels(path)
    if levels is None:
        return None
    total = 0.0
    for start_s, end_s in ranges:
        span = max(0.0, float(end_s) - float(start_s))
        if span <= 0:
            continue
        first = max(0, int(float(start_s) / _FRAME_S))
        last = min(len(levels), int(math.ceil(float(end_s) / _FRAME_S)))
        if last <= first:
            continue
        total += min(span, _spoken_frames(levels[first:last]))
    return total


def slice_wav(src: Path, dst: Path, start_s: float, end_s: float) -> Path:
    """Copy a time range out of a PCM wav. Used to resume a long transcription.

    The slice is its own file, so Whisper's timestamps start at zero and the
    caller adds ``start_s`` back. A closed laptop can then continue from the
    last finished slice instead of transcribing the meeting again.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(src), "rb") as inp:
        rate = inp.getframerate() or 1
        start = max(0, int(float(start_s) * rate))
        end = min(inp.getnframes(), int(float(end_s) * rate))
        if end <= start:
            end = min(inp.getnframes(), start + 1)
        inp.setpos(start)
        frames = inp.readframes(end - start)
        channels = inp.getnchannels()
        width = inp.getsampwidth()
        rate = inp.getframerate()
    tmp = dst.with_suffix(dst.suffix + ".partial")
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(frames)
    tmp.replace(dst)
    return dst


def extract_clip(
    src: Path,
    dst: Path,
    start_s: float,
    duration_s: float = 6.0,
) -> Path:
    """Cut a short wav snippet. Offline ffmpeg only - not live capture."""
    require_tool("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".partial")
    start_s = max(0.0, float(start_s))
    # Identity clips can be short on purpose. Forcing a 1s floor here used to
    # extend a clean solo stretch into the next speaker.
    duration_s = max(0.4, float(duration_s))
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
    # A video-call recording can drop a few tens of milliseconds in the middle
    # of a word. The identify window plays these clips, so a hole here is the
    # voice cutting in and out. Fill only those holes; leave real pauses.
    conceal_dropouts(dst)
    return dst


def conceal_dropouts(path: Path, max_hole_s: float = 0.04) -> int:
    """Replace short mutes that sit inside speech. The length stays the same.

    A 10–30ms dip to the noise floor, with speech on both sides, is a dropped
    capture packet. A breath is longer than ``max_hole_s`` and is left alone.
    Returns how many holes were filled.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                return 0
            rate = handle.getframerate() or 1
            samples = array.array("h")
            samples.frombytes(handle.readframes(handle.getnframes()))
    except (OSError, wave.Error):
        return 0
    if len(samples) < rate // 10:
        return 0
    filled = _conceal_dropouts(samples, rate, max_hole_s=max_hole_s)
    if not filled:
        return 0
    tmp = path.with_suffix(path.suffix + ".partial")
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())
    tmp.replace(path)
    return filled


def _conceal_dropouts(
    samples: array.array, rate: int, max_hole_s: float = 0.04
) -> int:
    frame = max(1, int(rate * 0.005))
    if len(samples) < frame * 6:
        return 0
    levels: list[float] = []
    for start in range(0, len(samples) - frame + 1, frame):
        energy = 0.0
        for value in samples[start : start + frame]:
            energy += float(value) * float(value)
        rms = math.sqrt(energy / frame) / 32768.0
        levels.append(20.0 * math.log10(rms + 1e-12))
    quiet = -42.0
    loud = -28.0
    max_frames = max(1, int(max_hole_s / 0.005))
    filled = 0
    index = 1
    while index < len(levels) - 1:
        if levels[index] >= quiet or levels[index - 1] < loud:
            index += 1
            continue
        end = index
        while end < len(levels) - 1 and levels[end] < quiet:
            end += 1
        run = end - index
        if (
            1 <= run <= max_frames
            and end < len(levels)
            and levels[end] >= loud
        ):
            _bridge_hole(samples, index * frame, end * frame)
            filled += 1
        index = max(end, index + 1)
    return filled


def _bridge_hole(samples: array.array, start: int, end: int) -> None:
    """Crossfade the audio just before the hole into the audio just after it."""
    count = end - start
    if count <= 0 or start < count or end + count > len(samples):
        return
    for offset in range(count):
        weight = offset / count
        left = samples[start - count + offset]
        right = samples[end + offset]
        mixed = int(left * (1.0 - weight) + right * weight)
        if mixed > 32767:
            mixed = 32767
        elif mixed < -32768:
            mixed = -32768
        samples[start + offset] = mixed
