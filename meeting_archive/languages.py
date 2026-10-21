"""Meeting language. The app stays English. A recording can be something else.

The choice is stored on the meeting. A later rebuild uses that, and does not
guess again. Search is unchanged: the index is still the English one.
"""

from __future__ import annotations

import re

# The menu offers these. Whisper accepts the codes. "auto" means decide from
# the audio and write down what was decided.
MEETING_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("auto", "Decide from the audio"),
    ("ja", "Japanese"),
    ("zh", "Chinese"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("ko", "Korean"),
)

_ALIASES = {
    "en": "en",
    "english": "en",
    "auto": "auto",
    "decide": "auto",
    "detect": "auto",
    "ja": "ja",
    "japanese": "ja",
    "zh": "zh",
    "chinese": "zh",
    "mandarin": "zh",
    "es": "es",
    "spanish": "es",
    "fr": "fr",
    "french": "fr",
    "de": "de",
    "german": "de",
    "ko": "ko",
    "korean": "ko",
}

# Whisper can decide on a language the menu does not list. The summary still
# needs a name for it. An unknown code gets a generic instruction instead.
_EXTRA_NAMES = {
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "sv": "Swedish",
    "pl": "Polish",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "th": "Thai",
}

_CODE = re.compile(r"^[a-z]{2,3}$")


def is_meeting_language(value: str | None) -> bool:
    raw = str(value or "").strip().lower().replace("_", "-")
    return raw in _ALIASES


def normalise_language(value: str | None, *, default: str = "en", allow_other: bool = False) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if not raw:
        return default
    if raw in _ALIASES:
        return _ALIASES[raw]
    if allow_other and _CODE.match(raw):
        return raw
    return default


def language_label(code: str) -> str:
    for key, label in MEETING_LANGUAGES:
        if key == code:
            return label
    return _EXTRA_NAMES.get(code, "")


def uses_english_names(code: str) -> bool:
    """English name patterns. Any other language would invent names from them."""
    return normalise_language(code) == "en"


def summary_instruction(code: str) -> str:
    """Empty for English, so those prompts stay exactly as they are."""
    code = normalise_language(code, allow_other=True)
    if code in {"en", "auto"}:
        return ""
    name = language_label(code)
    if name and name != "Decide from the audio":
        return f"Write the summary in {name}. Leave names as they were spoken."
    return "Write the summary in the language of the transcript. Leave names as they were spoken."


def resolve_spoken(requested: str, detected: str) -> str:
    """The language to store. A chosen language wins. Decide keeps what Whisper heard."""
    requested = normalise_language(requested)
    if requested != "auto":
        return requested
    heard = normalise_language(detected, default="", allow_other=True)
    if heard and heard != "auto":
        return heard
    return "auto"
