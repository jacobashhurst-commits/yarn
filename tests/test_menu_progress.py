"""The menu bar percent is the whole meeting, the same scale as the Dock window."""

import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "ui" / "meeting.5s.sh.template"


def _overall(phase, percent, indeterminate, elapsed, expected):
    text = TEMPLATE.read_text()
    start = text.index("# OVERALL_PERCENT\n")
    end = text.index("# END_OVERALL_PERCENT\n")
    script = text[start:end] + '\noverall_percent "$1" "$2" "$3" "$4" "$5"\n'
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "overall",
            str(phase),
            str(percent),
            "true" if indeterminate else "false",
            str(elapsed),
            str(expected),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


def test_transcribing_halfway_is_halfway_through_the_meeting_slice():
    # Transcribing owns 2–16. Halfway through the step is 9 of the meeting.
    assert _overall("Transcribing", 50, False, 0, 0) == 9
    assert _overall("Transcribing", 0, False, 0, 0) == 2
    assert _overall("Transcribing", 100, False, 0, 0) == 16


def test_later_steps_continue_from_where_the_previous_one_finished():
    # Voice separation owns most of the bar. Saving comes before tidy-up.
    assert _overall("Identifying speakers", 0, False, 0, 0) == 16
    assert _overall("Identifying speakers", 50, False, 0, 0) == 48
    assert _overall("Saving transcript", 0, False, 0, 0) == 82
    assert _overall("Cleaning transcript", 0, False, 0, 0) == 86


def test_a_step_without_its_own_percent_still_moves_through_its_slice():
    # Half the expected time, capped the way the Dock window caps a guess.
    assert _overall("Transcribing", 0, True, 50, 100) == 9
    assert _overall("Transcribing", 0, True, 500, 100) == 15


def _timed(*args):
    text = TEMPLATE.read_text()
    span_start = text.index("span() {")
    span_end = text.index("# Live figures")
    time_start = text.index("# TIME_LEFT\n")
    time_end = text.index("# END_TIME_LEFT\n")
    script = text[span_start:span_end] + "\n" + text[time_start:time_end] + "\n" + args[0] + "\n"
    result = subprocess.run(
        ["bash", "-c", script, "timed", *args[1:]],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_the_menu_counts_the_whole_meeting_the_same_way():
    # Ten minutes done, eighty left in this step, twenty still to come.
    assert _timed(
        'time_percent "$1" "$2" "$3" "$4" "$5" "$6"',
        "600",
        "4800",
        "1200",
        "0",
        "0",
        "true",
    ) == "9"
    assert _timed(
        'format_eta "$1" "$2" "$3" "$4" "$5"',
        "-80",
        "600",
        "true",
        "true",
        "0",
    ) == "Taking longer than usual, then about 10m"
