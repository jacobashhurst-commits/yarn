"""Microphone capture, straight from CoreAudio.

This used to be `ffmpeg -f avfoundation`, and ffmpeg silently lost audio. A
test tone beeping once per second came back beeping every 0.80 seconds at the
unchanged pitch of 996 Hz, which means one fifth of the samples were never
written -- not written at the wrong rate. Under launchd's background
scheduling the loss reached 73%. Nothing warned about it: no ffmpeg error, no
dropped-frame message, just a meeting whose transcript was mostly missing.

The same aggregate device read through PortAudio returns 11.95 seconds of audio
in an 11.86 second window with no overflow events, so capture happens here now
and ffmpeg is left to do offline channel splitting, which it does reliably.

Run as its own process so launchd owns it -- see the permission note in
ui/install-ui.sh. It stops on SIGINT or SIGTERM and finishes the wav header on
the way out.

Reads one request file, written by `rec start`:
    1  raw output path        4  required channel count
    2  device name            5  log directory
    3  target sample rate (recorded for the record; capture is always native)
"""

from __future__ import annotations

import logging
import signal
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

log = logging.getLogger(__name__)

SAMPLE_WIDTH = 2  # int16, matching what the rest of the pipeline expects

# Long enough that a live microphone is certain to have produced some noise,
# short enough that a misconfigured device is caught before the meeting starts.
PREFLIGHT_SECONDS = 0.5
PREFLIGHT_TIMEOUT = 8.0

# How much audio to write between header rewrites. The header carries the
# length, so refreshing it means a capture killed outright still leaves a
# readable file instead of an unplayable one.
HEADER_REFRESH_SECONDS = 2.0


class CaptureError(RuntimeError):
    """Something that must be shown to whoever asked for the recording."""


@dataclass
class Request:
    raw: Path
    device: str
    sample_rate: int
    required_channels: int
    logs: Path

    @classmethod
    def read(cls, path: Path) -> "Request":
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        if len(lines) < 5:
            raise CaptureError(f"malformed capture request at {path}")
        try:
            sample_rate = int(lines[2])
            required_channels = int(lines[3])
        except ValueError as exc:
            raise CaptureError(f"malformed capture request at {path}: {exc}") from exc
        return cls(
            raw=Path(lines[0]),
            device=lines[1],
            sample_rate=sample_rate,
            required_channels=required_channels,
            logs=Path(lines[4]),
        )


class WavWriter:
    """A wav file whose header stays roughly correct while it is being written.

    The `wave` module only writes its sizes on close, so a capture that is
    killed rather than interrupted leaves a file every tool refuses to open.
    A meeting is not worth losing to that, so the sizes are rewritten as the
    recording goes.
    """

    def __init__(self, path: Path, channels: int, sample_rate: int) -> None:
        self.path = Path(path)
        self.channels = channels
        self.sample_rate = sample_rate
        self.frames = 0
        self._bytes_per_frame = channels * SAMPLE_WIDTH
        self._since_refresh = 0
        self._refresh_bytes = int(HEADER_REFRESH_SECONDS * sample_rate) * self._bytes_per_frame
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("wb")
        self._file.write(self._header(0))

    def _header(self, data_bytes: int) -> bytes:
        byte_rate = self.sample_rate * self._bytes_per_frame
        return (
            b"RIFF"
            + struct.pack("<I", 36 + data_bytes)
            + b"WAVEfmt "
            + struct.pack(
                "<IHHIIHH",
                16,                      # fmt chunk size
                1,                       # PCM
                self.channels,
                self.sample_rate,
                byte_rate,
                self._bytes_per_frame,
                SAMPLE_WIDTH * 8,
            )
            + b"data"
            + struct.pack("<I", data_bytes)
        )

    def write(self, data: bytes) -> None:
        self._file.write(data)
        self.frames += len(data) // self._bytes_per_frame
        self._since_refresh += len(data)
        if self._since_refresh >= self._refresh_bytes:
            self._refresh()

    def _refresh(self) -> None:
        self._since_refresh = 0
        end = self._file.tell()
        self._file.seek(0)
        self._file.write(self._header(end - 44))
        self._file.seek(end)
        self._file.flush()

    def close(self) -> None:
        if self._file.closed:
            return
        self._refresh()
        self._file.close()

    @property
    def seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def resolve_device(name: str, required_channels: int):
    """Find an input device by name and confirm it has the channels we need."""
    import sounddevice as sd

    devices = sd.query_devices()
    inputs = [d for d in devices if d["max_input_channels"] > 0]
    match = next((d for d in inputs if d["name"] == name), None)
    if match is None:
        # PortAudio and ffmpeg occasionally disagree on trailing detail in a
        # device name, so fall back to a prefix before giving up.
        match = next((d for d in inputs if d["name"].startswith(name)), None)
    if match is None:
        available = ", ".join(repr(d["name"]) for d in inputs) or "none"
        raise CaptureError(
            f'no audio input device named "{name}". Available: {available}. '
            f"Run 'ma devices' and set audio.device_name."
        )
    if match["max_input_channels"] < required_channels:
        raise CaptureError(
            f'device "{match["name"]}" offers {match["max_input_channels"]} '
            f"input channel(s), but this mode needs {required_channels}. "
            f"Check the Aggregate Device channel layout in Audio MIDI Setup."
        )
    return match


def capture(request: Request, stop: threading.Event) -> WavWriter:
    """Record the device into request.raw until `stop` is set."""
    import sounddevice as sd

    device = resolve_device(request.device, request.required_channels)
    channels = device["max_input_channels"]
    sample_rate = int(device["default_samplerate"])

    blocks: Queue[bytes] = Queue()
    overflows = 0

    def on_audio(data, frames, _time, status) -> None:
        nonlocal overflows
        if status and status.input_overflow:
            # Unlike ffmpeg's silence, this is audio we know was lost.
            overflows += 1
        blocks.put(bytes(data))

    writer: WavWriter | None = None
    started = 0.0
    try:
        with sd.RawInputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            device=device["name"],
            latency="high",
            callback=on_audio,
        ):
            started = time.monotonic()
            preflight = _collect_preflight(blocks, sample_rate, channels, stop)
            # macOS hands a process without microphone access a stream of
            # zeros rather than refusing, so a capture of literal silence means
            # the permission is missing, not that the room is quiet. Even an
            # empty room produces some noise in the first half second.
            if preflight is not None and not any(preflight):
                raise CaptureError(
                    "the audio device is returning digital silence, so nothing "
                    "would be recorded. macOS gives a process without "
                    "microphone access silence instead of an error. Open System "
                    "Settings > Privacy & Security > Microphone, allow the "
                    "recorder, and try again."
                )

            # Created only once the device has proven itself, because `rec
            # start` treats the appearance of this file as "recording now".
            writer = WavWriter(request.raw, channels, sample_rate)
            if preflight:
                writer.write(bytes(preflight))

            while not stop.is_set():
                try:
                    writer.write(blocks.get(timeout=0.2))
                except Empty:
                    continue

            # Whatever the device handed over before the stream closed.
            while True:
                try:
                    writer.write(blocks.get_nowait())
                except Empty:
                    break
    except sd.PortAudioError as exc:
        raise CaptureError(
            f'could not open audio device "{request.device}": {exc}'
        ) from exc
    finally:
        if writer is not None:
            writer.close()

    elapsed = time.monotonic() - started
    log.info(
        "captured %s: %.2fs of audio in %.2fs (%.1f%% of real time), "
        "%d channel(s) at %d Hz, %d overflow(s)",
        request.raw.name, writer.seconds, elapsed,
        100 * writer.seconds / elapsed if elapsed else 0,
        channels, sample_rate, overflows,
    )
    if overflows:
        log.warning(
            "%d buffer overflow(s) during %s: some audio was dropped by the "
            "operating system, so the transcript will have gaps",
            overflows, request.raw.name,
        )
    return writer


def _collect_preflight(
    blocks: "Queue[bytes]", sample_rate: int, channels: int, stop: threading.Event
) -> bytearray | None:
    """Gather the opening moments of the stream so it can be checked.

    Returns None if the device produced nothing at all in time, which the
    caller reports as a device failure rather than as silence.
    """
    wanted = int(PREFLIGHT_SECONDS * sample_rate) * channels * SAMPLE_WIDTH
    collected = bytearray()
    deadline = time.monotonic() + PREFLIGHT_TIMEOUT
    while len(collected) < wanted and time.monotonic() < deadline and not stop.is_set():
        try:
            collected.extend(blocks.get(timeout=0.2))
        except Empty:
            continue
    if not collected:
        return None
    return collected


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: ma-capture <request-file>", file=sys.stderr)
        return 2

    request_path = Path(argv[0])
    error_file = request_path.parent / ".capture-error"
    error_file.unlink(missing_ok=True)

    stop = threading.Event()
    # SIGINT is what `rec stop` sends; SIGTERM is what launchd sends when the
    # job is unloaded. Both mean "finish the file properly".
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())

    try:
        request = Request.read(request_path)
        request.logs.mkdir(parents=True, exist_ok=True)
        # stderr only. Whoever started the recorder has already decided where
        # that goes -- launchd to recorder.log, a terminal start to rec.log --
        # and adding a file handler as well wrote every line twice.
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            handlers=[logging.StreamHandler(sys.stderr)],
            force=True,
        )
        capture(request, stop)
    except CaptureError as exc:
        # `rec start` watches for this file, so a failure in here reaches the
        # screen instead of being a job that quietly exited.
        try:
            error_file.write_text(f"{exc}\n", encoding="utf-8")
        except OSError:
            pass
        print(f"ma-capture: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the message has to get out
        try:
            error_file.write_text(f"the recorder failed: {exc}\n", encoding="utf-8")
        except OSError:
            pass
        log.exception("capture failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
