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
