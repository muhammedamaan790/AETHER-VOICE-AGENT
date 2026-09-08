"""The languages AETHER speaks, and everything that differs between them.

One record per language. Nothing else in the codebase learns what a language *is* -- the TTS client
asks which Rime model and voice, the STT asks which Whisper model and code, the hotel renderer asks
which templates. Adding a third language is a record here plus a renderer, not a search for
`if language ==` across nine files.

**Every value here was verified against the live service, not assumed** (RULES.md R9.5). Rime's
public voice catalogue (`users.rime.ai/data/voices/voice_details.json`, 863 entries, queried
2026-09-08) says:

* `mistv3` speaks `eng`, `fra`, `ger`, `spa` -- **not** Hindi;
* `astra` is `eng` on every model it appears under, including `arcana`;
* Hindi lives on `arcana` (`anaya`, `anil`, `arya`) and `coda` (`nadi`, `taru`).

So Hindi is not a parameter change on the English voice. It is a different model **and** a different
voice, which is why this file pairs them and why one WebSocket cannot serve both -- `speaker`,
`modelId` and `lang` are baked into the `/ws3` connect URL.

`anaya` was chosen by measurement over `/ws3`, warm first-audio, three utterances each
(`scripts/verify_rime_hindi.py`, RIME_EVIDENCE.md Part 1c):

    arcana/anaya   1355-1716 ms      <- chosen
    coda/taru      1585-2044 ms
    coda/nadi      1861-1910 ms
    arcana/arya    1948-2764 ms
    mistv3/astra    382- 508 ms      (English, for scale)

**Hindi costs roughly 3x the first audio of English, and that is recorded rather than hidden.** It
is the honest price of the second language on this provider, and it is why the switch is entered
only when a caller asks for it: an English call never pays it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    """One language, and every service setting that changes with it.

    Frozen: a language is a fact about the world, not session state. What changes at runtime is
    *which* record is active, held by the turn loop.
    """

    code: str                 # what Rime calls it, and what the trace records
    name: str                 # for the console and for logs
    endonym: str              # what the language calls itself, for the console
    rime_model: str
    rime_voice: str
    whisper_model: str        # `base.en` is English-only and cannot transcribe anything else
    whisper_code: str         # what faster-whisper calls it

    @property
    def rime(self) -> tuple[str, str]:
        return self.rime_model, self.rime_voice


ENGLISH = Language(
    code="eng",
    name="English",
    endonym="English",
    rime_model="mistv3",
    rime_voice="astra",
    # `base.en` is a monolingual model. It is kept for English precisely because it is monolingual:
    # it is faster and more accurate on English than the multilingual model of the same size, and
    # the recorded demo runs on it. Nothing about Hindi is allowed to change this line.
    whisper_model="base.en",
    whisper_code="en",
)

HINDI = Language(
    code="hin",
    name="Hindi",
    endonym="हिन्दी",
    rime_model="arcana",
    rime_voice="anaya",
    # The multilingual sibling of `base.en`. Same size, so the memory cost of holding both is
    # modest, and it is loaded lazily -- an English-only call never pays for it.
    whisper_model="base",
    whisper_code="hi",
)

LANGUAGES: dict[str, Language] = {lang.code: lang for lang in (ENGLISH, HINDI)}

DEFAULT = ENGLISH

# What AETHER says the moment it switches, in the language it has switched TO. Short on purpose: the
# caller asked for a language, not a speech, and the acknowledgement is also the proof that the new
# voice works.
ACKNOWLEDGEMENT = {
    "hin": "जी हाँ, मैं हिन्दी में बात करूँगी। बताइए, मैं आपकी क्या मदद कर सकती हूँ?",
    "eng": "Of course, I will continue in English. How may I help you?",
}

# The words that ask for a language. Matched on the **English** transcript, because that is what
# `base.en` produces before any switch has happened -- the recogniser cannot be asked to hear Hindi
# until it has been told to load a Hindi-capable model, so the request itself must survive being
# heard by an English-only model.
#
# UNLIKE the `suit`/`sweet` repair in the router, these spellings are NOT yet backed by traces: no
# call has asked for Hindi yet. They are the obvious renderings, and the greeting deliberately tells
# the caller to say the single word "Hindi" so the common case is the easiest one to hear. When real
# calls produce real mishearings, they belong here, and the ones here that never occur can go.
_SWITCH_WORDS: tuple[tuple[str, str], ...] = (
    (r"hindi", "hin"),
    (r"hindhi", "hin"),
    (r"hindee", "hin"),
    (r"english", "eng"),
    (r"angrezi", "eng"),
)

# "I don't speak Hindi" and "no Hindi please" are refusals, not requests. Without this, naming the
# language in order to decline it would switch into it.
_NEGATIONS = ("dont", "don t", "do not", "cannot", "can not", "cant", "can t",
              "no ", "not ", "without", "never")


def detect_switch(text: str, current: Language) -> Language | None:
    """The language this sentence is asking for, or None if it is not asking.

    Returns None when the request names the language already in use, so repeating "Hindi" mid-Hindi
    conversation is an ordinary utterance rather than a pointless re-switch.
    """
    spoken = " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())
    if not spoken:
        return None

    wanted: str | None = None
    for pattern, code in _SWITCH_WORDS:
        if re.search(rf"\b{pattern}\b", spoken):
            wanted = code
            break
    if wanted is None or wanted == current.code:
        return None

    # A sentence that names the language only to refuse it is not a request for it.
    if any(neg in spoken for neg in _NEGATIONS):
        return None
    return LANGUAGES[wanted]


__all__ = ["ACKNOWLEDGEMENT", "DEFAULT", "ENGLISH", "HINDI", "LANGUAGES", "Language", "by_code",
           "detect_switch"]


def by_code(code: str | None) -> Language:
    """Look up a language, falling back to English rather than raising.

    A bad code must never take a call down: the caller is on a phone, and the worst acceptable
    outcome of a misconfiguration is that they are answered in English.
    """
    if not code:
        return DEFAULT
    return LANGUAGES.get(str(code).strip().lower(), DEFAULT)
