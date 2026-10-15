"""Daily brief is read-only HTML over recent meetings."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from meeting_archive import brief, records
from meeting_archive.config import Config
from meeting_archive.models import ROOM, Turn
from meeting_archive.records import Transcript


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config(root=tmp_path / "archive")
    config.ensure_dirs()
    return config


def _write_meeting(cfg: Config, when: datetime, title: str, summary: str, minutes: int = 30):
    meeting_id = when.strftime("%Y-%m-%dT%H-%M-%S")
    transcript = Transcript(
        meeting_id=meeting_id,
        segments=[],
        turns=[Turn(ROOM, 0.0, 5.0, "hello")],
        duration_s=minutes * 60,
        model="test",
        title=title,
        summary=summary,
        md_name=records.markdown_filename(meeting_id, title),
    )
    path = cfg.transcript_json(meeting_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript.write(path)
    records.write_text_atomic(cfg.transcript_md(meeting_id), f"# {title}\n")
    records.write_notes(
        cfg.notes_json(meeting_id),
        meeting_id=meeting_id,
        notes=title,
        attendees="Jacob, Alex",
        applied=True,
    )
    return meeting_id


def test_gather_sources_looks_back_two_weeks(cfg: Config):
    today = date(2026, 10, 15)
    _write_meeting(
        cfg,
        datetime(2026, 10, 14, 16, 0, 0),
        "MYOB ROM",
        "**Action items**\n* Alex: send the deck\n",
    )
    _write_meeting(
        cfg,
        datetime(2026, 9, 1, 10, 0, 0),
        "Old news",
        "Should be ignored",
    )
    sources = brief.gather_sources(cfg, today=today)
    assert len(sources) == 1
    assert sources[0].title == "MYOB ROM"


def test_hours_by_day_fills_empty_days(cfg: Config):
    today = date(2026, 10, 15)
    _write_meeting(
        cfg,
        datetime(2026, 10, 14, 9, 0, 0),
        "A",
        "summary",
        minutes=60,
    )
    hours = brief.hours_by_day(
        brief.gather_sources(cfg, today=today), today=today
    )
    assert len(hours) == 7
    assert hours["2026-10-14"] == 1.0
    assert hours["2026-10-15"] == 0.0


def test_render_brief_html_includes_charts_and_sections(cfg: Config):
    today = date(2026, 10, 15)
    mid = _write_meeting(
        cfg,
        datetime(2026, 10, 14, 16, 0, 0),
        "MYOB ROM",
        "Action: send pricing",
        minutes=45,
    )
    sources = brief.gather_sources(cfg, today=today)
    result = brief.BriefResult(
        day=today,
        overview="Focus on MYOB follow-through.",
        actions=[brief.BriefItem("Send pricing", mid, "MYOB ROM", "From yesterday")],
        important=[brief.BriefItem("Scoping still open", mid, "MYOB ROM", "")],
        start=[brief.BriefItem("Draft team matrix", mid, "MYOB ROM", "")],
        focus=[
            brief.FocusSlice("MYOB", 60),
            brief.FocusSlice("Internal", 40),
        ],
        sources=sources,
        hours_by_day=brief.hours_by_day(sources, today=today),
        model="test-model",
    )
    html = brief.render_brief_html(result, hint_login=True)
    assert "Yarn daily brief" in html
    assert "Where the week went" in html
    assert "Where to spend today" in html
    assert "Needs action" in html
    assert "Send pricing" in html
    assert "From the meeting" in html
    assert "Draft" in html
    assert "Open brief at login" in html
    assert "Saved on this Mac" in html
    assert "data:image/png;base64," in html
    assert "#0D244B" in html
    assert "test-model" in html


def test_login_brief_noop_when_disabled(cfg: Config, monkeypatch):
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("should not call model")

    monkeypatch.setattr(brief.Summarizer, "_run", boom)
    try:
        brief.generate_brief(cfg, login=True)
        assert False, "expected SummaryUnavailable"
    except brief.SummaryUnavailable as exc:
        assert "on login is off" in str(exc).lower()
    assert called["n"] == 0


def test_missing_summary_is_labelled_not_filled_in(cfg: Config):
    today = date(2026, 10, 15)
    meeting_id = "2026-10-14T09-00-00"
    transcript = Transcript(
        meeting_id=meeting_id,
        segments=[],
        turns=[Turn(ROOM, 0.0, 5.0, "We should send the deck")],
        duration_s=600,
        model="test",
        title="No summary yet",
        summary="",
    )
    path = cfg.transcript_json(meeting_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript.write(path)
    records.write_text_atomic(cfg.transcript_md(meeting_id), "# No summary yet\n")
    sources = brief.gather_sources(cfg, today=today)
    assert len(sources) == 1
    assert sources[0].summary_missing
    html = brief.render_brief_html(
        brief.BriefResult(
            day=today,
            overview="Waiting on a summary.",
            sources=sources,
            hours_by_day=brief.hours_by_day(sources, today=today),
        )
    )
    assert "Summary not written yet" in html


def test_login_setting_records_the_ma_that_was_clicked(cfg: Config, tmp_path, monkeypatch):
    import sys

    from meeting_archive.cli import cmd_setting

    ma = tmp_path / "ma"
    ma.write_text("#!/bin/sh\n", encoding="utf-8")
    seen: dict[str, str] = {}

    def fake_install(_cfg, ma_bin):
        seen["bin"] = ma_bin
        return tmp_path / "agent.plist"

    monkeypatch.setattr(sys, "argv", [str(ma)])
    monkeypatch.setattr("meeting_archive.brief.install_login_agent", fake_install)
    args = type("A", (), {"key": "brief_on_login", "value": "on"})()
    assert cmd_setting(cfg, args) == 0
    assert seen["bin"] == str(ma.resolve())


def test_connect_keeps_existing_claude_settings(tmp_path, monkeypatch):
    from meeting_archive.connect import merge_mcp_config

    path = tmp_path / "claude_desktop_config.json"
    path.write_text('{"preferences": {"foo": 1}}\n', encoding="utf-8")
    merge_mcp_config(path, {"command": "/bin/chroma-mcp", "args": ["--data-dir", "/tmp/chroma"]})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["preferences"] == {"foo": 1}
    assert saved["mcpServers"]["yarn"]["command"] == "/bin/chroma-mcp"


def test_claude_connect_writes_after_quit(cfg: Config, tmp_path, monkeypatch):
    from meeting_archive.connect import connect_target

    normal = tmp_path / "Claude" / "claude_desktop_config.json"
    third = tmp_path / "Claude-3p" / "claude_desktop_config.json"
    normal.parent.mkdir()
    normal.write_text('{"preferences": {"foo": 1}}\n', encoding="utf-8")
    monkeypatch.setattr(
        "meeting_archive.connect.claude_config_paths",
        lambda: [normal, third],
    )
    order: list[tuple[bool, bool]] = []

    def restart(*, quit_first, reopen):
        order.append((quit_first, reopen))
        if quit_first:
            normal.write_text('{"preferences": {"foo": 1}}\n', encoding="utf-8")

    monkeypatch.setattr("meeting_archive.connect.restart_claude", restart)
    monkeypatch.setattr("meeting_archive.connect.probe_server", lambda _cfg: None)
    text = connect_target(cfg, "claude")
    saved = json.loads(normal.read_text(encoding="utf-8"))
    third_saved = json.loads(third.read_text(encoding="utf-8"))
    assert saved["preferences"] == {"foo": 1}
    assert saved["mcpServers"]["yarn"]["args"][0] == "--client-type"
    assert third_saved["mcpServers"]["yarn"]["command"].endswith("chroma-mcp")
    assert order == [(True, False), (False, True)]
    assert "Connectors" in text


def test_ollama_connect_prepares_local_search(cfg: Config, tmp_path, monkeypatch):
    from meeting_archive.connect import connect_target

    cfg.summary.model = "qwen3.5:4b"
    normal = tmp_path / "Claude" / "claude_desktop_config.json"
    third = tmp_path / "Claude-3p" / "claude_desktop_config.json"
    monkeypatch.setattr(
        "meeting_archive.connect.claude_config_paths",
        lambda: [normal, third],
    )
    monkeypatch.setattr("meeting_archive.connect.ollama_problem", lambda _cfg: None)
    text = connect_target(cfg, "ollama")
    saved = json.loads((cfg.root / "mcp" / "yarn.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["yarn"]["command"].endswith("chroma-mcp")
    assert "yarn" in json.loads(third.read_text(encoding="utf-8"))["mcpServers"]
    assert "Ask Yarn" in text
    assert "ChatGPT" in text


def test_should_hint_after_manual_uses(cfg: Config):
    assert not brief.should_hint_login_brief(cfg)
    from meeting_archive.config import write_setting

    write_setting(cfg, "brief_manual_count", 3)
    assert brief.should_hint_login_brief(cfg)
    write_setting(cfg, "brief_on_login", False)
    assert not brief.should_hint_login_brief(cfg)
