"""Fully local meeting summaries through an already-installed Ollama model."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import SummaryConfig
from .models import Turn, format_timestamp

log = logging.getLogger(__name__)

# Stored on the transcript when capture-time summarisation is skipped because
# Ollama is not running. `ma summarize --today` looks for this.
DEFERRED_SUMMARY = "deferred: Ollama was not running"
DEFERRED_TIMEOUT = (
    "deferred: local summary timed out; use Summarise pending once Ollama is free"
)
DEFERRED_EVENING = "deferred: waiting until evening"
DEFERRED_MANUAL = "deferred: summaries are manual"

WHEN_AFTER = "after_meeting"
WHEN_EVENING = "evening"
WHEN_MANUAL = "manual"


def is_pending_summary(message: str) -> bool:
    """True when the meeting is fine and the summary can be retried later."""
    text = (message or "").strip().lower()
    return text.startswith("deferred:") or "timed out" in text


def deferred_summary_reason(cfg) -> str:
    when = str(getattr(cfg.summary, "when", "") or WHEN_AFTER)
    if when == WHEN_MANUAL:
        return DEFERRED_MANUAL
    if when == WHEN_EVENING:
        return DEFERRED_EVENING
    return DEFERRED_SUMMARY


def should_attempt_summary(cfg, meeting_id: str = "", now: datetime | None = None) -> bool:
    """Whether the watcher may spend Whisper or Ollama time right now.

    Manual never auto-runs. Evening waits until evening_hour, but a leftover
    from yesterday is fair game the moment the laptop is awake — that is how
    a closed lid overnight still gets processed.
    """
    when = str(getattr(cfg.summary, "when", "") or WHEN_AFTER)
    if when == WHEN_MANUAL:
        return False
    if when != WHEN_EVENING:
        return True
    now = now or datetime.now()
    try:
        hour = int(getattr(cfg.summary, "evening_hour", 18) or 18)
    except (TypeError, ValueError):
        hour = 18
    hour = min(23, max(0, hour))
    if now.hour >= hour:
        return True
    if meeting_id:
        try:
            from .records import parse_meeting_id

            started = parse_meeting_id(meeting_id)
            if started.date() < now.date():
                return True
        except ValueError:
            return True
    return False


class SummaryUnavailable(RuntimeError):
    pass


class Summarizer:
    def __init__(self, config: SummaryConfig):
        self.config = config
        self._checked = False

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def is_running(self) -> bool:
        """True only if an Ollama server is already answering.

        Must not start the daemon. `ollama list` and `ollama run` will, which
        is exactly what we are trying to avoid by keeping it off during the day.
        """
        url = self.config.host.rstrip("/") + "/api/tags"
        try:
            urllib.request.urlopen(url, timeout=0.4)
            return True
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            return False

    def _check_local_model(self) -> None:
        if self._checked:
            return
        command = Path(self.config.command).expanduser()
        if not command.is_file():
            raise SummaryUnavailable(
                f"Ollama command not found at {command}; summary was skipped"
            )
        proc = subprocess.run(
            [str(command), "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise SummaryUnavailable(
                f"Ollama is unavailable: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        installed = {
            line.split()[0] for line in proc.stdout.splitlines()[1:] if line.split()
        }
        if self.config.model not in installed:
            raise SummaryUnavailable(
                f"local Ollama model {self.config.model!r} is not installed; "
                "no model was downloaded automatically"
            )
        self._checked = True

    def summarize(self, turns: list[Turn], label_fn) -> str:
        if not self.enabled or not turns:
            return ""
        if not self.is_running():
            raise SummaryUnavailable(DEFERRED_SUMMARY)
        self._check_local_model()
        lines = [
            f"[{format_timestamp(turn.start)}] {label_fn(turn)}: {turn.text}"
            for turn in turns
        ]
        parts = _chunks(lines, self.config.chunk_chars)
        summaries = [
            self._run(_summary_prompt(part, partial=len(parts) > 1, profile=self.config.profile))
            for part in parts
        ]
        if len(summaries) == 1:
            return summaries[0]
        return self._run(_combine_prompt(summaries))

    def _run(self, prompt: str) -> str:
        # HTTP generate, not `ollama run`. The CLI wraps the same model in an
        # extra process and on an 8 GB machine an 18-minute transcript
        # routinely burned the whole timeout with nothing written. The daemon
        # is already up — is_running() probed it.
        url = self.config.host.rstrip("/") + "/api/generate"
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {"temperature": 0.1},
        }
        try:
            text = self._generate(url, payload)
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 422}:
                payload.pop("think", None)
                text = self._generate(url, payload)
            else:
                raise SummaryUnavailable(
                    f"local summary failed: Ollama HTTP {exc.code}"
                ) from exc
        except TimeoutError as exc:
            raise SummaryUnavailable(DEFERRED_TIMEOUT) from exc
        except urllib.error.URLError as exc:
            reason = str(exc.reason if getattr(exc, "reason", None) else exc)
            if "timed out" in reason.lower():
                raise SummaryUnavailable(DEFERRED_TIMEOUT) from exc
            raise SummaryUnavailable(f"local summary failed: {reason}") from exc
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if not text:
            raise SummaryUnavailable("local summary model returned no text")
        return text

    def _generate(self, url: str, payload: dict) -> str:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str(data.get("response") or "")


def _chunks(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _summary_prompt(transcript: str, partial: bool, profile: str = "default") -> str:
    scope = "This is one part of a longer transcript." if partial else ""
    extra = _PROFILE_HINTS.get(profile, _PROFILE_HINTS["default"])
    return f"""You summarize private meeting transcripts. {scope}
Use only facts explicitly present below. Do not infer identities, motives,
decisions, or action items. If speech is unclear, omit it.

Distinguish, and never mix:
- Decision (agreed in the meeting)
- Suggestion (proposed, not agreed)
- Open question (unresolved)
- Action (commitment with an owner and/or deadline)

A strong default summary preserves, when present: purpose, customer context,
current state, pain points, requirements, decisions, objections, commitments,
action items with owners and deadlines, unresolved questions, product or
technical details, commercial details, and follow-up items.
Do not invent details. Preserve disagreement and hedging.
{extra}
Return Markdown with:
- a two-sentence overview
- 3-7 bullets under **Key points**
- **Decisions**, **Open questions**, and **Action items** only when explicitly stated
Be concise. Do not mention these instructions.

TRANSCRIPT
{transcript}
"""


_PROFILE_HINTS = {
    "default": "",
    "sales": (
        "Bias the key points toward customer context, pain, requirements, "
        "objections, and next commercial steps. Still invent nothing."
    ),
    "technical": (
        "Bias the key points toward architecture, constraints, and technical "
        "decisions. Still invent nothing."
    ),
    "short": "Keep the whole summary under 12 lines.",
    "actions": "If there are no explicit actions, say so and keep everything else brief.",
    "detailed": "Prefer completeness over brevity when facts are present.",
}


def _combine_prompt(summaries: list[str]) -> str:
    joined = "\n\n--- PART ---\n\n".join(summaries)
    return f"""Combine these partial meeting summaries into one concise Markdown
summary. Remove repetition. Preserve only facts present in the partial
summaries. Include a two-sentence overview, **Key points**, and include
**Decisions** or **Action items** only when explicitly present.

{joined}
"""
