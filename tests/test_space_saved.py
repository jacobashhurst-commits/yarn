"""The Dock's storage line is video never recorded plus audio already purged."""

from meeting_archive.space_saved import bytes_not_stored, format_saved


def test_held_audio_counts_the_video_only():
    one_hour = bytes_not_stored(3600, audio_kept=True)
    assert format_saved(one_hour) == "1.0 GB saved"


def test_purged_audio_counts_the_video_and_the_recording():
    saved = bytes_not_stored(3600, audio_kept=False)
    held = bytes_not_stored(3600, audio_kept=True)
    assert saved > held
    assert format_saved(saved) == "2.2 GB saved"


def test_the_unit_grows_from_megabytes_to_terabytes():
    assert format_saved(840_000_000) == "840 MB saved"
    assert format_saved(4_200_000_000) == "4.2 GB saved"
    assert format_saved(86_000_000_000) == "86 GB saved"
    assert format_saved(1_400_000_000_000) == "1.4 TB saved"
    assert format_saved(12_000_000_000_000) == "12 TB saved"


# --- the figures behind the Dock tooltips -----------------------------------

import json

from meeting_archive.config import Config, load_config
from meeting_archive.space_saved import (
    format_usd,
    report,
    saved_bytes,
    space_breakdown,
    token_cost_usd,
)
from meeting_archive.summarize import (
    adjust_saved_tokens,
    adjust_token_split,
    saved_token_split,
)


def _meeting(root, meeting_id, seconds):
    folder = root / "transcripts" / meeting_id[:4] / meeting_id[5:7]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{meeting_id}.json").write_text(
        json.dumps({"meeting_id": meeting_id, "duration_s": seconds})
    )


def test_cost_prices_reading_and_writing_separately():
    cost = token_cost_usd(
        1_000_000, 100_000, 0, input_usd_per_million=2.0, output_usd_per_million=10.0
    )
    assert cost == 3.0


def test_tokens_counted_before_the_split_are_priced_as_reading():
    cost = token_cost_usd(
        0, 0, 500_000, input_usd_per_million=2.0, output_usd_per_million=10.0
    )
    assert cost == 1.0


def test_money_is_written_plainly():
    assert format_usd(0.004) == "under $0.01"
    assert format_usd(3.5) == "$3.50"
    assert format_usd(1234.4) == "$1,234"


def test_the_split_never_claims_more_than_the_total(tmp_path):
    adjust_saved_tokens(1000, root=tmp_path)
    adjust_token_split(300, 50, root=tmp_path)
    assert saved_token_split(tmp_path) == (300, 50, 650)
    # An old file has only a total.
    (tmp_path / ".tokens-saved.json").write_text('{"total": 1979}\n')
    assert saved_token_split(tmp_path) == (0, 0, 1979)
    # Adding to the total keeps the split that was there.
    adjust_token_split(10, 5, root=tmp_path)
    adjust_saved_tokens(15, root=tmp_path)
    assert saved_token_split(tmp_path) == (10, 5, 1979)


def test_the_report_and_the_total_are_the_same_arithmetic(tmp_path):
    cfg = Config(root=tmp_path)
    _meeting(tmp_path, "2026-09-11T10-00-00", 3600)
    _meeting(tmp_path, "2026-09-12T10-00-00", 1800)
    archive = tmp_path / "recordings" / "archive"
    archive.mkdir(parents=True)
    (archive / "2026-09-12T10-00-00__room.wav").write_bytes(b"")
    (tmp_path / ".space-saver.json").write_text('{"freed_bytes": 5000000}')

    data = report(cfg)
    space = data["space"]
    assert space["video_bytes"] == 1_500_000_000
    # Only the meeting whose audio is gone counts its audio.
    assert space["audio_bytes"] == 3600 * 320_000
    assert space["pages_bytes"] == 5_000_000
    assert space["total_bytes"] == saved_bytes(tmp_path)
    assert space["label"] == "2.7 GB saved"
    assert "720p video" in space["tooltip"]
    assert "1 GB an hour" in space["tooltip"]
    assert "Space saver" in space["tooltip"]
    assert "estimate" in space["tooltip"]


def test_the_token_tooltip_names_its_assumptions(tmp_path):
    cfg = Config(root=tmp_path)
    adjust_saved_tokens(1_100_000, root=tmp_path)
    adjust_token_split(1_000_000, 100_000, root=tmp_path)
    tokens = report(cfg)["tokens"]
    assert tokens["cost_usd"] == 3.0
    tip = tokens["tooltip"]
    assert tip.startswith("Approx. $3.00")
    assert "Claude Sonnet 5.5" in tip and "$2 per million" in tip and "$10 per million" in tip


def test_an_empty_archive_has_honest_tooltips(tmp_path):
    data = report(Config(root=tmp_path))
    assert data["space"]["label"] == "0.0 MB saved"
    assert "Nothing yet" in data["tokens"]["tooltip"]


def test_prices_and_the_video_rate_come_from_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'root = "{tmp_path}"\n[savings]\nreference_model = "Some Model"\n'
        "input_usd_per_million = 1.5\noutput_usd_per_million = 6\n"
        'video_gb_per_hour = 2\nvideo_label = "1080p video"\n'
    )
    cfg = load_config(path)
    _meeting(tmp_path, "2026-09-11T10-00-00", 3600)
    (tmp_path / "recordings" / "archive").mkdir(parents=True)
    (tmp_path / "recordings" / "archive" / "2026-09-11T10-00-00__room.wav").write_bytes(b"")
    adjust_saved_tokens(1_000_000, root=tmp_path)
    data = report(cfg)
    assert space_breakdown(tmp_path, 2_000_000_000)["video_bytes"] == 2_000_000_000
    assert data["space"]["video_bytes"] == 2_000_000_000
    assert "1080p video" in data["space"]["tooltip"]
    assert data["tokens"]["cost_usd"] == 1.5
    assert "Some Model" in data["tokens"]["tooltip"]
