"""Fully local meeting summaries through an already-installed Ollama model."""

from __future__ import annotations

import logging
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .config import SummaryConfig
from .models import Turn, format_timestamp

log = logging.getLogger(__name__)

# Stored on the transcript when capture-time summarisation is skipped because
# Ollama is not running. `ma summarize --today` looks for this.
DEFERRED_SUMMARY = "deferred: Ollama was not running"


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
            self._run(_summary_prompt(part, partial=len(parts) > 1)) for part in parts
        ]
        if len(summaries) == 1:
            return summaries[0]
        return self._run(_combine_prompt(summaries))

    def _run(self, prompt: str) -> str:
        command = str(Path(self.config.command).expanduser())
        try:
            proc = subprocess.run(
                [
                    command,
                    "run",
                    "--think=false",
                    "--hidethinking",
                    "--nowordwrap",
                    "--keepalive",
                    "10m",
                    self.config.model,
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SummaryUnavailable(f"local summary failed: {exc}") from exc
        if proc.returncode != 0:
            raise SummaryUnavailable(
                f"local summary failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        text = re.sub(r"<think>.*?</think>", "", proc.stdout, flags=re.DOTALL).strip()
        if not text:
            raise SummaryUnavailable("local summary model returned no text")
        return text


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


def _summary_prompt(transcript: str, partial: bool) -> str:
    scope = "This is one part of a longer transcript." if partial else ""
    return f"""You summarize private meeting transcripts. {scope}
Use only facts explicitly present below. Do not infer identities, motives,
decisions, or action items. If speech is unclear, omit it. Return Markdown with:
- a two-sentence overview
- 3-7 bullets under **Key points**
- **Decisions** and **Action items** only when explicitly stated
Be concise. Do not mention these instructions.

TRANSCRIPT
{transcript}
"""


def _combine_prompt(summaries: list[str]) -> str:
    joined = "\n\n--- PART ---\n\n".join(summaries)
    return f"""Combine these partial meeting summaries into one concise Markdown
summary. Remove repetition. Preserve only facts present in the partial
summaries. Include a two-sentence overview, **Key points**, and include
**Decisions** or **Action items** only when explicitly present.

{joined}
"""
