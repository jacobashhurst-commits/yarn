"""Ask the local Ollama model about meetings on this Mac.

Claude and ChatGPT can search the same index through the MCP server. This
path does not need them. Yarn searches the meetings, then Ollama writes the
reply. The chat window is Yarn's; Ollama is only the model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .htmltheme import document, html_escape, masthead
from .models import format_timestamp
from .store import MeetingStore
from .summarize import Summarizer

NOT_FOUND = "Nothing in your meetings matches that yet."


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


def build_prompt(question: str, hits: list[dict], *, earlier: str = "") -> str:
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
    return (
        "Answer the question using only the meeting passages below. "
        "If they do not contain the answer, say you could not find it in the meetings. "
        "Do not invent names, decisions, or quotes.\n\n"
        f"{prior}"
        f"Question:\n{question.strip()}\n\n"
        f"Passages:\n{passages}"
    )


def render_answer_html(question: str, answer: str, hits: list[dict]) -> str:
    paragraphs = [
        f"<p>{html_escape(part)}</p>"
        for part in answer.strip().split("\n\n")
        if part.strip()
    ]
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
        + '<div class="answer">'
        + "".join(paragraphs)
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
    hits = search_meetings(cfg, question)
    if not hits:
        answer = NOT_FOUND
    else:
        if complete is None:
            complete = Summarizer(cfg.summary).complete
        answer = complete(build_prompt(question, hits, earlier=earlier_chat(cfg)))
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
