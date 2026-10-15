"""Daily brief is read-only HTML over recent meetings."""

from __future__ import annotations

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
    assert "Open brief at login" in html
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


def test_should_hint_after_manual_uses(cfg: Config):
    assert not brief.should_hint_login_brief(cfg)
    from meeting_archive.config import write_setting

    write_setting(cfg, "brief_manual_count", 3)
    assert brief.should_hint_login_brief(cfg)
    write_setting(cfg, "brief_on_login", False)
    assert not brief.should_hint_login_brief(cfg)
