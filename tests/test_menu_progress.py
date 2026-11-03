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
    # Transcribing owns 8–62. Halfway through the step is 35 of the meeting.
    assert _overall("Transcribing", 50, False, 0, 0) == 35
    assert _overall("Transcribing", 0, False, 0, 0) == 8
    assert _overall("Transcribing", 100, False, 0, 0) == 62


def test_later_steps_continue_from_where_the_previous_one_finished():
    assert _overall("Identifying speakers", 0, False, 0, 0) == 62
    assert _overall("Identifying speakers", 50, False, 0, 0) == 70
    assert _overall("Saving transcript", 0, False, 0, 0) == 99


def test_a_step_without_its_own_percent_still_moves_through_its_slice():
    # Half the expected time, capped the way the Dock window caps a guess.
    assert _overall("Transcribing", 0, True, 50, 100) == 35
    assert _overall("Transcribing", 0, True, 500, 100) == 58
