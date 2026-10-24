"""Core data types.

Channels are identified internally, and the channel id is what carries the
knowledge of who is speaking:

  mic    Jacob's microphone on a call -- known to be him
  sys    system audio on a call -- known to be everyone else
  room   an in-person meeting captured on one microphone -- several people,
         none of them identifiable

`room` exists because the alternative was labelling everyone in the room as
Jacob, which is confidently wrong rather than merely unknown. Human-facing
labels come from config, so the logic never hardcodes a name.

Diarization refines a channel; it does not replace it. The channel still
carries what is *known* (the mic is Jacob, full stop), and a speaker key
subdivides what is merely *distinguishable*: `sys` becomes sys-1/sys-2 for two
people on a call, `room` becomes room-1/room-2/room-3 round a desk. A speaker
key means "a voice we could tell apart", never "a person we identified" -- the
name only arrives when Jacob confirms it, and until then the label is
"Speaker 2", which is honest.

The mic channel is never diarized. It is one person by construction, and
splitting it could only ever invent a second Jacob.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MIC = "mic"
SYS = "sys"
ROOM = "room"

# Speaker keys look like "room-1", ordered by when the voice was first heard.
_SPEAKER_KEY = re.compile(r"^(?P<channel>[a-z]+)-(?P<index>\d+)$")


def speaker_key(channel: str, index: int) -> str:
    """Build a stable within-meeting key for a distinguished voice.

    Indices are 1-based and assigned in order of first appearance, not in
    whatever order the diarizer happened to emit its clusters. That makes
    "Speaker 1" mean "the first person heard", which is the only ordering a
    human can verify against their memory of the meeting.
    """
    return f"{channel}-{index}"


def speaker_index(key: str) -> int | None:
    """The 1-based ordinal in a speaker key, or None if it isn't one."""
    match = _SPEAKER_KEY.match(key or "")
    return int(match.group("index")) if match else None


def speaker_display_indices(profiles: list) -> dict[str, int]:
    """Human-facing Speaker 1..N by first appearance in the meeting.

    Diarizer keys keep their internal suffix (``room-8``) after same-voice
    merges absorb other clusters. The identify window and transcript must not
    show that raw suffix when only five distinct voices remain.
    """
    # The microphone profile uses the bare channel id. Numbering it would
    # steal "Speaker 1" from the first person on the computer audio and
    # rename them in every summary already written.
    ordered = sorted(
        (
            profile
            for profile in profiles
            if speaker_index(str(getattr(profile, "speaker", "") or "")) is not None
        ),
        key=lambda profile: (
            float(getattr(profile, "first_at", 0.0) or 0.0),
            str(getattr(profile, "speaker", "")),
        ),
    )
    return {profile.speaker: index for index, profile in enumerate(ordered, start=1)}


@dataclass
class SpeakerSpan:
    """A stretch of one channel's audio attributed to one distinguished voice."""

    channel: str
    start: float
    end: float
    speaker: str

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "speaker": self.speaker,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpeakerSpan":
        return cls(
            channel=d["channel"],
            start=float(d["start"]),
            end=float(d["end"]),
            speaker=d["speaker"],
        )


@dataclass
class Segment:
    """One Whisper segment from one channel, optionally attributed to a voice."""

    channel: str
    start: float
    end: float
    text: str
    # None means "not diarized": either the channel is one known person, or
    # diarization was off or unavailable. It never means "nobody spoke".
    speaker: str | None = None

    def to_dict(self) -> dict:
        d = {
            "channel": self.channel,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }
        # Omitted when absent, so undiarized transcripts stay byte-identical to
        # the ones written before diarization existed.
        if self.speaker:
            d["speaker"] = self.speaker
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(
            channel=d["channel"],
            start=float(d["start"]),
            end=float(d["end"]),
            text=d["text"],
            speaker=d.get("speaker") or None,
        )


@dataclass
class Turn:
    """Consecutive segments from the same speaker, collapsed into one turn."""

    channel: str
    start: float
    end: float
    text: str
    speaker: str | None = None

    def to_dict(self) -> dict:
        d = {
            "channel": self.channel,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }
        if self.speaker:
            d["speaker"] = self.speaker
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Turn":
        return cls(
            channel=d["channel"],
            start=float(d["start"]),
            end=float(d["end"]),
            text=d["text"],
            speaker=d.get("speaker") or None,
        )

    @property
    def voice(self) -> str:
        """The identity this turn is grouped under: the speaker if diarized.

        Turns collapse on this rather than on the channel alone, so two people
        on the same `room` microphone never merge into one turn.
        """
        return self.speaker or self.channel


@dataclass
class Chunk:
    index: int
    start: float
    end: float
    channels: list[str]
    text: str
    # Resolved human-facing labels for the voices in this chunk -- real names
    # where Jacob has given them, "Speaker 2" where he hasn't. Held here rather
    # than recomputed at index time so the chunk text and its `speakers`
    # metadata can never disagree about who is in it.
    speakers: list[str] = field(default_factory=list)


@dataclass
class NameCandidate:
    """A name detected in the transcript, with why we think it's a name."""

    name: str
    count: int = 0
    # "other" = someone on the far side of the call. "own" = Jacob himself.
    attributed_to: str = "other"
    patterns: set[str] = field(default_factory=set)
    evidence: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "attributed_to": self.attributed_to,
            "patterns": sorted(self.patterns),
            "evidence": self.evidence,
        }


@dataclass
class SpeakerGuess:
    """A name we think belongs to a specific diarized voice, and why.

    A guess from what was said only pre-fills the dialog. Dismissing it leaves
    the honest Speaker N label. A voice match that already cleared both gates
    is written onto the meeting so a returning voice is not asked again, and
    that write does not update the stored voice.
    """

    speaker: str
    name: str
    # "self"   the voice introduced itself: "I'm Marina"
    # "handoff" the previous voice handed over by name: "over to you, John"
    basis: str = "self"
    confidence: float = 0.0
    evidence: str = ""

    def to_dict(self) -> dict:
        return {
            "speaker": self.speaker,
            "name": self.name,
            "basis": self.basis,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpeakerGuess":
        return cls(
            speaker=d["speaker"],
            name=d["name"],
            basis=d.get("basis", "self"),
            confidence=float(d.get("confidence") or 0.0),
            evidence=d.get("evidence", ""),
        )


@dataclass
class SpeakerProfile:
    """What we can say about one diarized voice without knowing who it is.

    `quip` is a verbatim sentence the voice actually said, chosen to be the
    most recognisable thing in their contribution. It is the handle Jacob uses
    to remember which person this was.
    """

    speaker: str
    channel: str
    turns: int = 0
    speaking_s: float = 0.0
    first_at: float = 0.0
    quip: str = ""
    guess: str = ""

    def to_dict(self) -> dict:
        return {
            "speaker": self.speaker,
            "channel": self.channel,
            "turns": self.turns,
            "speaking_s": round(self.speaking_s, 2),
            "first_at": round(self.first_at, 2),
            "quip": self.quip,
            "guess": self.guess,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpeakerProfile":
        return cls(
            speaker=d["speaker"],
            channel=d.get("channel", ""),
            turns=int(d.get("turns") or 0),
            speaking_s=float(d.get("speaking_s") or 0.0),
            first_at=float(d.get("first_at") or 0.0),
            quip=d.get("quip", ""),
            guess=d.get("guess", ""),
        )


def format_duration(seconds: float) -> str:
    """Short human duration for dialogs, e.g. '4m 12s'."""
    total = max(0, int(seconds))
    m, s = divmod(total, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def format_timestamp(seconds: float) -> str:
    """Seconds to H:MM:SS for transcript rendering."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"
