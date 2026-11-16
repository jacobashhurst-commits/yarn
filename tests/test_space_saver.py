"""Space saver removes generated pages and nothing else."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from meeting_archive import library, pipeline, space_saver
from meeting_archive.config import Config, write_setting
from meeting_archive.models import ROOM, Segment
from meeting_archive.merge import merge_segments
from meeting_archive.records import Transcript, ensure_transcript_page
from meeting_archive.space_saved import report

NOW = datetime(2026, 11, 16, 12, 0, 0)
OLD = "2026-08-01T10-00-00"
RECENT = "2026-11-10T10-00-00"


class FakeStore:
    def replace_meeting(self, meeting_id, documents, metadatas):
        return len(documents)

    def delete_meeting(self, meeting_id):
        pass


def make_meeting(cfg, meeting_id):
    segments = [Segment(ROOM, 0.0, 5.0, "We agreed to ship on Thursday.")]
    transcript = Transcript(
        meeting_id=meeting_id,
        segments=segments,
        turns=merge_segments(segments),
        duration_s=600.0,
    )
    path = cfg.transcript_json(meeting_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript.write(path)
    cfg.transcript_md(meeting_id).write_text("# the readable copy\n")
    return ensure_transcript_page(cfg, transcript)


@pytest.fixture
def cfg(tmp_path):
    config = Config(root=tmp_path / "archive")
    config.ensure_dirs()
    return config


def everything(cfg):
    return sorted(
        str(p.relative_to(cfg.root)) for p in cfg.root.rglob("*") if p.is_file()
    )


def test_it_is_off_by_default(cfg):
    assert not space_saver.is_on(cfg)
    assert space_saver.keeps_page(cfg, OLD, NOW)


def test_only_old_pages_are_listed_and_the_size_is_theirs(cfg):
    old_page = make_meeting(cfg, OLD)
    make_meeting(cfg, RECENT)
    sweep = space_saver.removable(cfg, NOW)
    assert sweep.paths == [old_page]
    assert sweep.bytes == old_page.stat().st_size
    assert "1 page" in sweep.summary()


def test_looking_removes_nothing(cfg):
    make_meeting(cfg, OLD)
    before = everything(cfg)
    space_saver.removable(cfg, NOW)
    assert everything(cfg) == before


def test_purge_removes_only_the_pages(cfg):
    make_meeting(cfg, OLD)
    make_meeting(cfg, RECENT)
    notes = cfg.notes_json(OLD)
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(json.dumps({"meeting_id": OLD, "notes": "kept"}))
    stray = cfg.transcript_json(OLD).parent / "something-i-saved.html"
    stray.write_text("<p>mine</p>")
    audio = cfg.audio_archive_dir / f"{OLD}__room.wav"
    audio.write_bytes(b"RIFF")
    before = set(everything(cfg))

    done = space_saver.purge(cfg, NOW)

    gone = before - set(everything(cfg))
    assert len(gone) == 1 and next(iter(gone)).endswith(".html")
    assert done.count == 1
    assert cfg.transcript_json(OLD).exists()
    assert cfg.transcript_md(OLD).exists()
    assert notes.exists() and audio.exists()
    # A page that is not Yarn's is not Yarn's to delete.
    assert stray.exists()
    assert cfg.transcript_html(RECENT).exists()


def test_old_briefs_go_and_the_current_one_stays(cfg):
    briefs = cfg.briefs_dir
    (briefs / "2026-08-01.html").write_text("old")
    (briefs / "2026-11-15.html").write_text("recent")
    (briefs / "latest.html").write_text("current")
    (briefs / "notes.html").write_text("not a brief")
    space_saver.purge(cfg, NOW)
    assert sorted(p.name for p in briefs.iterdir()) == [
        "2026-11-15.html",
        "latest.html",
        "notes.html",
    ]


def test_what_was_freed_is_counted_in_space_saved(cfg):
    page = make_meeting(cfg, OLD)
    size = page.stat().st_size
    space_saver.purge(cfg, NOW)
    assert report(cfg)["space"]["pages_bytes"] == size
    make_meeting(cfg, "2026-07-01T09-00-00")
    second = cfg.transcript_html("2026-07-01T09-00-00").stat().st_size
    space_saver.purge(cfg, NOW)
    assert report(cfg)["space"]["pages_bytes"] == size + second


def test_a_removed_page_can_be_made_again_from_the_transcript(cfg):
    page = make_meeting(cfg, OLD)
    before = page.read_text()
    space_saver.purge(cfg, NOW)
    assert not page.exists()
    again = ensure_transcript_page(cfg, Transcript.read(cfg.transcript_json(OLD)))
    assert again.read_text() == before


def test_an_old_meeting_opens_from_the_library_without_a_saved_page(cfg):
    make_meeting(cfg, OLD)
    make_meeting(cfg, RECENT)
    write_setting(cfg, "space_saver", True)
    space_saver.purge(cfg)
    html = library.render_transcript_index(cfg).read_text()
    # The library did not write the old page back.
    assert not cfg.transcript_html(OLD).exists()
    assert f"/meeting?id={OLD}&amp;token=" in html
    assert cfg.transcript_html(RECENT).as_uri() in html
    # And the link has something behind it.
    body = library.render_meeting_page(cfg, OLD).decode()
    assert "We agreed to ship on Thursday." in body


def test_with_it_off_the_library_writes_pages_as_before(cfg):
    page = make_meeting(cfg, OLD)
    page.unlink()
    html = library.render_transcript_index(cfg).read_text()
    assert page.exists() and page.as_uri() in html


def test_reindexing_an_old_meeting_does_not_bring_its_page_back(cfg):
    make_meeting(cfg, OLD)
    write_setting(cfg, "space_saver", True)
    pipeline.ingest(cfg, OLD, FakeStore())
    assert not cfg.transcript_html(OLD).exists()
    assert cfg.transcript_md(OLD).exists()


def test_the_on_demand_page_refuses_anything_that_is_not_a_meeting_id(cfg):
    for bad in ["../../etc/passwd", "", "2026-08-01T10-00-00/../x", "a" * 20]:
        with pytest.raises(ValueError):
            library.render_meeting_page(cfg, bad)


def test_a_page_is_no_longer_mostly_logo(cfg):
    page = make_meeting(cfg, RECENT)
    assert page.stat().st_size < 120_000
