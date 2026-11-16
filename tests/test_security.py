"""Yarn stays on this Mac, and what it installs is what it expected.

None of these touch the network. The model server and the library server
are exercised through the functions that decide, not through a socket.
"""

from __future__ import annotations

import io
import stat

import pytest

from meeting_archive import library
from meeting_archive import setup as yarn_setup
from meeting_archive.config import Config, SummaryConfig, load_config, write_setting
from meeting_archive.summarize import (
    Summarizer,
    SummaryUnavailable,
    is_local_host,
    remote_host_refusal,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://LOCALHOST:11434/",
        "http://[::1]:11434",
    ],
)
def test_a_model_server_on_this_mac_is_local(url):
    assert is_local_host(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:11434",
        "https://api.example.com",
        "http://127.0.0.1.example.com:11434",
        "http://localhost.example.com",
        "http://example.com/127.0.0.1",
        "not a url",
        "",
    ],
)
def test_anything_else_is_not_local(url):
    assert not is_local_host(url)


def test_a_transcript_is_never_sent_to_another_machine(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: sent.append(a) or io.BytesIO(b"")
    )
    summarizer = Summarizer(SummaryConfig(host="http://192.168.1.20:11434"))

    assert summarizer.is_running() is False
    with pytest.raises(SummaryUnavailable, match="not this Mac"):
        summarizer._generate("http://192.168.1.20:11434/api/generate", {"prompt": "x"})
    assert sent == []


def test_another_machine_needs_an_explicit_switch():
    config = SummaryConfig(host="http://192.168.1.20:11434")
    assert remote_host_refusal(config)
    config.allow_remote_host = True
    assert remote_host_refusal(config) == ""


def test_the_switch_is_off_unless_config_says_so(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'root = "{tmp_path}"\n[summary]\nhost = "http://10.0.0.5:11434"\n')
    cfg = load_config(path)
    assert cfg.summary.allow_remote_host is False
    assert remote_host_refusal(cfg.summary)


def test_the_archive_is_readable_only_by_its_owner(tmp_path):
    root = tmp_path / "archive"
    root.mkdir(mode=0o755)
    cfg = Config(root=root)
    cfg.ensure_dirs()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_a_setting_is_written_whole(tmp_path):
    cfg = Config(root=tmp_path)
    write_setting(cfg, "audio_retention", "1_month")
    write_setting(cfg, "summarize_when", "manual")
    assert not (tmp_path / ".yarn-settings.json.tmp").exists()
    assert cfg.audio_retention == "1_month"
    assert cfg.summary.when == "manual"


def test_a_download_that_is_not_the_expected_file_is_removed(tmp_path):
    path = tmp_path / "ffmpeg"
    path.write_bytes(b"something else")
    with pytest.raises(RuntimeError, match="not the file Yarn expects"):
        yarn_setup.verify_file(path, "0" * 64)
    assert not path.exists()


def test_the_expected_file_is_kept(tmp_path):
    path = tmp_path / "model.bin"
    path.write_bytes(b"yarn")
    yarn_setup.verify_file(path, yarn_setup.file_sha256(path))
    assert path.exists()


def test_download_checks_a_known_url(tmp_path, monkeypatch):
    dest = tmp_path / "ffmpeg"

    def fake(url, dest, step_id, long=False):
        dest.write_bytes(b"tampered")

    monkeypatch.setattr(yarn_setup, "_download_unverified", fake)
    with pytest.raises(RuntimeError):
        yarn_setup.download(yarn_setup.FFMPEG_STATIC, dest, "tools")
    assert not dest.exists()


def test_every_pinned_download_has_a_checksum_and_a_fixed_version():
    for url in (
        yarn_setup.WHISPER_GGML,
        yarn_setup.DIARIZE_EMB,
        yarn_setup.FFMPEG_STATIC,
        yarn_setup.FFPROBE_STATIC,
        yarn_setup.UV_ARM64,
        yarn_setup.SWIFTBAR_ZIP,
    ):
        assert len(yarn_setup.DOWNLOAD_SHA256[url]) == 64
        assert "/latest/" not in url
        assert "/resolve/main/" not in url


@pytest.mark.parametrize(
    "host, ok",
    [
        ("127.0.0.1:47331", True),
        ("localhost:47331", True),
        ("[::1]:47331", True),
        ("evil.example.com:47331", False),
        ("127.0.0.1.evil.example.com", False),
        ("", False),
    ],
)
def test_the_library_only_answers_to_its_own_address(host, ok):
    assert library._is_local_host_header(host) is ok
