from pathlib import Path

import pytest

from meeting_archive.config import SummaryConfig
from meeting_archive.models import ROOM, Turn
from meeting_archive.summarize import Summarizer, SummaryUnavailable, _chunks


def test_long_transcripts_split_only_between_turns():
    lines = ["a" * 6, "b" * 6, "c" * 6]
    assert _chunks(lines, 14) == ["aaaaaa\nbbbbbb", "cccccc"]


def test_summarizer_refuses_missing_model_without_downloading(monkeypatch):
    config = SummaryConfig(command="/usr/local/bin/ollama", model="not-local")
    calls = []

    class Result:
        returncode = 0
        stdout = "NAME ID SIZE MODIFIED\nqwen3.5:9b x 1 GB now\n"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr("subprocess.run", fake_run)
    summarizer = Summarizer(config)
    monkeypatch.setattr(summarizer, "is_running", lambda: True)

    with pytest.raises(SummaryUnavailable, match="not installed"):
        summarizer.summarize(
            [Turn(ROOM, 0, 2, "A real transcript.")],
            lambda turn: "Speaker 1",
        )
    assert calls == [["/usr/local/bin/ollama", "list"]]


def test_timeout_and_deferred_messages_are_retryable():
    from meeting_archive.summarize import DEFERRED_TIMEOUT, is_pending_summary

    assert is_pending_summary("deferred: Ollama was not running")
    assert is_pending_summary(DEFERRED_TIMEOUT)
    assert is_pending_summary(
        "local summary failed: Command '['ollama', 'run']' timed out after 600 seconds"
    )
    assert not is_pending_summary("missing local diarization model")


def test_summarizer_skips_without_starting_ollama_when_it_is_off(monkeypatch):
    from meeting_archive.summarize import DEFERRED_SUMMARY

    summarizer = Summarizer(SummaryConfig())
    monkeypatch.setattr(summarizer, "is_running", lambda: False)
    with pytest.raises(SummaryUnavailable, match="not running"):
        summarizer.summarize(
            [Turn(ROOM, 0, 2, "A real transcript.")],
            lambda turn: "Speaker 1",
        )
    assert DEFERRED_SUMMARY.startswith("deferred")


def test_evening_schedule_waits_until_hour_then_catches_up_overnight():
    from datetime import datetime

    from meeting_archive.summarize import (
        DEFERRED_EVENING,
        DEFERRED_MANUAL,
        deferred_summary_reason,
        should_attempt_summary,
    )

    class Cfg:
        summary = SummaryConfig(when="evening", evening_hour=18)

    evening = Cfg()
    morning = datetime(2026, 9, 26, 10, 0, 0)
    night = datetime(2026, 9, 26, 19, 0, 0)
    assert should_attempt_summary(evening, "2026-09-26T09-00-00", now=morning) is False
    assert should_attempt_summary(evening, "2026-09-26T09-00-00", now=night) is True
    assert should_attempt_summary(evening, "2026-09-25T09-00-00", now=morning) is True
    assert deferred_summary_reason(evening) == DEFERRED_EVENING

    evening.summary.when = "manual"
    assert should_attempt_summary(evening, "2026-09-26T09-00-00", now=night) is False
    assert deferred_summary_reason(evening) == DEFERRED_MANUAL

    evening.summary.when = "after_meeting"
    assert should_attempt_summary(evening, "2026-09-26T09-00-00", now=morning) is True


def test_summarizer_uses_local_ollama_http_and_hides_thinking(monkeypatch):
    import json
    from urllib.request import Request

    config = SummaryConfig(command="/usr/local/bin/ollama", model="qwen3.5:9b")
    calls = []

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result("NAME ID SIZE MODIFIED\nqwen3.5:9b x 1 GB now\n")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"response": "<think>private reasoning</think>\nUseful summary."}
            ).encode()

    def fake_urlopen(request, timeout=0):
        assert isinstance(request, Request)
        payload = json.loads(request.data.decode())
        assert payload["model"] == "qwen3.5:9b"
        assert payload["think"] is False
        assert "A real transcript." in payload["prompt"]
        return FakeResponse()

    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    summarizer = Summarizer(config)
    monkeypatch.setattr(summarizer, "is_running", lambda: True)
    summary = summarizer.summarize(
        [Turn(ROOM, 0, 2, "A real transcript.")],
        lambda turn: "Speaker 1",
    )
    assert summary == "Useful summary."
    assert calls == [["/usr/local/bin/ollama", "list"]]
