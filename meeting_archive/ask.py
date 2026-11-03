"""Ask the local Ollama model about meetings on this Mac.

Claude and ChatGPT can search the same index through the MCP server. This
path does not need them. Yarn searches the meetings, then Ollama writes the
reply. The chat window is Yarn's; Ollama is only the model.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .htmltheme import document, html_escape, masthead
from .models import format_timestamp
from .store import MeetingStore
from .summarize import Summarizer

NOT_FOUND = "Nothing in your meetings matches that yet."
THREAD_MAX = 2000

# "Write me a summary email" is a drafting task. The passages are the source,
# not a place to go looking for an email that was already sent.
_DRAFT = re.compile(
    r"\b(write|draft|compose|email|e-mail|summar(?:y|ise|ize)|recap)\b",
    re.IGNORECASE,
)
_LATEST = re.compile(
    r"\b(?:my\s+)?(?:last|latest|most recent|previous)\s+meeting\b",
    re.IGNORECASE,
)
_TODAY = re.compile(
    r"\b(?:today|todays|today's|this morning|tonight)\b",
    re.IGNORECASE,
)
_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)
# Words that appear in almost every ask, so they must not pick a meeting.
_MEETING_STOP = frozenset(
    """
    a an the my our your me we i you write draft compose email emails e-mail
    summary summarise summarize recap review meeting meetings last latest
    recent previous today todays tonight morning yesterday from for with about
    just then give want need please that this them they what when where how
    into over after before again
    """.split()
)


class AskError(RuntimeError):
    pass


@dataclass
class AskResult:
    question: str
    answer: str
    path: Path


def search_meetings(cfg: Config, question: str, *, limit: int = 6) -> list[dict]:
    store = MeetingStore(cfg.chroma_dir, cfg.collection)
    try:
        return store.search(question, n_results=limit)
    except Exception as exc:
        raise AskError(
            "Yarn couldn't search your meetings. Record one first, then ask again."
        ) from exc


def chat_path(cfg: Config) -> Path:
    return cfg.root / "asks" / "chat.json"


def _turns(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def load_store(cfg: Config) -> dict:
    """Chats grouped the way the window lists them. A bare turn list is one chat."""
    path = chat_path(cfg)
    if not path.exists():
        return {"active": "", "chats": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": "", "chats": []}
    if isinstance(loaded, list):
        return {
            "active": "legacy",
            "chats": [{"id": "legacy", "updated": 0, "turns": _turns(loaded)}],
        }
    if not isinstance(loaded, dict):
        return {"active": "", "chats": []}
    chats = []
    for item in loaded.get("chats") or []:
        if not isinstance(item, dict):
            continue
        chats.append(
            {
                "id": str(item.get("id") or ""),
                "updated": int(item.get("updated") or 0),
                "turns": _turns(item.get("turns")),
            }
        )
    active = str(loaded.get("active") or "")
    if not active and chats:
        active = str(chats[-1]["id"])
    return {"active": active, "chats": chats}


def save_store(cfg: Config, store: dict) -> None:
    path = chat_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")


def chat_log(cfg: Config) -> list[dict]:
    store = load_store(cfg)
    for chat in store["chats"]:
        if chat["id"] == store["active"]:
            return chat["turns"]
    return []


def remember_exchange(cfg: Config, question: str, answer: str) -> None:
    store = load_store(cfg)
    now = int(time.time())
    target = next((chat for chat in store["chats"] if chat["id"] == store["active"]), None)
    if target is None:
        target = {"id": "chat", "updated": now, "turns": []}
        store["chats"].append(target)
        store["active"] = target["id"]
    target["turns"] = (
        target["turns"]
        + [{"role": "you", "text": question}, {"role": "yarn", "text": answer}]
    )[-40:]
    target["updated"] = now
    save_store(cfg, store)


def earlier_chat(cfg: Config) -> str:
    lines = []
    for item in chat_log(cfg)[-6:]:
        who = "You" if item.get("role") == "you" else "Yarn"
        text = " ".join(str(item.get("text") or "").split())
        if len(text) > 400:
            text = text[:397] + "..."
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def wants_latest_meeting(question: str) -> bool:
    return bool(_LATEST.search(question or ""))


def wants_draft(question: str) -> bool:
    return bool(_DRAFT.search(question or ""))


def _meeting_tokens(text: str) -> list[str]:
    return [
        word
        for word in re.findall(r"[a-z0-9]{4,}", (text or "").lower())
        if word not in _MEETING_STOP
    ]


def _note_refs(cfg: Config) -> list[dict]:
    """Small notes files: attendees and the title Jacob typed. Not the transcript."""
    folder = cfg.notes_json("x").parent
    refs: list[dict] = []
    if not folder.is_dir():
        return refs
    for path in folder.glob("*.json"):
        if path.name.count(".") > 1:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        meeting_id = str(data.get("meeting_id") or path.stem)
        haystack = " ".join(
            str(data.get(key) or "") for key in ("attendees", "notes")
        )
        refs.append({"id": meeting_id, "haystack": haystack.lower()})
    return refs


def _user_context(cfg: Config) -> str:
    """What was asked in this chat, without Yarn's own earlier replies."""
    lines = []
    for item in chat_log(cfg)[-8:]:
        if item.get("role") != "you":
            continue
        text = " ".join(str(item.get("text") or "").split())
        if text:
            lines.append(text)
    return "\n".join(lines)


def resolve_meeting_ids(cfg: Config, text: str) -> list[str]:
    """Meetings this question is about, newest first. Empty means search instead.

    "Write the email" does not name a meeting, but the earlier question in the
    same chat often does ("the Heidi Health one from today"). A keyword search
    then retrieves old meetings that merely mention email.
    """
    blob = text or ""
    day = ""
    if _TODAY.search(blob):
        day = date.today().isoformat()
    elif _YESTERDAY.search(blob):
        day = (date.today() - timedelta(days=1)).isoformat()
    tokens = _meeting_tokens(blob)
    scored: list[tuple[int, str]] = []
    for ref in _note_refs(cfg):
        score = sum(1 for token in tokens if token in ref["haystack"])
        if score:
            scored.append((score, ref["id"]))
    if day and scored:
        on_day = [item for item in scored if item[1].startswith(day)]
        if on_day:
            scored = on_day
    if scored:
        best = max(score for score, _meeting_id in scored)
        found = [meeting_id for score, meeting_id in scored if score == best]
        return sorted(found, reverse=True)[:2]
    if day:
        folder = cfg.transcripts_dir / day[:4] / day[5:7]
        if folder.is_dir():
            ids = sorted(path.stem for path in folder.glob(f"{day}T*.json"))
            if len(ids) == 1:
                return ids
            if ids and (_LATEST.search(blob) or _TODAY.search(blob)):
                return [ids[-1]]
    if _LATEST.search(blob):
        latest = latest_meeting_id(cfg)
        return [latest] if latest else []
    return []


def latest_meeting_id(cfg: Config) -> str:
    """Newest transcript on disk. Meeting ids sort in time order."""
    paths = [
        path
        for path in cfg.transcripts_dir.rglob("*.json")
        if path.is_file()
    ]
    if not paths:
        return ""
    return max(paths, key=lambda path: path.stem).stem


def passages_for_meeting(cfg: Config, meeting_id: str, *, limit_chars: int = 20000) -> list[dict]:
    """One passage built from a meeting's own summary and transcript."""
    from .records import Transcript, read_notes, speaker_roster

    path = cfg.transcript_json(meeting_id)
    if not path.exists():
        return []
    transcript = Transcript.read(path)
    notes = read_notes(cfg.notes_json(meeting_id))
    roster = speaker_roster(notes)
    from .models import speaker_display_indices

    display = speaker_display_indices(transcript.speaker_profiles)
    label = cfg.turn_labeller(roster, display)
    lines = []
    for turn in transcript.turns:
        lines.append(f"[{format_timestamp(turn.start)}] {label(turn)}: {turn.text}")
    summary = ""
    if transcript.summary.strip():
        summary = transcript.summary.strip()
    body = "\n".join(lines).strip()
    if summary and len(summary) + len(body) + 2 > limit_chars:
        budget = max(0, limit_chars - len(summary) - 20)
        half = budget // 2
        if half > 200 and len(body) > budget:
            body = body[:half].rstrip() + "\n…\n" + body[-half:].lstrip()
        else:
            body = body[:budget].rstrip()
    text = f"{summary}\n\n{body}".strip() if summary else body
    if len(text) > limit_chars:
        text = text[: limit_chars - 1].rstrip() + "…"
    if not text:
        return []
    when = ""
    try:
        from .records import parse_meeting_id

        when = parse_meeting_id(meeting_id).strftime("%a %-d %b %Y %H:%M")
    except (ValueError, OSError):
        when = meeting_id
    title = transcript.title or meeting_id
    attendees = str(notes.get("attendees") or "")
    return [
        {
            "document": f"{title}\n{text}",
            "metadata": {
                "meeting_id": meeting_id,
                "date_label": when,
                "speakers": attendees,
                "title": title,
            },
        }
    ]


def user_thread(cfg: Config) -> str:
    """Who is asking, written in Settings. Empty until they fill it in."""
    path = cfg.root / ".yarn-settings.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    text = str(data.get("thread") or "").strip()
    return text[:THREAD_MAX].strip()


def build_prompt(
    question: str,
    hits: list[dict],
    *,
    earlier: str = "",
    about: str = "",
) -> str:
    blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        meta = hit.get("metadata") or {}
        when = str(meta.get("date_label") or meta.get("meeting_id") or "a meeting")
        start = meta.get("start_s")
        if start not in (None, ""):
            when = f"{when} at {format_timestamp(float(start))}"
        speakers = str(meta.get("speakers") or "").strip()
        who = f" ({speakers})" if speakers else ""
        text = str(hit.get("document") or "").strip()
        blocks.append(f"{index}. {when}{who}\n{text}")
    passages = "\n\n".join(blocks)
    prior = ""
    if earlier.strip():
        prior = f"Earlier in this chat:\n{earlier.strip()}\n\n"
    person = ""
    note = about.strip()
    if note:
        person = (
            "About the person asking. Use this for their name and what they "
            "care about. It is not a meeting, and it does not add facts.\n"
            f"{note}\n\n"
        )
    if wants_draft(question):
        instruction = (
            "The user wants you to write something from these meetings, such as "
            "an email or a summary. Compose it yourself from the passages. "
            "When they asked for an email, write the email ready to send: "
            "a Subject line, a short greeting, what was decided, the next steps, "
            "and a closing. Use the attendees' names from the passage header, "
            "and attribute a point to a person only when that person's turns say it. "
            "If a summary and the spoken turns disagree, follow the turns. "
            "Use only facts that are actually in the passages. "
            "Do not say you could not find an email or a summary already written "
            "in the meetings. If the passages do not contain enough to draft it, "
            "say what is missing. Do not invent names, decisions, or quotes. "
            "Answer in the language of the question. Keep quotes in the language that was spoken."
        )
    else:
        instruction = (
            "Answer the question using only the meeting passages below. "
            "When a passage has both a summary and the spoken turns, and they disagree, follow the turns. "
            "If they do not contain the answer, say you could not find it in the meetings. "
            "Do not invent names, decisions, or quotes. "
            "Answer in the language of the question. Keep quotes in the language that was spoken."
        )
    return (
        f"{instruction}\n\n"
        f"{person}"
        f"{prior}"
        f"Question:\n{question.strip()}\n\n"
        f"Passages:\n{passages}"
    )


def render_answer_html(question: str, answer: str, hits: list[dict]) -> str:
    from .markup import markup_to_html

    paragraphs = markup_to_html(answer)
    sources: list[str] = []
    for hit in hits:
        meta = hit.get("metadata") or {}
        label = str(meta.get("date_label") or meta.get("meeting_id") or "Meeting")
        snippet = " ".join(str(hit.get("document") or "").split())
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        sources.append(
            "<li>"
            f"<strong>{html_escape(label)}</strong>"
            f"<span>{html_escape(snippet)}</span>"
            "</li>"
        )
    source_block = ""
    if sources:
        source_block = (
            '<section class="sources"><h2>From your meetings</h2><ul>'
            + "".join(sources)
            + "</ul></section>"
        )
    body = (
        masthead("On this Mac", question.strip())
        + '<div class="answer prose">'
        + paragraphs
        + "</div>"
        + source_block
    )
    return document("Ask Yarn", body)


def write_answer(cfg: Config, question: str, answer: str, hits: list[dict]) -> Path:
    folder = cfg.root / "asks"
    folder.mkdir(parents=True, exist_ok=True)
    html = render_answer_html(question, answer, hits)
    path = folder / "latest.html"
    path.write_text(html, encoding="utf-8")
    return path


def ask(cfg: Config, question: str, *, complete=None, remember: bool = True) -> AskResult:
    question = question.strip()
    if not question:
        raise AskError("Type a question first.")
    context = f"{question}\n{_user_context(cfg)}"
    meeting_ids = resolve_meeting_ids(cfg, context)
    if meeting_ids:
        hits = []
        for meeting_id in meeting_ids:
            hits.extend(passages_for_meeting(cfg, meeting_id))
    else:
        hits = search_meetings(cfg, question)
    if not hits:
        answer = NOT_FOUND
    else:
        if complete is None:
            complete = Summarizer(cfg.summary).complete
        answer = complete(
            build_prompt(
                question,
                hits,
                earlier=earlier_chat(cfg),
                about=user_thread(cfg),
            )
        )
    path = write_answer(cfg, question, answer, hits)
    # The Dock chat stores the thread itself. Remembering here as well
    # would write the same question twice.
    if remember:
        remember_exchange(cfg, question, answer)
    return AskResult(question=question, answer=answer, path=path)


def request_chat(cfg: Config) -> None:
    """Ask the Dock app to open its chat. No system dialog."""
    folder = cfg.root / "asks"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".open-chat").write_text("1\n", encoding="utf-8")
    import subprocess

    subprocess.run(
        ["/usr/bin/open", "-a", "Yarn"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def open_answer(path: Path) -> None:
    import subprocess
    subprocess.run(
        ["/usr/bin/open", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
