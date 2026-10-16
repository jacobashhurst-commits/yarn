"""Ask Yarn answers from the local index and the local model."""

import pytest

from meeting_archive.ask import (
    NOT_FOUND,
    ask,
    build_prompt,
    chat_log,
    render_answer_html,
    request_chat,
)
from meeting_archive.config import Config


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(root=tmp_path / "archive")
    config.ensure_dirs()
    return config


def test_draft_prompt_writes_the_email_from_the_passages():
    prompt = build_prompt(
        "Write me a summary email for my last meeting",
        [{"document": "We agreed Friday.", "metadata": {"date_label": "16 Oct"}}],
    )
    assert "Compose it yourself" in prompt
    assert "Subject line" in prompt
    assert "could not find an email" in prompt
    assert "We agreed Friday." in prompt


def test_last_meeting_uses_that_transcript_not_a_keyword_search(cfg: Config, monkeypatch):
    from meeting_archive.models import Turn
    from meeting_archive.records import Transcript

    meeting_id = "2026-10-16T09-30-43"
    transcript = Transcript(
        meeting_id=meeting_id,
        summary="Jacob and Mariko planned the discovery call.",
        turns=[Turn("room", 0.0, 5.0, "ask about revenue at the end", speaker="room-1")],
        title="Heidi Health prep",
    )
    path = cfg.transcript_json(meeting_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript.write(path)
    called: list[str] = []
    monkeypatch.setattr(
        "meeting_archive.ask.search_meetings",
        lambda *_args, **_kwargs: called.append("search") or [],
    )
    seen: list[str] = []

    def complete(prompt: str) -> str:
        seen.append(prompt)
        return "Draft."

    ask(cfg, "Write me a summary email for my last meeting", complete=complete, remember=False)
    assert called == []
    assert "Mariko" in seen[0]
    assert "Compose it yourself" in seen[0]


def test_prompt_stays_inside_the_passages():
    prompt = build_prompt(
        "what did we decide",
        [
            {
                "document": "We will ship Friday.",
                "metadata": {"date_label": "15 Oct", "speakers": "Mina"},
            }
        ],
    )
    assert "what did we decide" in prompt
    assert "ship Friday" in prompt
    assert "Do not invent" in prompt


def test_empty_archive_does_not_call_the_model(cfg: Config, monkeypatch):
    monkeypatch.setattr("meeting_archive.ask.search_meetings", lambda _cfg, _question: [])
    called: list[str] = []

    def complete(prompt: str) -> str:
        called.append(prompt)
        return "should not run"

    result = ask(cfg, "pricing", complete=complete)
    assert called == []
    assert result.answer == NOT_FOUND
    html = result.path.read_text(encoding="utf-8")
    assert "Nothing in your meetings" in html
    assert "pricing" in html


def test_answer_page_names_the_meeting(cfg: Config, monkeypatch):
    hits = [
        {
            "document": "Marina raised the billing issue.",
            "metadata": {"date_label": "15 Oct", "meeting_id": "m1"},
        }
    ]
    monkeypatch.setattr("meeting_archive.ask.search_meetings", lambda _cfg, _question: hits)
    result = ask(cfg, "billing", complete=lambda _prompt: "Marina raised billing.")
    html = result.path.read_text(encoding="utf-8")
    assert "Marina raised billing." in html
    assert "15 Oct" in html
    assert "From your meetings" in html
    assert "<h1>billing</h1>" in html
    assert "Marina raised the billing issue." in render_answer_html(
        "billing", "Marina raised billing.", hits
    )


def test_text_chat_does_not_store_a_second_copy(cfg: Config, monkeypatch):
    hits = [{"document": "Friday is the ship date.", "metadata": {"date_label": "15 Oct"}}]
    monkeypatch.setattr("meeting_archive.ask.search_meetings", lambda _cfg, _question: hits)
    ask(cfg, "pricing", complete=lambda _prompt: "We chose Friday.", remember=False)
    assert chat_log(cfg) == []


def test_text_chat_does_not_store_a_second_copy(cfg: Config, monkeypatch):
    hits = [{"document": "Friday is the ship date.", "metadata": {"date_label": "15 Oct"}}]
    monkeypatch.setattr("meeting_archive.ask.search_meetings", lambda _cfg, _question: hits)
    ask(cfg, "pricing", complete=lambda _prompt: "We chose Friday.", remember=False)
    assert chat_log(cfg) == []


def test_followup_sees_the_earlier_chat(cfg: Config, monkeypatch):
    hits = [
        {
            "document": "Friday is the ship date.",
            "metadata": {"date_label": "15 Oct"},
        }
    ]
    monkeypatch.setattr("meeting_archive.ask.search_meetings", lambda _cfg, _question: hits)
    ask(cfg, "pricing", complete=lambda _prompt: "We chose Friday.")
    seen: list[str] = []

    def complete(prompt: str) -> str:
        seen.append(prompt)
        return "Friday."

    ask(cfg, "when do we ship", complete=complete)
    assert "Earlier in this chat" in seen[0]
    assert "pricing" in seen[0]
    assert [item["role"] for item in chat_log(cfg)] == ["you", "yarn", "you", "yarn"]


def test_follow_up_email_uses_the_meeting_named_earlier(cfg: Config, monkeypatch):
    from meeting_archive.ask import save_store
    from meeting_archive.models import Turn
    from meeting_archive.records import Transcript, write_notes

    heidi = "2026-10-16T09-30-43"
    other = "2026-10-15T12-03-49"
    for meeting_id, title, summary in (
        (heidi, "Heidi Health prep", "Mariko will lead the discovery call."),
        (other, "MYOB", "MYOB wants an implementation plan."),
    ):
        transcript = Transcript(
            meeting_id=meeting_id,
            summary=summary,
            turns=[Turn("room", 0.0, 5.0, summary, speaker="room-1")],
            title=title,
        )
        path = cfg.transcript_json(meeting_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        transcript.write(path)
    write_notes(
        cfg.notes_json(heidi),
        heidi,
        notes="Heidi Health prep for first validate call",
        attendees="Mariko, Jacob",
    )
    write_notes(cfg.notes_json(other), other, notes="MYOB next steps", attendees="Charles")
    save_store(
        cfg,
        {
            "active": "email",
            "chats": [
                {
                    "id": "email",
                    "updated": 1,
                    "turns": [
                        {
                            "role": "you",
                            "text": "the meeting from heidi health from today",
                        },
                        {"role": "yarn", "text": "I could not find it in the meetings."},
                    ],
                }
            ],
        },
    )
    monkeypatch.setattr(
        "meeting_archive.ask.search_meetings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("searched")),
    )
    seen: list[str] = []

    def complete(prompt: str) -> str:
        seen.append(prompt)
        return "Subject: Heidi Health\n\nThanks for the call."

    ask(cfg, "I want you to write me the email", complete=complete, remember=False)
    assert "Mariko will lead" in seen[0]
    assert "MYOB" not in seen[0]
    assert "Subject line" in seen[0]


def test_request_chat_leaves_a_flag_and_does_not_script(cfg: Config, monkeypatch):
    opened: list[list[str]] = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kwargs: opened.append(args),
    )
    request_chat(cfg)
    assert (cfg.root / "asks" / ".open-chat").is_file()
    assert opened == [["/usr/bin/open", "-a", "Yarn"]]
    assert "osascript" not in " ".join(opened[0])
