"""Launch the optional dialogs.

Both popups are fire-and-forget child processes. Nothing in the pipeline ever
waits on one, and a popup that is dismissed, ignored, or never appears at all
has no effect on the recording, the transcript or the index. That is the
constraint that matters most in this whole build, so it is enforced here by
construction rather than by remembering to be careful.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def popup_script() -> Path:
    """Path to bin/meeting-popup, relative to this checkout."""
    return _bin("meeting-popup")


def speaker_popup_script() -> Path:
    """Path to bin/speaker-popup, relative to this checkout."""
    return _bin("speaker-popup")


def _bin(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "bin" / name


def launch(meeting_id: str, prefill_attendees: str = "") -> bool:
    """Spawn the popup and return immediately. Never raises."""
    script = popup_script()
    if sys.platform != "darwin":
        log.info("not macOS, skipping popup for meeting=%s", meeting_id)
        return False
    if not script.exists():
        log.warning("popup script missing at %s", script)
        return False

    cmd = [str(script), meeting_id]
    if prefill_attendees:
        cmd += ["--attendees", prefill_attendees]
    return _spawn(cmd, meeting_id, "notes popup")


def launch_speakers(meeting_id: str, profiles=None) -> bool:
    """Spawn the who-was-this dialogs and return immediately. Never raises.

    `profiles` is accepted for the caller's convenience and deliberately not
    passed along: the script asks `ma speakers` for the current pending list
    itself. That keeps the quips and the roster out of argv -- a quip is a
    verbatim sentence from a private meeting, and argv is world-readable in
    `ps` output.
    """
    script = speaker_popup_script()
    if sys.platform != "darwin":
        log.info("not macOS, skipping speaker popup for meeting=%s", meeting_id)
        return False
    if not script.exists():
        log.warning("speaker popup script missing at %s", script)
        return False
    return _spawn([str(script), meeting_id], meeting_id, "speaker popup")


def _spawn(cmd: list[str], meeting_id: str, what: str) -> bool:
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("launched %s for meeting=%s", what, meeting_id)
        return True
    except OSError:
        log.exception("could not launch %s for meeting=%s", what, meeting_id)
        return False
