"""The first Headphones press on a new install asks who the microphone is."""

from __future__ import annotations

import json

from meeting_archive.cli import ensure_own_name
from meeting_archive.config import load_config, name_prompt_pending


def _archive(tmp_path, settings=None, label="Sarah"):
    root = tmp_path / "archive"
    root.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(
        f'root = "{root}"\nown_label = "{label}"\n\n[names]\nown_names = ["Jacob", "Jake"]\n',
        encoding="utf-8",
    )
    if settings is not None:
        (root / ".yarn-settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )
    return load_config(path), path


def test_an_existing_config_is_not_asked_for_a_name(tmp_path):
    cfg, path = _archive(tmp_path, settings={"recognise_known": True})
    assert name_prompt_pending(cfg) is False
    asked = []
    assert ensure_own_name(cfg, path, ask=lambda default: asked.append(default)) == 0
    assert asked == []
    assert 'own_label = "Sarah"' in path.read_text(encoding="utf-8")


def test_a_confirmed_name_is_not_asked_again(tmp_path):
    cfg, path = _archive(tmp_path, settings={"own_name_confirmed": True})
    assert ensure_own_name(cfg, path, ask=lambda _default: "Sam") == 0
    assert "Sam" not in path.read_text(encoding="utf-8")


def test_first_headphones_use_saves_the_name(tmp_path):
    cfg, path = _archive(tmp_path, settings={"own_name_confirmed": False})
    asked = []

    def ask(default):
        asked.append(default)
        return "Sam Taylor"

    assert ensure_own_name(cfg, path, ask=ask) == 0
    assert asked == ["Sarah"]
    loaded = load_config(path)
    assert loaded.own_label == "Sam Taylor"
    assert loaded.names.own_names == ["Sam Taylor", "Sam"]
    saved = json.loads((cfg.root / ".yarn-settings.json").read_text(encoding="utf-8"))
    assert saved["own_name_confirmed"] is True


def test_cancel_leaves_the_name_and_asks_again_next_time(tmp_path):
    cfg, path = _archive(tmp_path, settings={"own_name_confirmed": False}, label="Sarah")
    before = path.read_text(encoding="utf-8")
    assert ensure_own_name(cfg, path, ask=lambda _default: "   ") == 2
    assert path.read_text(encoding="utf-8") == before
    assert name_prompt_pending(load_config(path)) is True


def test_a_single_name_does_not_invent_a_short_form(tmp_path):
    cfg, path = _archive(tmp_path, settings={"own_name_confirmed": False})
    assert ensure_own_name(cfg, path, ask=lambda _default: "Sam") == 0
    assert load_config(path).names.own_names == ["Sam"]


def test_an_apostrophe_roundtrips(tmp_path):
    cfg, path = _archive(tmp_path, settings={"own_name_confirmed": False})
    assert ensure_own_name(cfg, path, ask=lambda _default: "Sam O'Brien") == 0
    loaded = load_config(path)
    assert loaded.own_label == "Sam O'Brien"
    assert loaded.names.own_names == ["Sam O'Brien", "Sam"]
