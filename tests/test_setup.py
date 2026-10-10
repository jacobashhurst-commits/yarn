"""Yarn Setup speaks JSON lines so the window can show 1 of 9 without guessing."""

from __future__ import annotations

import json

import pytest

from meeting_archive import setup as yarn_setup


def test_setup_step_list_matches_the_window_contract():
    ids = [step_id for step_id, _ in yarn_setup.STEPS]
    assert ids == [
        "prereqs",
        "venv",
        "deps",
        "tools",
        "config",
        "whisper",
        "speakers",
        "ollama",
        "menubar",
    ]
    assert set(ids) == set(yarn_setup.HANDLERS)


def test_setup_emits_hello_progress_and_finish(tmp_path, monkeypatch, capsys):
    for step_id, _ in yarn_setup.STEPS:
        monkeypatch.setitem(yarn_setup.HANDLERS, step_id, lambda repo: None)

    assert yarn_setup.run_setup(tmp_path) == 0
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert events[0]["type"] == "hello"
    assert len(events[0]["steps"]) == 9
    starts = [e for e in events if e["type"] == "start"]
    dones = [e for e in events if e["type"] == "done"]
    assert [e["id"] for e in starts] == [s["id"] for s in events[0]["steps"]]
    assert all(e["ok"] for e in dones)
    assert events[-1] == {
        "type": "finish",
        "ok": True,
        "message": "Yarn is ready. Look for the ⏺ icon in the menu bar.",
    }


def test_setup_stops_on_the_first_failed_step(tmp_path, monkeypatch, capsys):
    def boom(_repo):
        raise RuntimeError("disk full")

    monkeypatch.setitem(yarn_setup.HANDLERS, "prereqs", boom)
    assert yarn_setup.run_setup(tmp_path) == 1
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert events[-1]["ok"] is False
    assert "disk full" in events[-1]["message"]
    assert [e["id"] for e in events if e["type"] == "start"] == ["prereqs"]


def test_prereqs_reject_intel_macs(tmp_path, monkeypatch):
    monkeypatch.setattr(yarn_setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(yarn_setup.platform, "machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="Apple Silicon"):
        yarn_setup.step_prereqs(tmp_path)
