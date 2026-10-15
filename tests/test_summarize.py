from pathlib import Path

import pytest

from meeting_archive.config import SummaryConfig
from meeting_archive.models import ROOM, Turn
from meeting_archive.summarize import (
    Summarizer,
    SummaryUnavailable,
    _chunks,
    record_saved_tokens,
    saved_token_total,
)


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


def test_ollama_oom_is_a_deferred_human_line():
    from meeting_archive.summarize import is_pending_summary, ollama_http_message

    message = ollama_http_message(
        500,
        '{"error":"model requires 7.1 GiB but only 4.8 GiB are available"}',
    )
    assert "7.1 GiB" in message
    assert is_pending_summary(message)


def test_recent_memory_refusal_is_not_retried_every_sweep(tmp_path):
    from meeting_archive.records import Transcript
    from meeting_archive.summarize import should_retry_summary

    path = tmp_path / "meeting.json"
    transcript = Transcript(
        meeting_id="m1",
        summary_error="deferred: model requires 7.1 GiB but only 4.8 GiB are available",
    )
    path.write_text("{}", encoding="utf-8")
    now = path.stat().st_mtime
    assert should_retry_summary(path, transcript, force=False, now=now + 10) is False
    assert should_retry_summary(path, transcript, force=True, now=now + 10) is True
    assert should_retry_summary(path, transcript, force=False, now=now + 700) is True


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


def test_token_deltas_count_only_new_tokens():
    from meeting_archive.summarize import _note_token_delta

    seen = {"prompt_eval_count": 0, "eval_count": 0}
    recorded = []

    def capture(delta, *, root):
        recorded.append(delta)

    import meeting_archive.summarize as summarize

    original = summarize.adjust_saved_tokens
    summarize.adjust_saved_tokens = capture
    try:
        _note_token_delta({"done": False, "response": "Hi"}, seen)
        _note_token_delta({"eval_count": 4}, seen)
        _note_token_delta({"prompt_eval_count": 10, "eval_count": 4}, seen)
        _note_token_delta({"prompt_eval_count": 10, "eval_count": 9}, seen)
        _note_token_delta({"prompt_eval_count": 10, "eval_count": 9}, seen)
    finally:
        summarize.adjust_saved_tokens = original
    # "Hi" is an estimate of 1, then Ollama's counts replace it.
    assert recorded == [1, 3, 10, 5]


def test_saved_tokens_add_up(tmp_path):
    record_saved_tokens(120, root=tmp_path)
    record_saved_tokens(30, root=tmp_path)
    assert saved_token_total(tmp_path) == 150
    record_saved_tokens(0, root=tmp_path)
    assert saved_token_total(tmp_path) == 150
