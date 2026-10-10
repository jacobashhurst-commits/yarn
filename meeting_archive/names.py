"""Detect people's names from what was actually said in the meeting.

This is deliberately pattern matching, not a model. In a two-channel v1 there
is only "Jacob" and "everyone else", so we cannot attach a name to a specific
voice. What we *can* do is work out which names came up, which is enough to
pre-fill the attendee box that would otherwise be empty now that calendar
lookup is deferred.

Three kinds of evidence, each with different attribution:

  SELF       "I'm John", "John here"        -> the speaker of that turn
                                              (not assumed to be Jacob when
                                               the channel is a shared room mic)
  ADDRESSED  "hey John", "thanks, John"     -> someone the speaker is talking to
  MENTION    "this is John", "meet John"    -> a third party being presented

Precision matters much more than recall here. A wrong suggestion costs Jacob a
correction every meeting and he stops trusting the box; a missed name costs him
nothing, because he can still type it himself. So the patterns are narrow and
the stoplist is broad.

Known limitations, accepted:
  - First names only. Compound and prefixed surnames (McKenzie, O'Brien) fail
    the word-boundary check and are skipped rather than mangled.
  - Names must be in Title Case, which is how Whisper writes them. ALL-CAPS
    names are not detected.
  - Names that collide with stoplisted words (May, Bill lowercased, Grace is
    fine but April is not) are sacrificed. Blocking a real name costs Jacob
    one typed word; suggesting a wrong one costs him trust in the feature.
  - Whisper's spelling of names is unreliable. Everything here is a
    suggestion, always editable, never applied silently.
"""

from __future__ import annotations

import re

from .models import MIC, ROOM, NameCandidate, SpeakerGuess, Turn

SELF = "self"
ADDRESSED = "addressed"
MENTION = "mention"

# A capitalised word that stands alone. The \b at the end means prefixed names
# like "McKenzie" produce no match at all rather than a truncated "Mc".
_NAME = r"\b([A-Z][a-z]{1,19})\b"

# Trigger words need to match at the start of a sentence ("Hey John") or mid
# sentence ("so hey John"), but the *name* group must stay case-sensitive or
# precision collapses. Scoped inline flags give case-insensitivity to the
# trigger alone.
def _ci(*words: str) -> str:
    return r"(?:(?i:" + "|".join(words) + r"))"


# Second-person words that follow an interior vocative. "So, John, can you..."
# is a name; "I spoke to Legal, Finance, and Procurement" is not.
_ADDRESSEE_CUE = _ci(
    "do", "does", "did", "can", "could", "would", "will", "what", "how",
    "any", "anything", "are", "is", "you", "your", "have", "thoughts",
    "feel", "think", "got", "mind", "over",
)

# (pattern_name, kind, regex)
_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # --- SELF ---
    (
        "self_intro",
        SELF,
        # "I'm Marina," is a name. "I'm Sydney-based" is a location adjective
        # and must not pre-fill the identify dialog.
        re.compile(
            _ci("i'm", "i am", "my name is", "my name's")
            + r"\s+"
            + _NAME
            + r"(?!-|\s+(?:based|born|side)\b)"
        ),
    ),
    ("name_here", SELF, re.compile(r"^\s*" + _NAME + r"\s+here\b")),
    # --- ADDRESSED ---
    (
        "greeting_vocative",
        ADDRESSED,
        re.compile(
            _ci("hey", "hi", "hello", "yo", "morning", "afternoon", "evening", "welcome")
            + r"\s*,?\s+"
            + _NAME
        ),
    ),
    (
        "thanks_vocative",
        ADDRESSED,
        re.compile(_ci("thanks", "thank you", "cheers") + r"\s*,?\s+" + _NAME),
    ),
    (
        "handover_vocative",
        ADDRESSED,
        re.compile(
            _ci("over to you", "handing over to", "back to you", "take it away", "go ahead")
            + r"\s*,?\s+"
            + _NAME
        ),
    ),
    # "What do you think, John?" -- vocative at the end of a clause.
    ("trailing_vocative", ADDRESSED, re.compile(r",\s*" + _NAME + r"\s*[?!.]")),
    ("trailing_vocative_eol", ADDRESSED, re.compile(r",\s*" + _NAME + r"\s*$")),
    # "John, can you share your screen?" -- vocative opening a turn.
    ("turn_initial_vocative", ADDRESSED, re.compile(r"^\s*" + _NAME + r"\s*,\s")),
    # "So, John, what do you think?" -- needs the addressee cue to avoid
    # matching every capitalised item in a comma-separated list.
    (
        "interior_vocative",
        ADDRESSED,
        re.compile(r",\s*" + _NAME + r"\s*,\s+(?=" + _ADDRESSEE_CUE + r"\b)"),
    ),
    # --- MENTION ---
    ("presentation", MENTION, re.compile(_ci("this is", "meet") + r"\s+" + _NAME)),
]

# Words that are capitalised in transcripts and sit exactly where a name would.
# Checked case-insensitively.
_STOPWORDS = {
    # fillers and discourse markers
    "yeah", "yep", "yes", "no", "nope", "nah", "ok", "okay", "alright",
    "right", "so", "and", "but", "or", "well", "um", "uh", "ah", "oh", "hmm",
    "anyway", "however", "actually", "basically", "obviously", "sorry",
    "please", "thanks", "thank", "cheers", "sure", "cool", "nice", "great",
    "perfect", "awesome", "exactly", "absolutely", "definitely", "maybe",
    "look", "listen", "see", "wait", "hang", "hold", "hey", "hi", "hello",
    "yo", "welcome", "good", "bad", "fine",
    # pronouns and determiners
    "i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your",
    "yours", "he", "him", "his", "she", "her", "hers", "it", "its", "they",
    "them", "their", "theirs", "this", "that", "these", "those", "there",
    "here", "who", "whom", "whose", "what", "when", "where", "why", "how",
    "which", "all", "both", "each", "every", "some", "any", "none",
    # common verbs and modals
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "done", "have", "has", "had", "can", "could", "will", "would",
    "shall", "should", "may", "might", "must", "let", "lets", "go", "going",
    "get", "got", "give", "take", "make", "made", "said", "says", "think",
    "know", "want", "need", "sounds", "seems",
    # group address
    "everyone", "everybody", "guys", "folks", "team", "all", "mate", "man",
    "dude", "sir", "madam", "ma", "buddy", "champ", "people", "gents",
    # time
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "today", "tomorrow", "yesterday", "tonight", "morning",
    "afternoon", "evening", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december",
    "now", "then", "next", "last", "first", "second", "third",
    # tools and brands that get addressed or mentioned like people
    "google", "siri", "alexa", "zoom", "teams", "slack", "chrome", "claude",
    "chatgpt", "notion", "jira", "figma", "excel", "outlook", "gmail",
    "windows", "mac", "apple", "microsoft", "meet", "webex", "loom",
    # misc high-frequency capitalised words
    "thing", "things", "stuff", "one", "two", "three", "point", "part",
    "sort", "kind", "bit", "lot", "way", "yet", "just", "even", "still",
    "also", "because", "if", "as", "at", "on", "in", "to", "for", "with",
    "from", "by", "of", "the", "a", "an",
    # indefinite pronouns -- "Nobody here has seen it" hits `name_here`
    "nobody", "somebody", "anybody", "someone", "anyone", "everything",
    "nothing", "something", "anything", "aboard", "over", "out", "up",
    "down", "back", "away", "around", "aside",
    # states and adjectives that follow "I'm" -- "I'm Sorry", "I'm Keen"
    "tired", "busy", "free", "late", "early", "ready", "done", "happy",
    "sad", "glad", "keen", "unsure", "aware", "across", "afraid", "excited",
    "curious", "confused", "guessing", "thinking", "hoping", "trying",
    "working", "looking", "calling", "joining", "running", "sharing",
    "presenting", "speaking", "based", "new", "old", "confident",
    "comfortable", "fantastic", "terrible", "keen", "agreed", "noted",
    "correct", "wrong", "true", "false", "confidential", "internal",
    "external", "urgent", "important",
    # demonyms -- "I'm Australian, based in Sydney"
    "australian", "aussie", "british", "american", "canadian", "indian",
    "chinese", "japanese", "korean", "french", "german", "dutch", "irish",
    "scottish", "welsh", "english", "spanish", "italian", "portuguese",
    "brazilian", "mexican", "russian", "polish", "swedish", "danish",
    "norwegian", "finnish", "greek", "turkish", "israeli", "egyptian",
    "nigerian", "kenyan", "filipino", "indonesian", "vietnamese", "thai",
    "malaysian", "singaporean", "kiwi", "european", "asian", "african",
    # business functions -- capitalised constantly, never first names.
    # "I spoke to Legal, Finance, do you know who owns it?"
    "legal", "finance", "sales", "marketing", "procurement", "support",
    "ops", "operations", "engineering", "product", "design", "data",
    "platform", "infrastructure", "security", "compliance", "risk",
    "billing", "revenue", "treasury", "payroll", "accounting", "admin",
    "customer", "success", "delivery", "growth", "partnerships",
}


def detect_names(
    turns: list[Turn],
    own_names: list[str] | None = None,
    min_count: int = 1,
) -> list[NameCandidate]:
    """Return ranked name candidates found in the transcript.

    Candidates attributed to Jacob himself are still returned (with
    attributed_to == "own") so callers can see why a name was rejected, but
    they sort last and are normally filtered out before display.
    """
    own_lower = {n.strip().lower() for n in (own_names or []) if n.strip()}
    found: dict[str, NameCandidate] = {}

    for turn in turns:
        # Count a name at most once per turn, so one rambling turn cannot
        # inflate a candidate's score.
        seen_in_turn: set[str] = set()

        for pattern_name, kind, regex in _PATTERNS:
            for match in regex.finditer(turn.text):
                raw = match.group(1)
                key = raw.lower()

                if key in _STOPWORDS or len(raw) < 2:
                    continue

                attributed = _attribute(kind, turn.channel, key, own_lower)

                candidate = found.get(key)
                if candidate is None:
                    candidate = NameCandidate(
                        name=raw.capitalize(),
                        count=0,
                        attributed_to=attributed,
                        evidence=_evidence(turn.text, match.start(1), match.end(1)),
                    )
                    found[key] = candidate
                elif candidate.attributed_to != "own" and attributed == "own":
                    # Any evidence that this is Jacob's own name wins, so we
                    # never suggest him as an attendee of his own meeting.
                    candidate.attributed_to = "own"

                candidate.patterns.add(pattern_name)
                if key not in seen_in_turn:
                    candidate.count += 1
                    seen_in_turn.add(key)

    candidates = [c for c in found.values() if c.count >= min_count]
    candidates.sort(key=lambda c: (c.attributed_to == "own", -c.count, c.name))
    return candidates


def _attribute(kind: str, channel: str, key: str, own_lower: set[str]) -> str:
    """Work out whether a detected name refers to Jacob or to someone else."""
    if key in own_lower:
        return "own"
    if kind == SELF:
        # On a call, a self-introduction on the mic channel is Jacob, because
        # the mic is only him. In a room recording the same microphone carries
        # everyone, so "I'm Marina" is someone else introducing themselves --
        # exactly the name we most want to keep. Only the configured own_names
        # can rule a name out in that mode, which the check above already did.
        if channel == ROOM:
            return "other"
        return "own" if channel == MIC else "other"
    # ADDRESSED and MENTION always point away from the speaker. On the system
    # channel the target could be Jacob, but that case is already caught by the
    # own_names check above.
    return "other"


def _evidence(text: str, start: int, end: int, window: int = 45) -> str:
    left = max(0, start - window)
    right = min(len(text), end + window)
    snippet = text[left:right].strip()
    if left > 0:
        snippet = "..." + snippet
    if right < len(text):
        snippet = snippet + "..."
    return snippet


def attendee_suggestion(candidates: list[NameCandidate]) -> str:
    """Comma-separated list of other people's names, for pre-filling the popup."""
    return ", ".join(c.name for c in candidates if c.attributed_to == "other")


# ---------------------------------------------------------------------------
# attaching a name to a specific diarized voice
# ---------------------------------------------------------------------------
#
# detect_names answers "which names came up". Once the audio has been
# diarized a stronger question becomes answerable: "which name belongs to
# *this voice*". Two kinds of evidence are strong enough to act on.
#
#   self      the voice said "I'm Marina". The speaker of the turn is Marina.
#   handoff   the previous voice said "over to you, John", and the next voice
#             to speak is therefore probably John.
#
# The handoff rule is what makes Jacob's own example work: he says "I'm Jacob,
# I'll pass to John" on the mic, John answers on another channel, and John's
# voice gets a name from a sentence John never spoke.
#
# Both produce *suggestions*. Nothing here writes a label into a transcript --
# it pre-fills the identification dialog, and a dismissed dialog leaves every
# voice as "Speaker N". Constraint 5 is about what we assert, not what we
# offer.

# A handoff names the person about to speak. Distinct from the ADDRESSED
# patterns above, which merely prove a name was said to someone.
_HANDOFF = re.compile(
    _ci(
        "over to you", "back to you", "handing over to", "handing to",
        "hand over to", "hand it over to", "hand it to", "i'll pass to",
        "i'm going to pass to", "i'm gonna pass to", "going to pass to",
        "gonna pass to", "passing over to", "passing to", "pass it to",
        "pass over to", "take it away", "go ahead", "take us through",
        "why don't you take", "do you want to take", "over to",
    )
    + r"\s*,?\s+"
    + _NAME
)

# How far a handoff's authority reaches. A handoff is answered immediately or
# not at all; without a bound, "over to you, John" would name whoever happened
# to speak next twenty minutes later.
_HANDOFF_MAX_TURNS = 3
_HANDOFF_MAX_GAP_S = 45.0

_SELF_PATTERNS = [p for p in _PATTERNS if p[1] == SELF]

_CONFIDENCE = {"self": 0.9, "handoff": 0.55}


def infer_speaker_names(
    turns: list[Turn],
    own_names: list[str] | None = None,
    own_label: str = "",
) -> list[SpeakerGuess]:
    """Guess which name belongs to each diarized voice.

    Returns at most one guess per speaker, strongest evidence first. A name is
    never guessed for two different voices: if both have a claim, the more
    confident one keeps it and the other stays unnamed, because offering the
    same name twice is worse than offering it once.
    """
    own_lower = {n.strip().lower() for n in (own_names or []) if n.strip()}
    ordered = sorted(
        [t for t in turns if t.text and t.text.strip()], key=lambda t: t.start
    )

    best: dict[str, SpeakerGuess] = {}

    def offer(guess: SpeakerGuess) -> None:
        current = best.get(guess.speaker)
        if current is None or guess.confidence > current.confidence:
            best[guess.speaker] = guess

    for index, turn in enumerate(ordered):
        # --- self-introduction: names the speaker of this very turn ---
        if turn.speaker:
            for _, _, regex in _SELF_PATTERNS:
                for match in regex.finditer(turn.text):
                    raw = match.group(1)
                    key = raw.lower()
                    if key in _STOPWORDS or len(raw) < 2:
                        continue
                    if key in own_lower:
                        # In a room recording Jacob is one of the voices on the
                        # shared microphone, so "I'm Jacob" is the one piece of
                        # evidence that says which voice is his. On the system
                        # channel it would be a remote person who happens to
                        # share his name, so it is ignored there.
                        if turn.channel == ROOM:
                            offer(
                                SpeakerGuess(
                                    speaker=turn.speaker,
                                    name=own_label or raw.capitalize(),
                                    basis="self",
                                    confidence=_CONFIDENCE["self"],
                                    evidence=_evidence(
                                        turn.text, match.start(1), match.end(1)
                                    ),
                                )
                            )
                        continue
                    offer(
                        SpeakerGuess(
                            speaker=turn.speaker,
                            name=raw.capitalize(),
                            basis="self",
                            confidence=_CONFIDENCE["self"],
                            evidence=_evidence(
                                turn.text, match.start(1), match.end(1)
                            ),
                        )
                    )

        # --- handoff: names whoever speaks next ---
        for match in _HANDOFF.finditer(turn.text):
            raw = match.group(1)
            key = raw.lower()
            if key in _STOPWORDS or len(raw) < 2 or key in own_lower:
                continue

            target = _next_voice(ordered, index, turn)
            if target is None:
                continue
            offer(
                SpeakerGuess(
                    speaker=target.speaker,
                    name=raw.capitalize(),
                    basis="handoff",
                    confidence=_CONFIDENCE["handoff"],
                    evidence=_evidence(turn.text, match.start(1), match.end(1)),
                )
            )

    return _resolve_collisions(best)


def _next_voice(ordered: list[Turn], index: int, turn: Turn) -> Turn | None:
    """The next turn by a different, diarized voice, within the handoff window.

    Crosses channels deliberately: a handoff from Jacob's mic to a remote
    speaker is the normal case on a call. Returns None when the next voice
    isn't diarized, because there is no key to attach a name to.
    """
    for offset in range(1, _HANDOFF_MAX_TURNS + 1):
        position = index + offset
        if position >= len(ordered):
            return None
        candidate = ordered[position]
        if candidate.start - turn.end > _HANDOFF_MAX_GAP_S:
            return None
        if candidate.voice == turn.voice:
            continue  # same person still finishing their sentence
        return candidate if candidate.speaker else None
    return None


def _resolve_collisions(best: dict[str, SpeakerGuess]) -> list[SpeakerGuess]:
    """One name per voice, and one voice per name."""
    guesses = sorted(
        best.values(), key=lambda g: (-g.confidence, g.speaker)
    )
    claimed: set[str] = set()
    out: list[SpeakerGuess] = []
    for guess in guesses:
        key = guess.name.lower()
        if key in claimed:
            continue
        claimed.add(key)
        out.append(guess)
    return out


def speaker_guess_map(guesses: list[SpeakerGuess]) -> dict[str, str]:
    """{speaker_key: name} for pre-filling dialogs."""
    return {g.speaker: g.name for g in guesses}
