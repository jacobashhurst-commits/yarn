"""Yarn Setup speaks JSON lines so the window can show 1 of 9 without guessing."""

from __future__ import annotations

import json
from pathlib import Path

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
        "message": (
            "Yarn is in the Dock. Click it to record, watch progress, "
            "name voices, or ask about your meetings. The menu bar has "
            "the same controls. Everything stays on this Mac."
        ),
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


def test_install_app_skips_download_when_already_present(tmp_path, monkeypatch):
    app = tmp_path / "SwiftBar.app"
    app.mkdir()
    monkeypatch.setattr(yarn_setup, "existing_app", lambda name: app)

    def boom(*_a, **_k):
        raise AssertionError("should not download an app that is already installed")

    monkeypatch.setattr(yarn_setup, "download", boom)
    assert (
        yarn_setup.install_app_from_zip("http://example.invalid", "SwiftBar.app", "menubar")
        == app
    )


def test_ollama_binary_uses_path(monkeypatch, tmp_path):
    fake = tmp_path / "ollama"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(yarn_setup, "which", lambda name: str(fake) if name == "ollama" else None)
    assert yarn_setup.ollama_binary() == str(fake)


def test_prereqs_reject_intel_macs(tmp_path, monkeypatch):
    monkeypatch.setattr(yarn_setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(yarn_setup.platform, "machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="Apple Silicon"):
        yarn_setup.step_prereqs(tmp_path)


def test_launchd_cannot_use_downloads(tmp_path, monkeypatch):
    home = tmp_path / "home"
    downloads = home / "Downloads" / "yarn-main"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert yarn_setup.is_blocked_for_launchd(downloads)
    assert not yarn_setup.is_blocked_for_launchd(home / "code" / "yarn")
    assert not yarn_setup.is_blocked_for_launchd(
        home / "Library" / "Application Support" / "Yarn" / "app"
    )


def test_setup_copies_a_downloads_zip_before_creating_the_venv(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    source = home / "Downloads" / "yarn-main"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname='yarn'\n", encoding="utf-8")
    (source / "keep.txt").write_text("ok", encoding="utf-8")
    (source / ".venv").mkdir()
    (source / ".venv" / "stale").write_text("no", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(yarn_setup, "run", lambda *_a, **_k: None)

    dest = yarn_setup.materialize_runtime(source)
    assert dest == home / "Library" / "Application Support" / "Yarn" / "app"
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "ok"
    assert not (dest / ".venv" / "stale").exists()
    logs = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert any("Downloads" in e.get("text", "") for e in logs)


def test_setup_installs_from_the_lockfile_when_uv_is_there(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **_k):
        calls.append(list(argv))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (tmp_path / ".venv" / "bin" / "ma").write_text("", encoding="utf-8")
    monkeypatch.setattr(yarn_setup, "which", lambda name: "/opt/homebrew/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(yarn_setup, "run", fake_run)
    monkeypatch.setattr(yarn_setup, "venv_python", lambda repo: repo / ".venv" / "bin" / "python")

    yarn_setup.step_deps(tmp_path)
    assert calls[0][:3] == ["/opt/homebrew/bin/uv", "sync", "--frozen"]
    assert "--directory" in calls[0]


def test_neo_setup_will_not_download_accurate(monkeypatch):
    monkeypatch.setattr(yarn_setup, "_PULL", ["fast", "accurate"])
    monkeypatch.setattr("meeting_archive.resources.can_run_accurate", lambda ram_gb=None: False)
    assert yarn_setup.requested_pulls() == ["fast"]


def test_summary_model_is_smaller_on_an_8gb_mac():
    from meeting_archive.resources import summary_model_choice

    model, notice = summary_model_choice(8.0)
    assert model == "qwen3.5:4b"
    assert "8 GB" in notice
    model, notice = summary_model_choice(16.0)
    assert model == "qwen3.5:9b"
    assert notice == ""


def test_write_summary_model_appends_and_replaces(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('root = "/tmp"\n', encoding="utf-8")
    yarn_setup.write_summary_model(path, "qwen3.5:4b")
    assert 'model = "qwen3.5:4b"' in path.read_text(encoding="utf-8")
    yarn_setup.write_summary_model(path, "qwen3.5:9b")
    text = path.read_text(encoding="utf-8")
    assert text.count("[summary]") == 1
    assert 'model = "qwen3.5:9b"' in text
    assert 'model = "qwen3.5:4b"' not in text


def test_sherpa_runtime_is_linked_beside_the_extension(tmp_path):
    site = tmp_path
    sherpa_lib = site / "sherpa_onnx" / "lib"
    capi = site / "onnxruntime" / "capi"
    sherpa_lib.mkdir(parents=True)
    capi.mkdir(parents=True)
    real = capi / "libonnxruntime.1.30.0.dylib"
    real.write_bytes(b"dylib")

    yarn_setup.link_sherpa_runtime(site)

    for name in ("libonnxruntime.dylib", "libonnxruntime.1.dylib"):
        link = sherpa_lib / name
        assert link.is_symlink()
        assert link.resolve() == real.resolve()
    yarn_setup.link_sherpa_runtime(site)
    assert (sherpa_lib / "libonnxruntime.dylib").resolve() == real.resolve()


def test_ollama_list_names_match_what_summaries_check():
    listing = (
        "NAME          ID    SIZE    MODIFIED\n"
        "qwen3.5:4b    abc   2.5 GB  2 hours ago\n"
        "qwen3.5:9b    def   6.6 GB  yesterday\n"
    )
    assert yarn_setup.ollama_model_names(listing) == {"qwen3.5:4b", "qwen3.5:9b"}
    assert yarn_setup.ollama_model_names("NAME\n") == set()


def test_pull_progress_reads_a_percent():
    assert yarn_setup._percent_in("pulling a1b2  70% ") == 70
    assert yarn_setup._percent_in("pulling manifest") is None


def test_ollama_status_hides_cursor_codes():
    raw = (
        "\x1b[?2026h\x1b[?25l\x1b[1Gpulling manifest "
        "\x1b[?2026h\x1b[?25l\x1b[1GError: unexpected EOF"
    )
    lines, rest = yarn_setup._take_status(raw)
    assert lines == ["pulling manifest"]
    assert rest == "Error: unexpected EOF"
    assert "\x1b" not in " ".join(lines)
    assert "[?2026h" not in " ".join(lines)


def test_ollama_pull_retries_a_dropped_download(monkeypatch):
    class Stream:
        def __init__(self, text: str):
            self.text = text
            self.sent = False

        def read(self, _n: int) -> str:
            if self.sent:
                return ""
            self.sent = True
            return self.text

    class Proc:
        def __init__(self, text: str, code: int):
            self.stdout = Stream(text)
            self.code = code

        def wait(self, timeout: int = 0) -> int:
            return self.code

    scripts = [
        ("\x1b[?25l\x1b[1GError: unexpected EOF\n", 1),
        ("pulling qwen 100%\n", 0),
    ]
    made: list[list[str]] = []

    def popen(argv, **_kwargs):
        made.append(list(argv))
        text, code = scripts.pop(0)
        return Proc(text, code)

    monkeypatch.setattr(yarn_setup.subprocess, "Popen", popen)
    monkeypatch.setattr(yarn_setup.time, "sleep", lambda _s: None)
    notes: list[str] = []
    monkeypatch.setattr(
        yarn_setup,
        "emit",
        lambda payload: notes.append(str(payload.get("text") or "")),
    )

    yarn_setup.pull_ollama_model("ollama", "qwen3.5:4b", "Fast")

    assert made == [["ollama", "pull", "qwen3.5:4b"], ["ollama", "pull", "qwen3.5:4b"]]
    assert any("Trying again" in note for note in notes)
    assert not any("[?25l" in note or "\x1b" in note for note in notes)


def test_ollama_pull_does_not_retry_a_real_refusal(monkeypatch):
    class Stream:
        def __init__(self):
            self.sent = False

        def read(self, _n: int) -> str:
            if self.sent:
                return ""
            self.sent = True
            return "Error: model 'nope' not found\n"

    class Proc:
        def __init__(self):
            self.stdout = Stream()

        def wait(self, timeout: int = 0) -> int:
            return 1

    calls = {"n": 0}

    def popen(argv, **_kwargs):
        calls["n"] += 1
        return Proc()

    monkeypatch.setattr(yarn_setup.subprocess, "Popen", popen)
    monkeypatch.setattr(yarn_setup, "emit", lambda _payload: None)

    with pytest.raises(RuntimeError, match="not found"):
        yarn_setup.pull_ollama_model("ollama", "nope", "Fast")
    assert calls["n"] == 1


def test_faster_whisper_repo_names_the_huggingface_id():
    assert (
        yarn_setup.faster_whisper_repo("large-v3-turbo")
        == "Systran/faster-whisper-large-v3-turbo"
    )
    assert yarn_setup.faster_whisper_repo("org/custom") == "org/custom"


def test_step_whisper_prefetches_faster_whisper_when_there_is_no_cli(
    tmp_path, monkeypatch
):
    class Whisper:
        engine = "faster_whisper"
        model = "large-v3-turbo"
        model_path = "/tmp/unused"

    class Cfg:
        whisper = Whisper()

    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "/cache/model"
        stderr = ""

    monkeypatch.setattr("meeting_archive.config.load_config", lambda: Cfg())
    monkeypatch.setattr(
        yarn_setup,
        "run",
        lambda argv, **_k: calls.append(list(argv)) or Result(),
    )
    monkeypatch.setattr(
        yarn_setup, "venv_python", lambda repo: Path("/venv/bin/python")
    )

    yarn_setup.step_whisper(tmp_path)
    assert calls
    assert calls[0][0] == "/venv/bin/python"
    assert "snapshot_download" in calls[0][-1]
    assert "Systran/faster-whisper-large-v3-turbo" in calls[0][-1]


def test_flatten_nested_speaker_model(tmp_path):
    dest = tmp_path / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    nested = dest.parent / dest.parent.name / "model.onnx"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"onnx")
    yarn_setup.flatten_segmentation_model(dest)
    assert dest.read_bytes() == b"onnx"


def test_guess_mic_uses_the_real_device_name(monkeypatch):
    listing = (
        "[AVFoundation indev @ 0x123] AVFoundation video devices:\n"
        "[AVFoundation indev @ 0x123] [0] FaceTime HD Camera\n"
        "[AVFoundation indev @ 0x123] AVFoundation audio devices:\n"
        "[AVFoundation indev @ 0x123] [0] BlackHole 2ch\n"
        "[AVFoundation indev @ 0x123] [1] MacBook Neo Microphone\n"
    )
    monkeypatch.setattr(
        "meeting_archive.audio.list_avfoundation_devices", lambda: listing
    )
    assert yarn_setup._guess_mic() == "MacBook Neo Microphone"
