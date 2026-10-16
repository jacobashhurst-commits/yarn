"""Orchestration: recording -> transcript -> index.

Two entry points that matter.

`finalize` runs the moment recording stops. It splits the aggregate-device
capture into the two channels, writes the manifest into the inbox last, and
returns. It never transcribes, because Jacob has just walked out of a meeting
and the only job here is getting the audio safe.

`process` runs in the watcher. It is the slow half: transcribe, diarize the
multi-person channels, merge, detect names, write the transcript, index it,
archive the audio.

Diarization sits between transcription and merging because the merge groups
turns by voice, so the speaker has to be attached to each segment before turns
are formed.

`ingest` is split out from `process` so notes arriving late, or an embedding
model change, only cost a reindex rather than a re-transcription.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import audio, jobs, metrics, records, resources
from .chunking import chunk_turns, context_header
from .config import Config
from .diarize import (
    Diarizer,
    DiarizationUnavailable,
    assign_speakers,
)
from .merge import merge_segments, split_long_turns
from .models import MIC, ROOM, SYS, Segment, SpeakerSpan
from .names import (
    attendee_suggestion,
    detect_names,
    infer_speaker_names,
    speaker_guess_map,
)
from .quips import apply_guesses, profile_speakers
from .records import Manifest, Transcript
from .store import MeetingStore
from .summarize import (
    DEFERRED_SUMMARY,
    Summarizer,
    SummaryUnavailable,
    deferred_summary_reason,
    should_attempt_summary,
)
from .transcribe import Transcriber

log = logging.getLogger(__name__)


def _about(cfg: Config, meeting_id: str) -> str:
    """Whatever Jacob typed when the recording stopped, if anything yet."""
    notes = records.read_notes(cfg.notes_json(meeting_id))
    text = str(notes.get("notes") or "").strip()
    if text:
        return " ".join(text.splitlines()[0].split())[:56]
    job = jobs.load(cfg, meeting_id)
    title = str(getattr(job, "title", "") or "").strip()
    if title and title.lower() != "meeting":
        return title[:56]
    return ""


def _processing_status(
    cfg: Config,
    meeting_id: str,
    phase: str,
    percent: int,
    detail: str = "",
    *,
    audio_position_s: float | None = None,
    audio_duration_s: float | None = None,
    indeterminate: bool = False,
    steal: bool = True,
) -> None:
    """Publish a tiny atomic status file for the SwiftBar menu.

    `percent` is stage-local and only meaningful when we have a real signal.
    Indeterminate stages still send 0 rather than inventing 65 or 82.
    Pass steal=False for short side work (summaries) so a longer stage keeps
    the menu bar.
    """
    from . import queue as queue_mod

    if not steal and _status_owned_by_other(cfg, meeting_id):
        return

    batch = queue_mod.begin_work(cfg, meeting_id)
    now = int(datetime.now().timestamp())
    previous = records.read_notes(cfg.processing_status)
    phase_started = now
    if previous.get("meeting_id") == meeting_id and previous.get("phase") == phase:
        phase_started = int(previous.get("phase_started_epoch") or now)
    job = jobs.load(cfg, meeting_id)
    meeting_s = float(audio_duration_s or (job.duration_s if job else 0.0) or 0.0)
    percent = min(max(int(percent), 0), 100)
    elapsed = max(0, now - phase_started)
    eta, estimated = _phase_estimate(cfg, phase, meeting_s, elapsed, percent, indeterminate)
    expected_s = _expected_phase_s(cfg, phase, meeting_s)
    payload = {
        "meeting_id": meeting_id,
        "phase": phase,
        "percent": percent,
        "detail": detail,
        "updated_epoch": now,
        "indeterminate": bool(indeterminate),
        "title": _about(cfg, meeting_id),
        "remaining": int(batch.get("remaining") or 0),
        "current": int(batch.get("current") or 0),
        "total": int(batch.get("total") or 0),
        "phase_started_epoch": phase_started,
        "elapsed": _fmt_span(elapsed),
        "eta": eta,
        "estimated_percent": estimated,
        # Long silent stages (voice separation) never rewrite this file, so
        # the menu works out elapsed / remaining itself from these two.
        "expected_s": int(expected_s),
        "stale_after_epoch": now + int(max(7200, expected_s * 2.5)),
        "recording_length": _fmt_span(meeting_s) if meeting_s else "",
        **_step_summary(job, phase),
    }
    if audio_position_s is not None:
        payload["audio_position_s"] = round(float(audio_position_s), 1)
    if audio_duration_s is not None:
        payload["audio_duration_s"] = round(float(audio_duration_s), 1)
    records.write_json_atomic(cfg.processing_status, payload)


_PHASE_STAGE = {
    "Transcribing": "transcribe",
    "Identifying speakers": "diarize",
    "Recognising known speakers": "recognise",
    "Cleaning transcript": "cleanup",
    "Indexing": "index",
    "Indexing for search": "index",
    "Writing summary": "summarize",
}

# Display order follows what actually runs: the summary is attempted after
# the meeting is already indexed.
_STEP_LABELS = (
    ("transcribe", "Transcribe"),
    ("diarize", "Separate voices"),
    ("recognise", "Match known voices"),
    ("cleanup", "Clean up"),
    ("index", "Search index"),
    ("summarize", "Summary"),
)


def _fmt_span(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _step_summary(job, phase: str) -> dict:
    """"Step 2 of 5" plus a one-line checklist, for the menu bar."""
    if job is None:
        return {"step": "", "steps": ""}
    active = _PHASE_STAGE.get(phase, job.current_stage)
    shown = [
        (name, label)
        for name, label in _STEP_LABELS
        if job.stages[name].status != jobs.SKIPPED or name == active
    ]
    parts: list[str] = []
    step = ""
    for position, (name, label) in enumerate(shown, start=1):
        stage = job.stages[name]
        if name == active and stage.status in (jobs.RUNNING, jobs.PENDING):
            parts.append(f"▶ {label}")
            step = f"Step {position} of {len(shown)}: {label}"
        elif stage.status == jobs.DONE:
            took = f" {_fmt_span(stage.duration_s)}" if stage.duration_s >= 1 else ""
            parts.append(f"✓ {label}{took}")
        elif stage.status == jobs.STAGE_FAILED:
            parts.append(f"✗ {label}")
        else:
            parts.append(f"○ {label}")
    return {"step": step, "steps": "  ".join(parts)}


def _phase_estimate(
    cfg: Config,
    phase: str,
    meeting_s: float,
    elapsed: int,
    percent: int,
    indeterminate: bool,
) -> tuple[str, int]:
    """("about 40m left", estimated percent) from real progress or past speed."""
    if not indeterminate and 3 <= percent < 100 and elapsed >= 20:
        left = elapsed * (100 - percent) / percent
        return f"about {_fmt_span(left)} left", percent
    expected = _expected_phase_s(cfg, phase, meeting_s)
    if expected < 30:
        return "", 0
    estimated = min(95, int(elapsed * 100 / expected))
    left = expected - elapsed
    if left <= 0:
        return "taking longer than usual", 95
    return f"about {_fmt_span(left)} left (typical for a {_fmt_span(meeting_s)} recording)", estimated


def _expected_phase_s(cfg: Config, phase: str, meeting_s: float) -> float:
    stage = _PHASE_STAGE.get(phase)
    if not stage or meeting_s <= 0:
        return 0.0
    return metrics.typical_realtime_factor(cfg, stage) * meeting_s


def _clear_processing_status(cfg: Config, meeting_id: str) -> None:
    from . import queue as queue_mod

    try:
        current = records.read_notes(cfg.processing_status)
        if current.get("meeting_id") == meeting_id:
            cfg.processing_status.unlink(missing_ok=True)
            queue_mod.end_work(cfg, meeting_id)
    except OSError:
        pass


def _status_owned_by_other(cfg: Config, meeting_id: str) -> bool:
    """True when another meeting still owns the menu-bar status file.

    Summaries and one-shot `ma process` runs must not wipe the in-flight
    diarize status of a longer job - that is what made the bar go idle while
    voice separation was still running after the lid opened.
    """
    current = records.read_notes(cfg.processing_status)
    owner = str(current.get("meeting_id") or "")
    if not owner or owner == meeting_id:
        return False
    try:
        stale_after = int(current.get("stale_after_epoch") or 0)
    except (TypeError, ValueError):
        stale_after = 0
    if stale_after and int(datetime.now().timestamp()) >= stale_after:
        return False
    return True


# ---------------------------------------------------------------------------
# stop-of-recording
# ---------------------------------------------------------------------------

def active_capture(cfg: Config) -> Path | None:
    """The raw file the current recording is writing to, if one is running."""
    try:
        lines = (cfg.root / ".recording").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return Path(lines[2]) if len(lines) > 2 and lines[2] else None


def stranded_captures(cfg: Config) -> list[Path]:
    """Raw captures in in-progress that no longer belong to a recording.

    finalize deliberately leaves a capture it refuses exactly where it is, so a
    rejected meeting stays recoverable instead of being deleted. The cost is
    that nothing then points at it, so it has to be reported -- two meetings
    once sat here for an hour with no sign of them anywhere.
    """
    active = active_capture(cfg)
    if not cfg.in_progress_dir.exists():
        return []
    return sorted(p for p in cfg.in_progress_dir.glob("*__raw.wav") if p != active)


# A failed `rec start` can leave a few seconds of audio behind: the capture
# process outlived the menu's "exited before started" error. Those stubs are
# not unpublished meetings. Real refused captures (silence, clock slip) are
# usually much longer.
ABORTED_START_MAX_S = 15.0


def aborted_start_captures(cfg: Config) -> list[Path]:
    """Short leftover wavs from a Start that the menu already treated as failed."""
    aborted: list[Path] = []
    for path in stranded_captures(cfg):
        try:
            info = audio.probe(path)
        except (OSError, audio.AudioError, ValueError, KeyError):
            continue
        if float(info.get("duration") or 0.0) <= ABORTED_START_MAX_S:
            aborted.append(path)
    return aborted


def discard_aborted_starts(cfg: Config) -> list[str]:
    removed: list[str] = []
    for path in aborted_start_captures(cfg):
        path.unlink(missing_ok=True)
        removed.append(path.name)
        log.info("discarded aborted start %s", path.name)
    return removed


def finalize(
    cfg: Config,
    raw_path: Path,
    started_at: str = "",
    mode: str = "call",
    expected_duration_s: float = 0.0,
) -> Manifest:
    """Split a raw capture into channels and publish it to the inbox.

    mode="call"  two channels: Jacob on the mic, everyone else on system audio.
    mode="room"  one channel from the microphone, carrying several people none
                 of whom can be told apart. Labelled "Unknown" rather than
                 "Jacob", because attributing the whole room to Jacob would be
                 wrong in a way nobody would ever notice afterwards.
    """
    if mode not in ("call", "room"):
        raise ValueError(f"unknown recording mode {mode!r}; expected call or room")
    cfg.ensure_dirs()
    raw_path = Path(raw_path)
    if not raw_path.exists():
        raise FileNotFoundError(raw_path)

    meeting_id = raw_path.name.split("__")[0]
    info = audio.probe(raw_path)

    # A capture of literal zeros is never a quiet meeting; it means the audio
    # never arrived. Refusing here leaves the raw file where it is and tells
    # Jacob now, instead of publishing a meeting whose transcript would come
    # back empty an hour later with nothing to explain why.
    if audio.is_silent(raw_path):
        raise audio.AudioError(
            f"{raw_path.name} contains no audio at all (digital silence for "
            f"{info['duration']:.0f}s). The recording was not saved.\n"
            f"macOS gives an app that lacks microphone access silence rather "
            f"than an error, so check System Settings > Privacy & Security > "
            f"Microphone and enable whatever started the recording (SwiftBar "
            f"for the menu bar, your terminal for the command line)."
        )

    # File duration must track elapsed wall time. A live 48 -> 16 kHz
    # conversion once wrote only 52 seconds of samples during a 193-second
    # meeting, making both playback and speech roughly 3.7x too fast. A valid
    # recording can differ by a fraction of a second while ffmpeg starts and
    # stops, but never by minutes.
    if expected_duration_s > 0:
        difference = abs(info["duration"] - expected_duration_s)
        tolerance = max(2.0, expected_duration_s * 0.05)
        if difference > tolerance:
            raise audio.AudioError(
                f"{raw_path.name} has {info['duration']:.1f}s of audio but the "
                f"recording ran for {expected_duration_s:.1f}s. The capture "
                f"clock lost {difference:.1f}s, so publishing it would create "
                f"a sped-up, incomplete transcript. The raw file was kept at "
                f"{raw_path} for recovery."
            )

    channels: dict[str, str] = {}
    if mode == "room":
        # System audio is ignored entirely: in a meeting room there is nothing
        # playing through the laptop, and a silent channel would only give
        # Whisper somewhere to hallucinate.
        room_path = cfg.inbox_dir / f"{meeting_id}__room.wav"
        audio.extract_mono(raw_path, room_path, cfg.audio.mic_channels, cfg.audio.sample_rate)
        channels[ROOM] = room_path.name
    else:
        mic_path = cfg.inbox_dir / f"{meeting_id}__mic.wav"
        sys_path = cfg.inbox_dir / f"{meeting_id}__sys.wav"
        audio.extract_mono(raw_path, mic_path, cfg.audio.mic_channels, cfg.audio.sample_rate)
        audio.extract_mono(raw_path, sys_path, cfg.audio.sys_channels, cfg.audio.sample_rate)
        channels[MIC] = mic_path.name
        channels[SYS] = sys_path.name

    # Keep the raw multi-channel capture next to the split channels until the
    # meeting is processed. It is the only copy of the unmixed audio, and if
    # the channel mapping in config turns out to be wrong, it is what makes
    # that recoverable rather than lost.
    raw_kept = cfg.inbox_dir / raw_path.name
    shutil.move(str(raw_path), str(raw_kept))

    manifest = Manifest(
        meeting_id=meeting_id,
        channels=channels,
        started_at=started_at,
        ended_at=datetime.now().isoformat(timespec="seconds"),
        duration_s=info["duration"],
        raw_file=raw_kept.name,
        source="native",
    )
    # Written last: the watcher triggers on manifests only, so nothing can be
    # picked up before both channels are fully on disk.
    manifest.write(cfg.inbox_dir / f"{meeting_id}.json")
    jobs.enqueue(cfg, meeting_id, source="native", duration_s=info["duration"])
    from . import queue as queue_mod

    queue_mod.refresh(cfg)
    log.info(
        "finalized meeting=%s mode=%s duration=%.1fs", meeting_id, mode, info["duration"]
    )
    return manifest


# ---------------------------------------------------------------------------
# indexing
# ---------------------------------------------------------------------------

def ingest(cfg: Config, meeting_id: str, store: MeetingStore) -> int:
    """(Re)index a meeting from its stored transcript and notes."""
    transcript_path = cfg.transcript_json(meeting_id)
    if not transcript_path.exists():
        raise FileNotFoundError(
            f"no transcript for {meeting_id} at {transcript_path}"
        )

    transcript = Transcript.read(transcript_path)
    from .glossary import apply_glossary_to_transcript

    if apply_glossary_to_transcript(transcript, cfg.glossary.replacements):
        transcript.write(transcript_path)
    notes = records.read_notes(cfg.notes_json(meeting_id))
    previous_md = cfg.transcript_md(meeting_id)
    transcript.title = records.meeting_title(notes, transcript)
    transcript.md_name = records.markdown_filename(meeting_id, transcript.title)
    transcript.write(transcript_path)
    label = records.date_label(meeting_id)

    # Names Jacob has confirmed for diarized voices. Read at index time rather
    # than baked into the transcript, so naming a voice later is a reindex.
    roster = records.speaker_roster(notes)
    from .models import speaker_display_indices

    display = speaker_display_indices(transcript.speaker_profiles)
    labeller = cfg.turn_labeller(roster, display)

    detected = attendee_suggestion(transcript.detected_names)
    header = ""
    if cfg.chunk.include_context_header:
        header = context_header(
            date_label=label,
            attendees=notes.get("attendees", ""),
            detected_names=detected,
            notes=notes.get("notes", ""),
        )

    chunks = chunk_turns(
        transcript.turns,
        target_chars=cfg.chunk.target_chars,
        overlap_turns=cfg.chunk.overlap_turns,
        label_fn=labeller,
        header=header,
    )

    try:
        epoch = int(records.parse_meeting_id(meeting_id).timestamp())
        date_only = records.parse_meeting_id(meeting_id).date().isoformat()
    except ValueError:
        epoch, date_only = 0, ""

    metadatas = []
    for chunk in chunks:
        metadatas.append(
            {
                "meeting_id": meeting_id,
                "date": date_only,
                "date_label": label,
                "epoch": epoch,
                "chunk_index": chunk.index,
                "start_s": round(chunk.start, 2),
                "end_s": round(chunk.end, 2),
                # The resolved labels actually used in the chunk text, so
                # filtering on a speaker and reading the chunk agree.
                "speakers": chunk.speakers or [cfg.label(c) for c in chunk.channels],
                "attendees": notes.get("attendees", ""),
                "notes": notes.get("notes", ""),
                "detected_names": detected,
                "identified_speakers": ", ".join(
                    sorted({v for v in roster.values() if v})
                ),
                "has_notes": bool(notes.get("notes") or notes.get("attendees")),
                "duration_s": round(transcript.duration_s, 1),
                "title": transcript.title,
                "transcript_path": str(cfg.transcript_md(meeting_id)),
            }
        )

    count = store.replace_meeting(
        meeting_id,
        documents=[c.text for c in chunks],
        metadatas=metadatas,
    )

    # Re-render the markdown so notes and speaker names show up in the
    # readable transcript too. The file is named from the title, so a later
    # notes popup can rename it; drop the previous name if it changed.
    md_path = cfg.transcript_md(meeting_id)
    records.write_text_atomic(
        md_path,
        records.render_markdown(
            transcript,
            labeller,
            notes,
            processing=records.processing_from_job(jobs.load(cfg, meeting_id)),
        ),
    )
    if previous_md != md_path and previous_md.exists():
        previous_md.unlink()
    stamped = cfg.transcript_json(meeting_id).with_suffix(".md")
    if stamped != md_path and stamped.exists():
        stamped.unlink()

    if notes and not notes.get("applied", True):
        records.mark_notes_applied(cfg.notes_json(meeting_id))

    return count


def apply_summary(
    cfg: Config,
    meeting_id: str,
    summarizer: Summarizer,
    store: MeetingStore,
) -> bool:
    """Fill in a deferred summary and re-render the readable transcript.

    Returns True when a summary was written. Leaves the meeting unchanged if
    there is no speech to summarise.
    """
    path = cfg.transcript_json(meeting_id)
    if not path.exists():
        raise FileNotFoundError(path)
    transcript = Transcript.read(path)
    if not transcript.turns:
        return False
    notes = records.read_notes(cfg.notes_json(meeting_id))
    roster = records.speaker_roster(notes)
    from . import queue as queue_mod

    queue_mod.refresh(cfg, exclude=meeting_id)
    owned_status = not _status_owned_by_other(cfg, meeting_id)
    _processing_status(
        cfg,
        meeting_id,
        "Writing summary",
        0,
        indeterminate=True,
        steal=False,
    )
    try:
        from .glossary import apply_glossary

        from .models import speaker_display_indices

        display = speaker_display_indices(transcript.speaker_profiles)
        transcript.summary = apply_glossary(
            summarizer.summarize(
                transcript.turns, cfg.turn_labeller(roster, display)
            ),
            cfg.glossary.replacements,
        )
        transcript.summary_model = cfg.summary.model
        transcript.summary_error = ""
        transcript.write(path)
        ingest(cfg, meeting_id, store)
        return True
    except SummaryUnavailable as exc:
        # Interrupted or deferred: keep the transcript, drop any partial
        # summary, retry later. An empty summary is the signal to try again.
        transcript.summary = ""
        transcript.summary_error = str(exc)
        transcript.write(path)
        raise
    finally:
        # Only clear when this call owned the status file - never erase an
        # in-flight Separate-voices card belonging to another meeting.
        if owned_status:
            _clear_processing_status(cfg, meeting_id)
        queue_mod.refresh(cfg)


def needs_summary(transcript: Transcript) -> bool:
    """True when capture finished without a summary, and there is speech."""
    if not transcript.turns:
        return False
    if transcript.summary.strip():
        return False
    return True


def rediarize(
    cfg: Config,
    meeting_id: str,
    diarizer: Diarizer,
    store: MeetingStore,
    summarizer: Summarizer | None = None,
) -> Transcript:
    """Recompute speakers from archived audio without rerunning Whisper.

    Speaker count corrections should cost minutes, not a second full
    transcription. The original channel WAVs are retained for exactly this
    sort of derived-data rebuild.
    """
    transcript_path = cfg.transcript_json(meeting_id)
    if not transcript_path.exists():
        raise FileNotFoundError(transcript_path)
    transcript = Transcript.read(transcript_path)

    targets: list[tuple[str, Path]] = []
    for channel in diarizer.config.diarize_channels:
        path = cfg.audio_archive_dir / f"{meeting_id}__{channel}.wav"
        if path.exists() and diarizer.channel_enabled(channel):
            targets.append((channel, path))
    if not targets:
        raise FileNotFoundError(
            f"no retained diarizable audio for meeting {meeting_id}"
        )

    notes = records.read_notes(cfg.notes_json(meeting_id))
    expected_speakers = records.expected_diarized_speakers(
        notes,
        {channel for channel, _ in targets},
        cfg.names.own_names,
    )
    _processing_status(
        cfg,
        meeting_id,
        "Identifying speakers",
        0,
        "automatic speaker count",
        indeterminate=True,
    )
    try:
        spans: list[SpeakerSpan] = []
        for channel, path in targets:
            spans.extend(
                diarizer.diarize_file(
                    path,
                    channel,
                    expected_speakers=expected_speakers,
                )
            )

        target_channels = {channel for channel, _ in targets}
        for segment in transcript.segments:
            if segment.channel in target_channels:
                segment.speaker = None
        assign_speakers(transcript.segments, spans)

        turns = merge_segments(
            transcript.segments,
            coalesce_gap_s=cfg.chunk.coalesce_gap_s,
            max_turn_s=cfg.chunk.max_turn_s,
        )
        transcript.turns = split_long_turns(
            turns, max_chars=cfg.chunk.max_turn_chars
        )
        transcript.speaker_spans = spans
        transcript.speaker_guesses = (
            infer_speaker_names(
                transcript.turns,
                own_names=cfg.names.own_names,
                own_label=cfg.own_label,
            )
            if cfg.names.enabled and spans
            else []
        )
        transcript.speaker_profiles = profile_speakers(
            transcript.turns,
            guesses=speaker_guess_map(transcript.speaker_guesses),
        )
        # Rediarizing renumbers the voices, so any known-speaker match has to
        # be recomputed against the new keys rather than carried over. Kept
        # non-fatal: losing a suggestion must not cost the rediarize.
        if cfg.speakers_memory.recognise_known and spans:
            try:
                from . import speakers as speaker_memory

                extra = speaker_memory.suggest_matches(
                    transcript.speaker_profiles,
                    speaker_memory.load_library(cfg),
                    embeddings=speaker_memory.embeddings_for_profiles(
                        cfg,
                        meeting_id,
                        transcript.turns,
                        transcript.speaker_profiles,
                        spans=spans,
                        segments=transcript.segments,
                    ),
                    min_confidence=cfg.speakers_memory.min_confidence,
                    min_margin=cfg.speakers_memory.min_margin,
                )
                named = {guess.speaker for guess in transcript.speaker_guesses}
                transcript.speaker_guesses.extend(
                    g for g in extra if g.speaker not in named
                )
            except Exception:
                log.exception("speaker memory failed on rediarize for %s", meeting_id)
        apply_guesses(
            transcript.speaker_profiles,
            speaker_guess_map(transcript.speaker_guesses),
        )
        transcript.diarize_model = cfg.diarize.model if spans else ""
        transcript.diarize_error = ""

        # The old summary referred to obsolete Speaker N labels. Rewrite it
        # now when Ollama is available; otherwise remove the misleading text
        # and let `ma summarize --today` fill it later. A refused summary
        # must not throw away the speaker labels we just computed.
        transcript.summary = ""
        transcript.summary_model = ""
        if summarizer is not None and summarizer.enabled and summarizer.is_running():
            _processing_status(
                cfg, meeting_id, "Writing summary", 0, indeterminate=True
            )
            roster = records.speaker_roster(notes)
            from .models import speaker_display_indices

            display = speaker_display_indices(transcript.speaker_profiles)
            try:
                transcript.summary = summarizer.summarize(
                    transcript.turns, cfg.turn_labeller(roster, display)
                )
                transcript.summary_model = cfg.summary.model
                transcript.summary_error = ""
            except SummaryUnavailable as exc:
                transcript.summary_error = str(exc)
                log.error("summary unavailable after rediarize meeting=%s: %s", meeting_id, exc)
        else:
            transcript.summary_error = DEFERRED_SUMMARY

        transcript.write(transcript_path)
        _processing_status(
            cfg, meeting_id, "Indexing for search", 0, indeterminate=True
        )
        ingest(cfg, meeting_id, store)
        return Transcript.read(transcript_path)
    finally:
        _clear_processing_status(cfg, meeting_id)


def consolidate_voices(cfg: Config, meeting_id: str, store: MeetingStore) -> int:
    """Merge diarized tracks that are the same person, without re-segmenting.

    A long call can leave one clear voice as several tracks. The names already
    confirmed stay on the track we keep. Returns how many tracks were absorbed.
    """
    from .diarize import (
        SAME_VOICE_COSINE,
        _cosine,
        _embed_samples,
        _speaker_waveform,
        group_same_voices,
    )
    from .models import SpeakerSpan
    import wave

    import numpy as np

    transcript_path = cfg.transcript_json(meeting_id)
    transcript = Transcript.read(transcript_path)
    if not transcript.speaker_spans:
        return 0
    by_channel: dict[str, list] = {}
    for span in transcript.speaker_spans:
        by_channel.setdefault(span.channel, []).append(span)

    mapping: dict[str, str] = {}
    for channel, spans in by_channel.items():
        wav_path = cfg.audio_archive_dir / f"{meeting_id}__{channel}.wav"
        if not wav_path.exists():
            continue
        with wave.open(str(wav_path), "rb") as wav:
            frames = wav.readframes(wav.getnframes())
            sample_rate = wav.getframerate()
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        durations: dict[str, float] = {}
        for span in spans:
            durations[span.speaker] = durations.get(span.speaker, 0.0) + span.duration
        speakers = [speaker for speaker, total in durations.items() if total >= 1.5]
        vectors: dict[str, list[float]] = {}
        for speaker in speakers:
            clip = _speaker_waveform(samples, sample_rate, spans, speaker, max_s=8.0)
            if clip is None:
                continue
            vector = _embed_samples(cfg.diarize, clip, sample_rate)
            if vector:
                vectors[speaker] = vector
        if len(vectors) < 2:
            continue

        def cosine(left: str, right: str, _vectors=vectors) -> float:
            return _cosine(_vectors[left], _vectors[right])

        mapping.update(
            group_same_voices(list(vectors), cosine, durations, SAME_VOICE_COSINE)
        )

    absorbed = sum(1 for speaker, root in mapping.items() if speaker != root)
    if absorbed == 0:
        return 0

    def canon(speaker: str | None) -> str | None:
        if not speaker:
            return speaker
        return mapping.get(speaker, speaker)

    transcript.speaker_spans = [
        SpeakerSpan(
            channel=span.channel,
            start=span.start,
            end=span.end,
            speaker=canon(span.speaker) or span.speaker,
        )
        for span in transcript.speaker_spans
    ]
    for segment in transcript.segments:
        segment.speaker = canon(segment.speaker)
    turns = merge_segments(
        transcript.segments,
        coalesce_gap_s=cfg.chunk.coalesce_gap_s,
        max_turn_s=cfg.chunk.max_turn_s,
    )
    transcript.turns = split_long_turns(turns, max_chars=cfg.chunk.max_turn_chars)
    from .quips import profile_speakers

    transcript.speaker_profiles = profile_speakers(transcript.turns)
    notes_path = cfg.notes_json(meeting_id)
    notes = records.read_notes(notes_path)
    roster = dict(notes.get("speakers") or {})
    kept: dict[str, str] = {}
    for speaker, name in roster.items():
        root = mapping.get(speaker, speaker)
        if name and root not in kept:
            kept[root] = name
    notes["speakers"] = kept
    records.write_json_atomic(notes_path, notes)
    transcript.write(transcript_path)
    ingest(cfg, meeting_id, store)
    log.info("consolidated %d extra voice track(s) in %s", absorbed, meeting_id)
    return absorbed


# ---------------------------------------------------------------------------
# processing
# ---------------------------------------------------------------------------

def _fmt_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _release_if_needed(obj) -> None:
    if not resources.is_low_memory_machine():
        return
    unload = getattr(obj, "unload", None)
    if callable(unload):
        unload()


def _whisper_engine(transcriber) -> str:
    config = getattr(transcriber, "config", None)
    return str(getattr(config, "engine", "") or "")


def process(
    cfg: Config,
    manifest_path: Path,
    transcriber: Transcriber,
    store: MeetingStore,
    diarizer: "Diarizer | None" = None,
    summarizer: "Summarizer | None" = None,
    *,
    force: bool = False,
) -> str:
    """Process one inbox manifest end to end. Returns the meeting id."""
    cfg.ensure_dirs()
    manifest_path = Path(manifest_path)
    manifest = Manifest.read(manifest_path)
    meeting_id = manifest.meeting_id

    # Claim the meeting by moving its files out of the inbox. A move is atomic
    # within a filesystem, so two watchers could never both claim it.
    claimed = _claim(cfg, manifest, manifest_path)
    job = jobs.enqueue(
        cfg,
        meeting_id,
        source=manifest.source or "native",
        duration_s=manifest.duration_s,
        title=manifest.title,
    )
    from . import queue as queue_mod

    queue_mod.refresh(cfg, exclude=meeting_id)
    _processing_status(cfg, meeting_id, "Preparing audio", 0, indeterminate=True)

    try:
        # Driven by the manifest, not a fixed list, so a room recording with a
        # single channel and a call with two go through the same path.
        segments: list[Segment] = []
        if not manifest.channels:
            raise ValueError(f"manifest for {meeting_id} lists no channels")

        checkpoint_path = jobs.whisper_checkpoint_path(cfg, meeting_id)
        skip_whisper = job.stage_done("transcribe") and (
            checkpoint_path.exists() or cfg.transcript_json(meeting_id).exists()
        )
        if skip_whisper:
            segments = _load_whisper_segments(cfg, meeting_id, checkpoint_path)
            if not segments:
                skip_whisper = False
        if skip_whisper:
            log.info(
                "resuming meeting=%s from Whisper checkpoint (%d segments)",
                meeting_id,
                len(segments),
            )
        else:
            jobs.begin_stage(cfg, job, "transcribe")
            transcribe_started = time.perf_counter()
            channels = sorted(manifest.channels.items())
            last_progress = -1
            n_channels = max(len(channels), 1)
            for channel_index, (channel, filename) in enumerate(channels):
                path = cfg.processing_dir / filename

                def report(
                    fraction: float, *, name=channel, index=channel_index
                ) -> None:
                    nonlocal last_progress
                    overall = (index + fraction) / n_channels
                    percent = int(overall * 100)
                    if percent <= last_progress:
                        return
                    last_progress = percent
                    position = overall * (manifest.duration_s or 0.0)
                    detail = f"{name} audio"
                    if manifest.duration_s:
                        detail = (
                            f"{_fmt_clock(position)} / "
                            f"{_fmt_clock(manifest.duration_s)} {name}"
                        )
                    _processing_status(
                        cfg,
                        meeting_id,
                        "Transcribing",
                        percent,
                        detail,
                        audio_position_s=position,
                        audio_duration_s=manifest.duration_s or None,
                    )

                report(0.0)
                segments.extend(
                    transcriber.transcribe_file(
                        path,
                        channel,
                        duration_s=manifest.duration_s,
                        progress=report,
                    )
                )
            jobs.finish_stage(cfg, job, "transcribe")
            # Persist before the long diarize stage so a reboot / kill can
            # skip Whisper and pick up at Separate voices.
            _write_whisper_checkpoint(
                cfg,
                meeting_id,
                segments,
                model=cfg.whisper.model,
                engine=_whisper_engine(transcriber),
            )
            metrics.record(
                cfg,
                meeting_id=meeting_id,
                stage="transcribe",
                engine=_whisper_engine(transcriber),
                model=cfg.whisper.model,
                meeting_duration_s=manifest.duration_s,
                wall_s=time.perf_counter() - transcribe_started,
            )
            _release_if_needed(transcriber)

        jobs.begin_stage(cfg, job, "diarize")
        _processing_status(
            cfg,
            meeting_id,
            "Identifying speakers",
            0,
            "Analysing speaker segments",
            indeterminate=True,
        )
        diarize_started = time.perf_counter()
        spans, diarize_error = _diarize_channels(
            cfg,
            manifest,
            segments,
            diarizer,
            status_meeting_id=meeting_id,
        )
        jobs.finish_stage(
            cfg,
            job,
            "diarize",
            status=jobs.STAGE_FAILED if diarize_error else jobs.DONE,
            error=diarize_error,
        )
        metrics.record(
            cfg,
            meeting_id=meeting_id,
            stage="diarize",
            engine=cfg.diarize.engine,
            model=cfg.diarize.model,
            meeting_duration_s=manifest.duration_s,
            wall_s=time.perf_counter() - diarize_started,
            success=not diarize_error,
            error=diarize_error,
        )
        _release_if_needed(diarizer)

        turns = merge_segments(
            segments,
            coalesce_gap_s=cfg.chunk.coalesce_gap_s,
            max_turn_s=cfg.chunk.max_turn_s,
        )
        turns = split_long_turns(turns, max_chars=cfg.chunk.max_turn_chars)

        candidates = (
            detect_names(turns, own_names=cfg.names.own_names, min_count=cfg.names.min_count)
            if cfg.names.enabled
            else []
        )

        # Which name belongs to which voice. Only ever a suggestion for the
        # dialog; nothing here labels a turn.
        guesses = (
            infer_speaker_names(
                turns, own_names=cfg.names.own_names, own_label=cfg.own_label
            )
            if cfg.names.enabled and spans
            else []
        )
        profiles = profile_speakers(turns, guesses=speaker_guess_map(guesses))

        if cfg.speakers_memory.recognise_known:
            jobs.begin_stage(cfg, job, "recognise")
            _processing_status(
                cfg,
                meeting_id,
                "Recognising known speakers",
                0,
                f"0 / {len(profiles)} voices",
                indeterminate=True,
            )
            try:
                from . import speakers as speaker_memory

                extra = speaker_memory.suggest_matches(
                    profiles,
                    speaker_memory.load_library(cfg),
                    embeddings=speaker_memory.embeddings_for_profiles(
                        cfg, meeting_id, turns, profiles, spans=spans, segments=segments
                    ),
                    min_confidence=cfg.speakers_memory.min_confidence,
                    min_margin=cfg.speakers_memory.min_margin,
                )
                named = {guess.speaker for guess in guesses}
                guesses.extend(g for g in extra if g.speaker not in named)
                jobs.finish_stage(
                    cfg, job, "recognise", detail=f"{len(extra)} suggestion(s)"
                )
            except Exception as exc:
                log.exception("speaker memory failed for meeting=%s", meeting_id)
                jobs.finish_stage(
                    cfg, job, "recognise", status=jobs.STAGE_FAILED, error=str(exc)
                )
        else:
            jobs.finish_stage(cfg, job, "recognise", status=jobs.SKIPPED)

        # Voice matches only exist after the recognise stage, which needs the
        # profiles in order to embed them. Re-stamp so the identify dialog
        # offers the same name the transcript records. Outside the try above,
        # so a failed or skipped recognise degrades to a no-op rather than
        # leaving the conversation-derived guesses unapplied.
        apply_guesses(profiles, speaker_guess_map(guesses))

        raw_turns: list = []
        if cfg.cleanup.enabled and summarizer is not None:
            jobs.begin_stage(cfg, job, "cleanup")
            _processing_status(
                cfg,
                meeting_id,
                "Cleaning transcript",
                0,
                "Proofreading ASR",
                indeterminate=True,
            )
            try:
                from . import cleanup as cleanup_mod

                notes = records.read_notes(cfg.notes_json(meeting_id))
                cleaned = cleanup_mod.cleanup_turns(
                    turns,
                    cfg.turn_labeller(records.speaker_roster(notes)),
                    summarizer,
                )
                raw_turns = list(turns)
                turns = cleaned
                jobs.finish_stage(cfg, job, "cleanup")
            except Exception as exc:
                log.warning("cleanup skipped for meeting=%s: %s", meeting_id, exc)
                jobs.finish_stage(
                    cfg, job, "cleanup", status=jobs.SKIPPED, error=str(exc)
                )
                raw_turns = []
        else:
            jobs.finish_stage(cfg, job, "cleanup", status=jobs.SKIPPED)

        summary, summary_error = "", ""
        attempt_summary = False
        if summarizer is not None and summarizer.enabled:
            if not force and not should_attempt_summary(cfg, meeting_id):
                summary_error = deferred_summary_reason(cfg)
                log.info(
                    "summary deferred for meeting=%s (%s)",
                    meeting_id,
                    summary_error,
                )
                jobs.finish_stage(
                    cfg, job, "summarize", status=jobs.SKIPPED, error=summary_error
                )
            elif not summarizer.is_running():
                summary_error = DEFERRED_SUMMARY
                log.info("ollama not running; summary deferred for meeting=%s", meeting_id)
                jobs.finish_stage(
                    cfg, job, "summarize", status=jobs.SKIPPED, error=summary_error
                )
            else:
                # Write the transcript first. If the lid closes during Ollama,
                # the meeting is already archived and the watcher retries.
                attempt_summary = True
                jobs.finish_stage(
                    cfg, job, "summarize", status=jobs.SKIPPED, error=DEFERRED_SUMMARY
                )
        else:
            jobs.finish_stage(cfg, job, "summarize", status=jobs.SKIPPED)

        _processing_status(cfg, meeting_id, "Saving transcript", 0, indeterminate=True)
        from .glossary import apply_glossary

        for segment in segments:
            segment.text = apply_glossary(segment.text, cfg.glossary.replacements)
        for turn in turns:
            turn.text = apply_glossary(turn.text, cfg.glossary.replacements)

        transcript = Transcript(
            meeting_id=meeting_id,
            segments=segments,
            turns=turns,
            detected_names=candidates,
            duration_s=manifest.duration_s,
            started_at=manifest.started_at,
            ended_at=manifest.ended_at,
            model=cfg.whisper.model,
            created_at=datetime.now().isoformat(timespec="seconds"),
            summary=summary,
            summary_model=cfg.summary.model if summary else "",
            summary_error=summary_error,
            speaker_spans=spans,
            speaker_profiles=profiles,
            speaker_guesses=guesses,
            diarize_model=cfg.diarize.model if spans else "",
            diarize_error=diarize_error,
            source=manifest.source or "native",
            whisper_engine=_whisper_engine(transcriber),
            raw_turns=raw_turns,
        )
        transcript.write(cfg.transcript_json(meeting_id))
        checkpoint_path.unlink(missing_ok=True)

        jobs.begin_stage(cfg, job, "index")
        _processing_status(
            cfg, meeting_id, "Indexing", 0, "Writing search chunks", indeterminate=True
        )
        n_chunks = ingest(cfg, meeting_id, store)
        jobs.finish_stage(cfg, job, "index", detail=f"{n_chunks} chunks")

        # Only now is it safe to move the audio out of processing: the
        # transcript is on disk and the chunks are in Chroma.
        _archive_audio(cfg, claimed)
        titled = Transcript.read(cfg.transcript_json(meeting_id))
        jobs.complete(cfg, job, title=titled.title)
        log.info(
            "processed meeting=%s turns=%d voices=%d names=%s",
            meeting_id,
            len(turns),
            len(profiles),
            attendee_suggestion(candidates) or "-",
        )
        if attempt_summary:
            try:
                apply_summary(cfg, meeting_id, summarizer, store)
                jobs.finish_stage(cfg, job, "summarize")
            except SummaryUnavailable as exc:
                log.error("summary unavailable for meeting=%s: %s", meeting_id, exc)
                jobs.finish_stage(
                    cfg, job, "summarize", status=jobs.SKIPPED, error=str(exc)
                )
            except Exception as exc:
                log.exception("summary failed for meeting=%s", meeting_id)
                jobs.finish_stage(
                    cfg, job, "summarize", status=jobs.STAGE_FAILED, error=str(exc)
                )
        _clear_processing_status(cfg, meeting_id)
        queue_mod.refresh(cfg)
        return meeting_id

    except Exception as exc:
        jobs.fail(cfg, job, str(exc), stage=job.current_stage)
        _clear_processing_status(cfg, meeting_id)
        from . import queue as queue_mod

        queue_mod.refresh(cfg)
        log.exception("processing failed for meeting=%s, moving to failed/", meeting_id)
        _move_all(claimed, cfg.failed_dir)
        raise


def _write_whisper_checkpoint(
    cfg: Config,
    meeting_id: str,
    segments: list[Segment],
    *,
    model: str,
    engine: str,
) -> Path:
    """Save Whisper output before diarize so sleep/reboot can skip re-ASR."""
    path = jobs.whisper_checkpoint_path(cfg, meeting_id)
    payload = {
        "meeting_id": meeting_id,
        "model": model,
        "whisper_engine": engine,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "segments": [segment.to_dict() for segment in segments],
    }
    records.write_json_atomic(path, payload)
    return path


def _load_whisper_segments(
    cfg: Config, meeting_id: str, checkpoint_path: Path
) -> list[Segment]:
    if checkpoint_path.exists():
        try:
            data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            return [Segment.from_dict(item) for item in data.get("segments") or []]
        except (OSError, ValueError, KeyError, TypeError):
            log.exception("could not read Whisper checkpoint for %s", meeting_id)
    transcript_path = cfg.transcript_json(meeting_id)
    if transcript_path.exists():
        try:
            return list(Transcript.read(transcript_path).segments)
        except (OSError, ValueError):
            log.exception("could not read transcript segments for %s", meeting_id)
    return []


def _diarize_channels(
    cfg: Config,
    manifest: Manifest,
    segments: list[Segment],
    diarizer: "Diarizer | None",
    *,
    status_meeting_id: str = "",
) -> tuple[list[SpeakerSpan], str]:
    """Diarize the multi-person channels and attribute segments, in place.

    Returns the spans and an error string that is empty on success.

    Diarization failing is deliberately NOT fatal, which is the one place this
    module steps away from "fail loudly and keep the audio". The reasoning: a
    transcript with no speaker labels is a mild loss, and it is still complete,
    searchable and correct. Quarantining the meeting instead would turn a
    missing local model file into a lost meeting, and Jacob would not find out
    until he went looking for it. The error is recorded in the
    transcript and logged at error level, so the degradation is visible rather
    than silent.
    """
    if diarizer is None or not diarizer.enabled:
        return [], ""

    targets = [
        (channel, filename)
        for channel, filename in sorted(manifest.channels.items())
        if diarizer.channel_enabled(channel)
    ]
    if not targets:
        return [], ""

    notes = records.read_notes(cfg.notes_json(manifest.meeting_id))
    expected_speakers = records.expected_diarized_speakers(
        notes,
        {channel for channel, _ in targets},
        cfg.names.own_names,
    )
    if expected_speakers:
        log.info(
            "meeting=%s lists %d speaker(s); clustering stays automatic",
            manifest.meeting_id,
            expected_speakers,
        )

    stop_heartbeat = threading.Event()
    owner = status_meeting_id or manifest.meeting_id

    def _beat() -> None:
        while not stop_heartbeat.wait(60.0):
            _processing_status(
                cfg,
                owner,
                "Identifying speakers",
                0,
                "Analysing speaker segments",
                indeterminate=True,
            )

    heartbeat = threading.Thread(target=_beat, name="diarize-heartbeat", daemon=True)
    heartbeat.start()

    spans: list[SpeakerSpan] = []
    try:
        for channel, filename in targets:
            spans.extend(
                diarizer.diarize_file(
                    cfg.processing_dir / filename,
                    channel,
                    expected_speakers=expected_speakers,
                )
            )
    except DiarizationUnavailable as exc:
        log.error("diarization unavailable, continuing without speakers: %s", exc)
        return [], str(exc)
    except Exception as exc:
        log.exception("diarization failed, continuing without speakers")
        return [], f"diarization failed: {exc}"
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=1.0)

    assigned = assign_speakers(segments, spans)
    log.info(
        "diarized channels=%s spans=%d segments_attributed=%d/%d",
        ",".join(c for c, _ in targets),
        len(spans),
        assigned,
        len(segments),
    )
    return spans, ""


def _claim(cfg: Config, manifest: Manifest, manifest_path: Path) -> list[Path]:
    """Move a meeting's files from inbox to processing. Returns new paths."""
    moved: list[Path] = []
    names = list(manifest.channels.values())
    if manifest.raw_file:
        names.append(manifest.raw_file)
    for name in names:
        src = cfg.inbox_dir / name
        if src.exists():
            dst = cfg.processing_dir / name
            shutil.move(str(src), str(dst))
            moved.append(dst)
    dst_manifest = cfg.processing_dir / manifest_path.name
    shutil.move(str(manifest_path), str(dst_manifest))
    moved.append(dst_manifest)
    return moved


def _archive_audio(cfg: Config, claimed: list[Path]) -> None:
    for path in claimed:
        if not path.exists():
            continue
        if path.suffix == ".json":
            path.unlink()  # manifest has served its purpose
        else:
            dst = cfg.audio_archive_dir / path.name
            shutil.move(str(path), str(dst))


def _move_all(paths: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists():
            try:
                shutil.move(str(path), str(destination / path.name))
            except OSError:
                log.exception("could not move %s to %s", path, destination)
