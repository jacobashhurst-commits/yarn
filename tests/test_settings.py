"""The menu bar, the Settings window and the command line share one file.

Each of them changes a setting by running `ma setting KEY VALUE`. These go
through that command and then load the config the way the pipeline does.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from meeting_archive import cli
from meeting_archive.config import load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    root.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(f'root = "{root}"\nown_label = "Jacob"\n\n[names]\nown_names = ["Jacob"]\n')
    monkeypatch.setenv("MEETING_ARCHIVE_CONFIG", str(path))
    return path, root


def setting(key, value):
    return cli.main(["setting", key, value])


@pytest.mark.parametrize(
    "key, value, read, expected",
    [
        ("space_saver", "on", lambda c: c.space_saver, True),
        ("identify_popup", "off", lambda c: c.diarize.identify_popup, False),
        ("suggest_names_popup", "off", lambda c: c.names.suggest_popup, False),
        ("cleanup_enabled", "on", lambda c: c.cleanup.enabled, True),
        ("pause_on_pressure", "off", lambda c: c.processing.pause_on_pressure, False),
        ("summary_profile", "actions", lambda c: c.summary.profile, "actions"),
        ("evening_hour", "20", lambda c: c.summary.evening_hour, 20),
        ("min_participant_s", "5", lambda c: c.diarize.min_participant_s, 5.0),
        ("min_participant_s", "0", lambda c: c.diarize.min_participant_s, 0.0),
        ("audio_retention", "1_month", lambda c: c.audio_retention_days, 30),
        ("audio_retention", "month", lambda c: c.audio_retention, "1_month"),
        ("summarize_when", "manual", lambda c: c.summary.when, "manual"),
    ],
)
def test_a_setting_reaches_the_config_the_pipeline_loads(home, key, value, read, expected):
    assert setting(key, value) == 0
    assert read(load_config()) == expected


@pytest.mark.parametrize(
    "key, value",
    [
        ("summary_profile", "poetry"),
        ("evening_hour", "25"),
        ("evening_hour", "soon"),
        ("min_participant_s", "-1"),
        ("min_participant_s", "lots"),
        ("audio_retention", "forever"),
        ("space_saver", "maybe"),
    ],
)
def test_a_value_that_makes_no_sense_is_refused_and_nothing_is_written(home, key, value):
    _, root = home
    assert setting(key, value) == 1
    assert not (root / ".yarn-settings.json").exists()


def test_your_name_is_saved_where_transcripts_read_it(home):
    path, root = home
    assert setting("own_name", "Sam Taylor") == 0
    data = tomllib.loads(path.read_text())
    assert data["own_label"] == "Sam Taylor"
    assert data["names"]["own_names"] == ["Sam Taylor", "Sam"]
    assert load_config().own_label == "Sam Taylor"
    assert json.loads((root / ".yarn-settings.json").read_text())["own_name_confirmed"] is True


def test_an_empty_name_is_refused(home):
    path, _ = home
    assert setting("own_name", "   ") == 1
    assert tomllib.loads(path.read_text())["own_label"] == "Jacob"


def test_settings_do_not_overwrite_each_other(home):
    setting("space_saver", "on")
    setting("summary_profile", "short")
    setting("evening_hour", "19")
    cfg = load_config()
    assert (cfg.space_saver, cfg.summary.profile, cfg.summary.evening_hour) == (True, "short", 19)
