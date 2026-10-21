"""Yarn Setup speaks JSON lines so the window can show 1 of 9 without guessing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting_archive import setup as yarn_setup


def test_a_partial_download_continues_instead_of_starting_over(tmp_path):
    fresh = yarn_setup.download_request("https://example.invalid/model.bin", 0)
    assert fresh.get_header("Range") is None
    resumed = yarn_setup.download_request("https://example.invalid/model.bin", 4096)
    assert resumed.get_header("Range") == "bytes=4096-"
    assert yarn_setup.resume_mode(206, 4096) == "ab"
    assert yarn_setup.resume_mode(200, 4096) == "wb"
    assert yarn_setup.resume_mode(206, 0) == "wb"

    partial = tmp_path / "model.bin.partial"
    dest = tmp_path / "model.bin"
    partial.write_bytes(b"abc")
    assert yarn_setup.promote_finished_partial(
        partial, dest, {"Content-Range": "bytes */3"}
    )
    assert dest.read_bytes() == b"abc"
    assert not partial.exists()

    partial.write_bytes(b"abcdef")
    assert yarn_setup.promote_finished_partial(
        partial, dest, {"Content-Range": "bytes */3"}
    )
    assert dest.read_bytes() == b"abc"

    partial.write_bytes(b"ab")
    assert not yarn_setup.promote_finished_partial(
        partial, dest, {"Content-Range": "bytes */3"}
    )
    assert partial.read_bytes() == b"ab"


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
    monkeypatch.setattr(yarn_setup, "app_is_trusted", lambda _app: True)
    monkeypatch.setattr(yarn_setup, "_clear_quarantine", lambda _app: None)

    def boom(*_a, **_k):
        raise AssertionError("should not download an app that is already installed")

    monkeypatch.setattr(yarn_setup, "download", boom)
    assert (
        yarn_setup.install_app_from_zip("http://example.invalid", "SwiftBar.app", "menubar")
        == app
    )


def test_install_replaces_an_app_macos_calls_damaged(tmp_path, monkeypatch):
    import shutil
    import subprocess

    apps = tmp_path / "Applications"
    damaged = apps / "Ollama.app"
    damaged.mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(yarn_setup, "application_folders", lambda: [apps])
    monkeypatch.setattr(yarn_setup, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(yarn_setup, "emit", lambda _payload: None)

    def trusted(app: Path) -> bool:
        return (app / "Contents" / "Info.plist").is_file()

    monkeypatch.setattr(yarn_setup, "app_is_trusted", trusted)

    def fake_download(_url, dest: Path, _step, long=False):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"zip")

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[:2] == ["ditto", "-xk"]:
            bundle = Path(argv[3]) / "Ollama.app" / "Contents"
            bundle.mkdir(parents=True)
            (bundle / "Info.plist").write_text("ok", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "ditto":
            src, dest = Path(argv[1]), Path(argv[2])
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["xattr", "-dr"]:
            assert argv[2] == "com.apple.quarantine"
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    monkeypatch.setattr(yarn_setup, "download", fake_download)
    monkeypatch.setattr(yarn_setup, "run", fake_run)

    installed = yarn_setup.install_app_from_zip(
        "http://example.invalid/Ollama.zip", "Ollama.app", "ollama"
    )
    assert installed == apps / "Ollama.app"
    assert (installed / "Contents" / "Info.plist").is_file()
    assert calls[0][:2] == ["ditto", "-xk"]
    assert not any(argv[:2] == ["xattr", "-cr"] for argv in calls)


def test_ollama_binary_uses_path(monkeypatch, tmp_path):
    fake = tmp_path / "ollama"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(yarn_setup, "which", lambda name: str(fake) if name == "ollama" else None)
    monkeypatch.setattr(yarn_setup, "_ollama_runs", lambda path: path == str(fake))
    assert yarn_setup.ollama_binary() == str(fake)


def test_ollama_binary_skips_a_link_macos_will_not_run(monkeypatch, tmp_path):
    fake = tmp_path / "ollama"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(yarn_setup, "which", lambda name: str(fake) if name == "ollama" else None)
    monkeypatch.setattr(yarn_setup, "existing_app", lambda name: None)
    monkeypatch.setattr(yarn_setup, "_ollama_runs", lambda path: False)
    assert yarn_setup.ollama_binary() is None


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
        lambda payload: notes.append(
            str(payload.get("text") or "") + "\n" + str(payload.get("note") or "")
        ),
    )

    yarn_setup.pull_ollama_model("ollama", "qwen3.5:4b", "Fast")

    assert made == [["ollama", "pull", "qwen3.5:4b"], ["ollama", "pull", "qwen3.5:4b"]]
    joined = "\n".join(notes)
    assert "Continuing from what already arrived" in joined
    assert "Downloading Fast" in joined
    assert "dropped" not in joined.lower()
    assert "[?25l" not in joined and "\x1b" not in joined


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
        == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    )
    assert yarn_setup.faster_whisper_repo("org/custom") == "org/custom"
    assert "Systran" not in yarn_setup.faster_whisper_repo("large-v3-turbo")


def test_speech_failure_hides_the_traceback():
    detail = (
        "Traceback (most recent call last):\n"
        '  File "/Users/megashhurst/Library/Application Support/Yarn/app/.venv/'
        'lib/python3.12/site-packages/huggingface_hub/_snapshot_download.py", '
        "line 274, in snapshot_download\n"
        "    repo_info = api.repo_info(repo_id=repo_id)\n"
        "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
        "Repository Not Found. Invalid username or password.\n"
    )
    plain = yarn_setup._plain_failure(detail)
    assert plain == "Repository Not Found. Invalid username or password."
    assert "site-packages" not in plain


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
    assert "download_model" in calls[0][-1]
    assert "mobiuslabsgmbh/faster-whisper-large-v3-turbo" in calls[0][-1]
    assert "Systran" not in calls[0][-1]


def test_flatten_nested_speaker_model(tmp_path):
    dest = tmp_path / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    nested = dest.parent / dest.parent.name / "model.onnx"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"onnx")
    yarn_setup.flatten_segmentation_model(dest)
    assert dest.read_bytes() == b"onnx"


def test_host_python_skips_the_old_system_python(monkeypatch):
    monkeypatch.setattr(yarn_setup.sys, "version_info", (3, 9, 6, "final", 0))

    def which(name):
        return {
            "python3": "/usr/bin/python3",
            "python3.12": "/opt/homebrew/bin/python3.12",
        }.get(name)

    monkeypatch.setattr(yarn_setup, "which", which)
    monkeypatch.setattr(
        yarn_setup,
        "python_supports_yarn",
        lambda path: path.endswith("python3.12"),
    )
    monkeypatch.setattr(yarn_setup, "uv_python_find", lambda: None)
    assert yarn_setup.host_python() == "/opt/homebrew/bin/python3.12"


def test_host_python_explains_python39_without_the_import_error(monkeypatch):
    monkeypatch.setattr(yarn_setup.sys, "version_info", (3, 9, 6, "final", 0))
    monkeypatch.setattr(
        yarn_setup,
        "which",
        lambda name: "/usr/bin/python3" if name == "python3" else None,
    )
    monkeypatch.setattr(yarn_setup, "python_supports_yarn", lambda _path: False)
    monkeypatch.setattr(yarn_setup, "uv_python_find", lambda: None)
    with pytest.raises(RuntimeError, match="Python 3.11") as caught:
        yarn_setup.host_python()
    assert "tomllib" not in str(caught.value)
    assert "3.9" in str(caught.value)


def test_install_host_python_asks_uv_for_312(tmp_path, monkeypatch):
    import subprocess

    uv = tmp_path / "uv"
    monkeypatch.setattr(yarn_setup, "which", lambda _name: None)
    monkeypatch.setattr(yarn_setup, "support_bin", lambda: tmp_path)

    def install_uv():
        uv.write_text("uv", encoding="utf-8")

    monkeypatch.setattr(yarn_setup, "_install_uv", install_uv)
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["python", "find"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=str(tmp_path / "python3.12") + "\n", stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(yarn_setup, "run", fake_run)
    monkeypatch.setattr(
        yarn_setup,
        "python_supports_yarn",
        lambda path: str(path).endswith("python3.12"),
    )
    assert yarn_setup.install_host_python() == str(tmp_path / "python3.12")
    assert calls[0][1:3] == ["python", "install"]
    assert calls[0][3] == "3.12"


def test_uv_binary_is_taken_from_the_tarball(tmp_path):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = b"#!/bin/sh\necho uv\n"
        info = tarfile.TarInfo("uv-aarch64-apple-darwin/uv")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    dest = tmp_path / "uv"
    yarn_setup._extract_uv(buf.getvalue(), dest)
    assert dest.read_bytes().startswith(b"#!/bin/sh")
    assert dest.stat().st_mode & 0o111


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
