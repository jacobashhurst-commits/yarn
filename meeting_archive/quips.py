"""Pick the most recognisable thing each voice actually said.

"Who was Speaker 2?" is unanswerable on its own. "Who was Speaker 2, the one
who said *we finally finished the kitchen renovation*?" answers itself. That
sentence is the whole point of this module.

Everything here is extractive: the quip is a verbatim span of what the speaker
said, chosen and trimmed but never rewritten. A generated summary would read
better and would occasionally attribute a detail to someone who never said it,
which is the one failure this feature cannot have -- Jacob is about to attach a
real person's name to it.

Scoring favours the concrete and the personal, because that is what makes a
voice recognisable. "Sounds good to me" identifies nobody. "I'm flying to
Lisbon on Thursday" identifies exactly one person in the room.
"""

from __future__ import annotations

import re

from .models import SpeakerProfile, Turn

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z']+")

# Openers and backchannel that carry no identity at all. A turn made only of
# these is skipped before scoring rather than penalised, because otherwise a
# meeting of nothing but "yeah, sure, sounds good" still produces a quip and it
# is always misleading.
_FILLER = {
    "yeah", "yep", "yup", "yes", "no", "nope", "nah", "ok", "okay", "okey",
    "right", "sure", "aye", "mhm", "mm", "mmm", "hmm", "uh", "um", "er",
    "ah", "oh", "so", "well", "alright",
    "thanks", "thank", "you", "cheers", "ta", "great", "good", "nice",
    "cool", "awesome", "brilliant", "lovely", "perfect", "exactly",
    "agreed", "agree", "absolutely", "definitely", "totally", "indeed",
    "certainly", "understood", "noted", "correct", "true", "fine",
    "sounds", "sound", "seems", "seem", "makes", "make", "sense", "works",
    "work", "sorry", "hello", "hi", "hey", "bye", "goodbye", "please",
    "can", "hear", "me", "i", "think", "know", "just", "like", "really",
    "and", "the", "a", "to", "of", "it", "that", "this", "is", "are",
    "then", "too", "also", "for", "with", "be", "was", "were", "not",
}

# Phrases that are about the call rather than the person. A quip of "you're on
# mute" tells Jacob nothing about who was speaking.
_MECHANICAL = re.compile(
    r"(?i)\b("
    r"on mute|you're muted|can you hear|hear me now|my (?:audio|mic|camera)|"
    r"bad connection|breaking up|lost you|frozen|share my screen|"
    r"see my screen|let me share|next slide|sorry i'm late|"
    r"joining late|drop off|have to run|hard stop"
    r")\b"
)

_FIRST_PERSON = re.compile(r"(?i)\b(i|i'm|i've|i'll|my|mine|we|we're|our)\b")
_PROPER = re.compile(r"\b([A-Z][a-z]{2,19})\b")
_NUMBER = re.compile(r"\b\d")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _is_all_filler(text: str) -> bool:
    words = _words(text)
    if not words:
        return True
    return all(w in _FILLER for w in words)


def score_sentence(text: str, position: int = 0) -> float:
    """How recognisable is this sentence as coming from one particular person?

    `position` is the sentence's index within the speaker's contribution;
    slightly later sentences are preferred because the first thing anyone says
    on a call is usually a greeting.
    """
    words = _words(text)
    count = len(words)
    if count < 4:
        return 0.0
    if _is_all_filler(text):
        return 0.0
    if _MECHANICAL.search(text):
        return 0.0

    score = 0.0

    # Long enough to be substantive, short enough to read in a dialog. Peaks
    # around 12 words and falls away on both sides.
    score += 4.0 - abs(count - 12) * 0.16

    # Concrete detail: names, places, numbers, dates.
    proper = len(set(_PROPER.findall(text)))
    score += min(proper, 3) * 1.1
    if _NUMBER.search(text):
        score += 0.9

    # Personal statements are what make a voice memorable -- "I just moved
    # house" beats "the deadline is Friday" for telling two strangers apart.
    if _FIRST_PERSON.search(text):
        score += 1.4

    # Proportion of non-filler words: a sentence padded with backchannel is
    # less distinctive than a dense one of the same length.
    substantive = sum(1 for w in words if w not in _FILLER)
    score += (substantive / count) * 1.6

    # A greeting is almost always sentence zero.
    if position == 0:
        score -= 0.8

    return max(score, 0.0)


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE.split(text) if p and p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def trim(text: str, max_chars: int = 110) -> str:
    """Shorten to fit a dialog, on a word boundary, with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:.- ")
    return f"{cut}…" if cut else text[:max_chars]


def pick_quip(turns: list[Turn], max_chars: int = 110) -> str:
    """The most recognisable sentence across one speaker's turns, verbatim.

    Returns "" rather than a weak quip when everything the speaker said was
    filler or call mechanics. An empty quip makes the dialog fall back to
    timing information, which is honest; a misleading one would have Jacob
    attaching a name to the wrong voice.
    """
    best = ""
    best_score = 0.0
    for turn in turns:
        for position, sentence in enumerate(_sentences(turn.text)):
            score = score_sentence(sentence, position)
            if score > best_score:
                best_score, best = score, sentence
    return trim(best, max_chars) if best else ""


def profile_speakers(
    turns: list[Turn],
    guesses: dict[str, str] | None = None,
    max_chars: int = 110,
) -> list[SpeakerProfile]:
    """Build a profile per diarized voice, ordered by who spoke first.

    Turns with no speaker key are ignored: the mic channel is already known to
    be Jacob, and an undiarized channel has nothing to identify.
    """
    guesses = guesses or {}
    grouped: dict[str, list[Turn]] = {}
    for turn in turns:
        if turn.speaker:
            grouped.setdefault(turn.speaker, []).append(turn)

    profiles: list[SpeakerProfile] = []
    for speaker, speaker_turns in grouped.items():
        ordered = sorted(speaker_turns, key=lambda t: t.start)
        profiles.append(
            SpeakerProfile(
                speaker=speaker,
                channel=ordered[0].channel,
                turns=len(ordered),
                speaking_s=sum(max(t.end - t.start, 0.0) for t in ordered),
                first_at=ordered[0].start,
                quip=pick_quip(ordered, max_chars=max_chars),
                guess=guesses.get(speaker, ""),
            )
        )

    profiles.sort(key=lambda p: (p.first_at, p.speaker))
    return profiles


def apply_guesses(
    profiles: list[SpeakerProfile], guesses: dict[str, str]
) -> list[SpeakerProfile]:
    """Re-stamp each profile's suggested name from the final guess map.

    A voice match cannot exist while the profiles are being built: matching a
    known speaker needs a profile first, to pick the snippet it embeds. So the
    guess a profile is constructed with is always the pre-recognition one, and
    without this second pass every known-speaker match was written into the
    transcript's `speaker_guesses` and then silently dropped from the dialog.

    A missing key keeps whatever the profile already had, so re-applying can
    never blank a name that was already there.
    """
    for profile in profiles:
        profile.guess = guesses.get(profile.speaker, profile.guess)
    return profiles
