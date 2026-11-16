"""Fully local meeting summaries through an already-installed Ollama model."""

from __future__ import annotations

import json
import os
import logging
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import time
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
    from yesterday is fair game the moment the laptop is awake - that is how
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


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_local_host(url: str) -> bool:
    """True when this address is a model server on this Mac."""
    try:
        name = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    return name.casefold() in _LOOPBACK_HOSTS


def remote_host_refusal(config) -> str:
    """Why this summary model address is refused, or "" when it is allowed."""
    if is_local_host(config.host) or getattr(config, "allow_remote_host", False):
        return ""
    return (
        "The summary model address is not this Mac, so nothing was sent to "
        "it. Yarn only sends a transcript to a model on this computer."
    )


class SummaryUnavailable(RuntimeError):
    pass


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def ollama_http_message(code: int, body: str) -> str:
    """Turn Ollama's HTTP body into a retryable, human line.

    A 500 that says the 9B model needs 7.1 GiB and only 4.8 are free is not a
    dead meeting. Treating it as a generic HTTP 500 made Process waiting now
    look like a no-op, and the watcher retried every 5s.
    """
    err = ""
    try:
        err = str((json.loads(body) or {}).get("error") or "").strip()
    except ValueError:
        err = body.strip()[:300]
    if err:
        return f"deferred: {err}"
    return f"deferred: local summary failed: Ollama HTTP {code}"


SUMMARY_RETRY_S = 600


def should_retry_summary(
    path: Path,
    transcript,
    *,
    force: bool = False,
    now: float | None = None,
) -> bool:
    """Avoid hammering Ollama every 5s after a memory refusal."""
    if force:
        return True
    err = str(getattr(transcript, "summary_error", "") or "")
    if not err:
        return True
    lowered = err.lower()
    if "available" not in lowered and "gib" not in lowered and "memory" not in lowered:
        return True
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return True
    return age >= SUMMARY_RETRY_S


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
        if remote_host_refusal(self.config):
            return False
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

    def summarize(self, turns: list[Turn], label_fn, language: str = "") -> str:
        if not self.enabled or not turns:
            return ""
        if not self.is_running():
            raise SummaryUnavailable(DEFERRED_SUMMARY)
        self._check_local_model()
        spoken = summary_turns(turns)
        lines = [
            f"[{format_timestamp(turn.start)}] {label_fn(turn)}: {turn.text}"
            for turn in spoken
        ]
        loose = unattributed_labels(spoken, label_fn)
        parts = _chunks(lines, self.config.chunk_chars)
        summaries = [
            self._run(
                _summary_prompt(
                    part,
                    partial=len(parts) > 1,
                    profile=self.config.profile,
                    language=language,
                    unattributed=loose,
                )
            )
            for part in parts
        ]
        if len(summaries) == 1:
            return summaries[0]
        return self._run(_combine_prompt(summaries, language=language))

    def complete(self, prompt: str) -> str:
        """One local answer. Does not start Ollama if it is closed."""
        if not self.is_running():
            raise SummaryUnavailable(
                "Ollama isn't open. Choose Open Ollama in the Yarn menu, then ask again."
            )
        return self._run(prompt)

    def _run(self, prompt: str) -> str:
        # HTTP generate, not `ollama run`. The CLI wraps the same model in an
        # extra process and on an 8 GB machine an 18-minute transcript
        # routinely burned the whole timeout with nothing written. The daemon
        # is already up - is_running() probed it.
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
            body = _http_error_body(exc)
            if exc.code in {400, 422}:
                payload.pop("think", None)
                try:
                    text = self._generate(url, payload)
                except urllib.error.HTTPError as retry_exc:
                    raise SummaryUnavailable(
                        ollama_http_message(retry_exc.code, _http_error_body(retry_exc))
                    ) from retry_exc
            else:
                raise SummaryUnavailable(ollama_http_message(exc.code, body)) from exc
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
        # Stream so each new token is written while the model is still
        # talking. The Dock reads that file once a second.
        # The last line before a transcript leaves this process. Checked
        # here as well as in is_running so no caller can skip it.
        refused = remote_host_refusal(self.config)
        if refused:
            raise SummaryUnavailable(refused)
        payload = dict(payload)
        payload["stream"] = True
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        parts: list[str] = []
        seen = {"prompt_eval_count": 0, "eval_count": 0}
        with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
            try:
                chunks = iter(response)
            except TypeError:
                chunks = [response.read()]
            for raw in chunks:
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    parts.append(str(data.get("response") or ""))
                    _note_token_delta(data, seen)
        return "".join(parts)


def saved_token_total(root: Path) -> int:
    path = root / ".tokens-saved.json"
    if not path.exists():
        return 0
    try:
        total = int(json.loads(path.read_text(encoding="utf-8")).get("total") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0
    return max(0, total)


def record_saved_tokens(count: int, *, root: Path) -> None:
    """Add tokens this Mac's model just used. The Dock calls that 'tokens saved'."""
    adjust_saved_tokens(count, root=root)


def _read_token_file(root: Path) -> dict:
    try:
        data = json.loads((root / ".tokens-saved.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_token_file(root: Path, data: dict) -> None:
    # Whole-file replace. The Dock reads this once a second while the model
    # is writing, and a half-written file read as zero tokens.
    path = root / ".tokens-saved.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _count(data: dict, key: str) -> int:
    try:
        return max(0, int(data.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def adjust_saved_tokens(delta: int, *, root: Path) -> None:
    """Move the running total by delta. A negative delta corrects an estimate."""
    if delta == 0:
        return
    data = _read_token_file(root)
    data["total"] = max(0, _count(data, "total") + delta)
    _write_token_file(root, data)


def adjust_token_split(input_delta: int, output_delta: int, *, root: Path) -> None:
    """Keep how much of the total was read and how much was written.

    Hosted models charge several times more for what they write than for
    what they read, so a cost comparison needs the two apart. The total
    stays where it always was; these sit beside it.
    """
    if input_delta == 0 and output_delta == 0:
        return
    data = _read_token_file(root)
    data["input"] = max(0, _count(data, "input") + input_delta)
    data["output"] = max(0, _count(data, "output") + output_delta)
    _write_token_file(root, data)


def saved_token_split(root: Path) -> tuple[int, int, int]:
    """(read, written, unsplit). Unsplit is the total counted before the split existed."""
    data = _read_token_file(root)
    total = _count(data, "total")
    read = min(_count(data, "input"), total)
    written = min(_count(data, "output"), total - read)
    return read, written, total - read - written


def _note_token_delta(data: dict, seen: dict) -> None:
    """Keep the Dock total moving while the model is still writing.

    Streaming chunks carry the new text and no counts. The real
    prompt_eval_count and eval_count arrive on the last line and replace
    the estimate, so the final number is Ollama's, not a guess.
    """
    for key in ("prompt_eval_count", "eval_count"):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > int(seen.get(key) or 0):
            seen[key] = value
    official = int(seen.get("prompt_eval_count") or 0) + int(seen.get("eval_count") or 0)
    text = str(data.get("response") or "")
    if official:
        desired = official
    elif text and "done" in data:
        seen["estimate"] = int(seen.get("estimate") or 0) + max(1, (len(text) + 3) // 4)
        desired = int(seen["estimate"])
    else:
        return
    written = int(seen.get("written") or 0)
    delta = desired - written
    # Until Ollama's own counts arrive, everything seen is text it wrote.
    if official:
        want_in = int(seen.get("prompt_eval_count") or 0)
        want_out = int(seen.get("eval_count") or 0)
    else:
        want_in, want_out = 0, desired
    delta_in = want_in - int(seen.get("written_in") or 0)
    delta_out = want_out - int(seen.get("written_out") or 0)
    if delta == 0 and delta_in == 0 and delta_out == 0:
        return
    seen["written"] = desired
    seen["written_in"] = want_in
    seen["written_out"] = want_out
    try:
        from .config import load_config

        root = load_config().root
        if delta:
            adjust_saved_tokens(delta, root=root)
        adjust_token_split(delta_in, delta_out, root=root)
    except (OSError, ValueError, TypeError):
        log.debug("could not record saved tokens", exc_info=True)


def summary_turns(turns: list[Turn]) -> list[Turn]:
    """The turns worth showing a summary model.

    A line that is only "yeah" or "mm, okay" says nothing, and a model told
    to report what each person said will still try: "Speaker 3 agreed."
    Those lines stay in the transcript and are left out of the prompt. If
    that would leave nothing, the meeting really was that short, and all of
    it is used.
    """
    from .merge import is_backchannel

    kept = [
        turn
        for turn in turns
        # No duration limit here: a drawn-out "yeeeah, okay" is still filler.
        if not is_backchannel(turn.text, 0.0, 1.0)
    ]
    return kept or list(turns)


def unattributed_labels(turns: list[Turn], label_fn) -> list[str]:
    """Labels on lines whose speaker could not be told apart.

    Only on a channel where other voices were separated. There, a line with
    no speaker is a brief remark that matched nobody. On a channel that was
    never separated the same label is simply everyone, and is left alone.
    """
    separated = {turn.channel for turn in turns if turn.speaker}
    labels: list[str] = []
    for turn in turns:
        if turn.speaker or turn.channel not in separated or turn.channel == "mic":
            continue
        label = str(label_fn(turn))
        if label not in labels:
            labels.append(label)
    return labels


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


def _summary_prompt(
    transcript: str,
    partial: bool,
    profile: str = "default",
    language: str = "",
    unattributed: list[str] | None = None,
) -> str:
    from .languages import summary_instruction

    scope = "This is one part of a longer transcript." if partial else ""
    extra = _PROFILE_HINTS.get(profile, _PROFILE_HINTS["default"])
    spoken = summary_instruction(language)
    spoken_line = f"{spoken}\n" if spoken else ""
    loose_line = ""
    if unattributed:
        quoted = " or ".join(f'"{label}"' for label in unattributed)
        loose_line = (
            f"\nA line labelled {quoted} is a brief remark whose speaker "
            "could not be told apart. It is not a participant. Do not count "
            "it as a person, name it, or report what it said as someone's "
            "view. Use it only if it carries a fact.\n"
        )
    return f"""You summarize private meeting transcripts. {scope}
Use only facts explicitly present below. Do not infer identities, motives,
decisions, or action items. If speech is unclear, omit it.

Each label before a colon is a person who spoke. When more than one person
speaks, the overview and the key points must say what each of them said.
The person on the microphone and a person heard from the computer are
different people. Do not leave either of them out, and do not turn one
person's words into a description of the meeting instead of something they said.
{loose_line}
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
{spoken_line}Return Markdown with:
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


def _combine_prompt(summaries: list[str], language: str = "") -> str:
    from .languages import summary_instruction

    joined = "\n\n--- PART ---\n\n".join(summaries)
    spoken = summary_instruction(language)
    spoken_line = f"{spoken}\n" if spoken else ""
    return f"""Combine these partial meeting summaries into one concise Markdown
summary. Remove repetition. Preserve only facts present in the partial
summaries. Include a two-sentence overview, **Key points**, and include
**Decisions** or **Action items** only when explicitly present.
{spoken_line}
{joined}
"""
