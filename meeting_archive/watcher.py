"""The long-running watcher.

Polling, not filesystem events. A folder that receives a file a handful of
times a day does not need FSEvents, and polling has no failure mode where a
dropped event silently costs a meeting. The whole loop is a sorted directory
listing every few seconds.

Two things are watched:

  inbox/   manifests from `ma finalize` -> transcribe and index
  notes/   notes files with applied=false -> reindex that meeting only

The notes pass is what makes the popup non-blocking. Processing starts the
moment recording stops, and if Jacob types his notes ten minutes later the
meeting is simply reindexed with them. If he dismisses the popup, nothing
happens at all.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from . import pipeline, popup, records
from .config import Config
from .diarize import Diarizer
from .names import attendee_suggestion
from .records import Transcript
from .store import MeetingStore
from .summarize import Summarizer
from .transcribe import Transcriber

log = logging.getLogger(__name__)


def run(cfg: Config, interval: float = 5.0, once: bool = False) -> None:
    cfg.ensure_dirs()
    transcriber = Transcriber(cfg.whisper)
    diarizer = Diarizer(cfg.diarize)
    summarizer = Summarizer(cfg.summary)
    store = MeetingStore(cfg.chroma_dir, cfg.collection)

    log.info("watching %s (interval=%.0fs)", cfg.inbox_dir, interval)
    while True:
        try:
            handled = sweep_inbox(cfg, transcriber, store, diarizer, summarizer)
            handled += sweep_notes(cfg, store)
        except Exception:  # keep the watcher alive across any single failure
            log.exception("sweep failed")
            handled = 0

        if once:
            return
        # Sleep less when there was work, in case several meetings queued up.
        time.sleep(0.5 if handled else interval)


def sweep_inbox(
    cfg: Config,
    transcriber: Transcriber,
    store: MeetingStore,
    diarizer: "Diarizer | None" = None,
    summarizer: "Summarizer | None" = None,
) -> int:
    handled = 0
    for manifest_path in sorted(cfg.inbox_dir.glob("*.json")):
        if manifest_path.name.endswith(".partial"):
            continue
        try:
            meeting_id = pipeline.process(
                cfg, manifest_path, transcriber, store, diarizer, summarizer
            )
        except Exception:
            # process() has already logged and quarantined the files.
            continue
        handled += 1
        # Identification first: naming a voice rewrites every label in the
        # transcript, where the attendee list only adds a header line.
        _maybe_identify_speakers(cfg, meeting_id)
        _maybe_suggest_names(cfg, meeting_id)
    return handled


def sweep_notes(cfg: Config, store: MeetingStore) -> int:
    """Reindex meetings whose notes changed since they were last indexed."""
    handled = 0
    for notes_path in sorted(cfg.notes_dir.glob("*.json")):
        if notes_path.name.endswith(".partial"):
            continue
        data = records.read_notes(notes_path)
        if not data or data.get("applied", True):
            continue

        meeting_id = data.get("meeting_id") or notes_path.stem
        if not cfg.transcript_json(meeting_id).exists():
            # Notes arrived before transcription finished. Leave them; ingest
            # will pick them up on its own and mark them applied.
            continue

        try:
            pipeline.ingest(cfg, meeting_id, store)
            log.info("reindexed meeting=%s with notes", meeting_id)
            handled += 1
        except Exception:
            log.exception("could not apply notes for meeting=%s", meeting_id)
    return handled


def _maybe_identify_speakers(cfg: Config, meeting_id: str) -> None:
    """Ask who each distinguished voice was, once there is a transcript.

    This dialog cannot appear when recording stops, which is where Jacob asked
    for it: at that moment nothing has been transcribed, so there are no voices
    to ask about and nothing to quote back at him. It appears when the
    transcript lands instead, which is also the only ordering the non-blocking
    rule allows -- the audio and the transcript are both already safe, and
    dismissing this changes nothing except that the labels stay "Speaker N".
    """
    if not (cfg.diarize.enabled and cfg.diarize.identify_popup):
        return

    try:
        transcript = Transcript.read(cfg.transcript_json(meeting_id))
    except (OSError, ValueError):
        return

    if not transcript.speaker_profiles:
        return

    roster = records.speaker_roster(records.read_notes(cfg.notes_json(meeting_id)))
    pending = transcript.unnamed_speakers(roster)
    if not pending:
        return  # every voice already has a name; don't ask twice

    # There is deliberately no numerical ceiling. Real meetings commonly have
    # 6-8 speakers and can have more; suppressing every dialog above six made
    # the identity feature disappear exactly when it was most useful. The
    # first dialog still has "Not now", which cancels the remaining sequence.
    popup.launch_speakers(meeting_id, pending)


def _maybe_suggest_names(cfg: Config, meeting_id: str) -> None:
    """Offer detected names if the on-stop popup left the attendee box empty.

    This is the one place the name detection becomes visible. It is a second,
    equally dismissible dialog -- and it only appears when it would actually
    tell Jacob something he hasn't already typed.
    """
    if not (cfg.names.enabled and cfg.names.suggest_popup):
        return

    notes = records.read_notes(cfg.notes_json(meeting_id))
    if notes.get("attendees"):
        return  # he already told us; don't second-guess him

    try:
        transcript = Transcript.read(cfg.transcript_json(meeting_id))
    except (OSError, ValueError):
        return

    suggestion = attendee_suggestion(transcript.detected_names)
    if not suggestion:
        return

    popup.launch(meeting_id, prefill_attendees=suggestion)
