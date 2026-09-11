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


def test_summarizer_uses_local_ollama_and_hides_thinking(monkeypatch):
    config = SummaryConfig(command="/usr/local/bin/ollama", model="qwen3.5:9b")
    calls = []

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[-1] == "list":
            return Result("NAME ID SIZE MODIFIED\nqwen3.5:9b x 1 GB now\n")
        assert "--think=false" in args
        assert "A real transcript." in kwargs["input"]
        return Result("<think>private reasoning</think>\nUseful summary.")

    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr("subprocess.run", fake_run)
    summarizer = Summarizer(config)
    monkeypatch.setattr(summarizer, "is_running", lambda: True)
    summary = summarizer.summarize(
        [Turn(ROOM, 0, 2, "A real transcript.")],
        lambda turn: "Speaker 1",
    )
    assert summary == "Useful summary."
    assert len(calls) == 2
