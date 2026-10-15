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
