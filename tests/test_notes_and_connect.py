"""Written notes on a meeting, and the switch for apps outside Yarn."""

from __future__ import annotations

import json

import pytest

from meeting_archive import cli, connect, pipeline
from meeting_archive.config import Config
from meeting_archive.merge import merge_segments
from meeting_archive.models import ROOM, Segment
from meeting_archive.records import (
    Transcript,
    read_notes,
    render_markdown,
    render_transcript_html,
    write_notes,
)


class Store:
    def __init__(self):
        self.documents, self.metadatas = [], []

    def replace_meeting(self, meeting_id, documents, metadatas):
        self.documents, self.metadatas = documents, metadatas
        return len(documents)


MEETING = "2026-11-10T10-00-00"


@pytest.fixture
def cfg(tmp_path):
    config = Config(root=tmp_path / "archive")
    config.ensure_dirs()
    segments = [Segment(ROOM, 0.0, 5.0, "We agreed to ship on Thursday.")]
    transcript = Transcript(
        meeting_id=MEETING, segments=segments, turns=merge_segments(segments), duration_s=60
    )
    path = config.transcript_json(MEETING)
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript.write(path)
    return config


def test_written_notes_are_saved_beside_the_name(cfg):
    path = cfg.notes_json(MEETING)
    write_notes(path, MEETING, notes="Launch planning", detail="Ask Priya about the budget.")
    saved = read_notes(path)
    assert saved["notes"] == "Launch planning"
    assert saved["detail"] == "Ask Priya about the budget."


def test_saving_something_else_later_does_not_wipe_the_notes(cfg):
    path = cfg.notes_json(MEETING)
    write_notes(path, MEETING, detail="Ask Priya about the budget.")
    write_notes(path, MEETING, speakers={"room-1": "Sam"})
    write_notes(path, MEETING, detail="")
    assert read_notes(path)["detail"] == "Ask Priya about the budget."


def test_written_notes_appear_on_the_transcript(cfg):
    notes = {"detail": "Ask Priya about the budget.\nChase the contract."}
    transcript = Transcript.read(cfg.transcript_json(MEETING))
    label = cfg.turn_labeller({}, {})
    markdown = render_markdown(transcript, label, notes)
    assert "## Your notes" in markdown
    assert markdown.index("## Your notes") < markdown.index("## Summary")
    page = render_transcript_html(transcript, label, notes)
    assert "Your notes" in page and "Chase the contract." in page
    # Under the listen and who-spoke cards, above the voice list.
    assert page.index("Listen") < page.index("Who spoke") < page.index("Your notes")


def test_notes_on_the_transcript_page_can_be_replaced_or_cleared(cfg):
    from meeting_archive import library

    library.save_meeting_notes(cfg, MEETING, "Bring the contract.")
    page = cfg.transcript_html(MEETING).read_text()
    assert "Bring the contract." in page
    assert 'name="detail"' in page
    assert "Save notes" in page
    library.save_meeting_notes(cfg, MEETING, "")
    saved = read_notes(cfg.notes_json(MEETING))
    assert saved["detail"] == ""
    assert saved["applied"] is False
    assert "Bring the contract." not in cfg.transcript_html(MEETING).read_text()
    assert "Save notes" in cfg.transcript_html(MEETING).read_text()


def test_room_and_headphone_recordings_both_play(cfg):
    from meeting_archive import library

    archive = cfg.audio_archive_dir
    (archive / f"{MEETING}__room.wav").write_bytes(b"RIFFroom")
    room = library.recording_sources(cfg, MEETING, "tok")
    assert [caption for caption, _href in room] == ["The recording"]
    assert "channel=room" in room[0][1]
    (archive / f"{MEETING}__room.wav").unlink()
    (archive / f"{MEETING}__mic.wav").write_bytes(b"RIFFmic")
    (archive / f"{MEETING}__sys.wav").write_bytes(b"RIFFsys")
    (archive / f"{MEETING}__call.wav").write_bytes(b"RIFFcall")
    call = library.recording_sources(cfg, MEETING, "tok")
    assert [caption for caption, _href in call] == ["Jacob", "The other side"]
    assert "channel=mic" in call[0][1] and "channel=sys" in call[1][1]
    page = library.render_meeting_page(cfg, MEETING).decode()
    assert "Play both" in page
    assert "channel=mic" in page and "channel=sys" in page
    assert "channel=call" not in page


def test_no_notes_means_no_empty_section(cfg):
    transcript = Transcript.read(cfg.transcript_json(MEETING))
    assert "Your notes" not in render_markdown(transcript, cfg.turn_labeller({}, {}), {})


def test_written_notes_are_searchable_and_not_passed_off_as_speech(cfg):
    write_notes(cfg.notes_json(MEETING), MEETING, detail="Ask Priya about the budget.")
    store = Store()
    pipeline.ingest(cfg, MEETING, store)
    assert len(store.documents) == 2
    assert "Ask Priya about the budget." in store.documents[-1]
    assert store.metadatas[-1]["speakers"] == ["Written notes"]
    assert "Ask Priya" not in store.documents[0]


def test_notes_markup_cannot_break_the_page(cfg):
    transcript = Transcript.read(cfg.transcript_json(MEETING))
    page = render_transcript_html(
        transcript, cfg.turn_labeller({}, {}), {"detail": "<script>alert(1)</script>"}
    )
    assert "<script>alert(1)</script>" not in page


# --- apps outside Yarn -----------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "archive"
    root.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'root = "{root}"\n')
    monkeypatch.setenv("MEETING_ARCHIVE_CONFIG", str(config))
    monkeypatch.setattr(cli, "_tell", lambda *a, **k: None)
    return tmp_path, Config(root=root)


def _claude(home_dir):
    return home_dir / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def test_it_is_off_on_a_new_install_and_connecting_is_refused(home, capsys):
    home_dir, cfg = home
    assert connect.external_enabled(cfg) is False
    assert cli.main(["connect", "cursor"]) == 1
    assert "turned off" in capsys.readouterr().err
    assert not (home_dir / ".cursor" / "mcp.json").exists()


def test_turning_it_on_allows_a_connection(home):
    home_dir, cfg = home
    assert cli.main(["setting", "external_apps", "on"]) == 0
    assert cli.main(["connect", "cursor"]) == 0
    assert "yarn" in json.loads((home_dir / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert connect.connected_apps() == ["Cursor"]


def test_someone_already_connected_is_shown_as_on(home):
    home_dir, cfg = home
    path = _claude(home_dir)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"yarn": {"command": "x"}}}))
    assert connect.external_enabled(cfg) is True
    assert connect.connected_apps() == ["Claude"]


def test_disconnect_removes_only_yarn(home):
    home_dir, cfg = home
    path = _claude(home_dir)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {"yarn": {"command": "x"}, "other": {"command": "y"}},
            }
        )
    )
    assert cli.main(["connect", "disconnect"]) == 0
    after = json.loads(path.read_text())
    assert after == {"theme": "dark", "mcpServers": {"other": {"command": "y"}}}
    assert connect.connected_apps() == []
    assert connect.external_enabled(cfg) is False


def test_status_is_json_the_settings_window_can_read(home, capsys):
    cli.main(["connect", "status"])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {
        "enabled": False,
        "connected": [],
    }
