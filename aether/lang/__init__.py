"""The languages AETHER speaks, and everything that differs between them.

One record per language. Nothing else in the codebase learns what a language *is* -- the TTS client
asks which Rime model and voice, the STT asks which Whisper model and code, the hotel renderer asks
which templates. Adding a third language is a record here plus a renderer, not a search for
`if language ==` across nine files.

**Every value here was verified against the live service, not assumed** (RULES.md R9.5). Rime's
public voice catalogue (`users.rime.ai/data/voices/voice_details.json`, 863 entries, queried
2026-09-08) says:

* `mistv3` speaks `eng`, `fra`, `ger`, `spa` -- **not** Hindi;
* `astra` is `eng` on every model it appears under;
* Hindi lives on `coda`, voices `nadi` (female) and `taru` (male).

So Hindi is not a parameter change on the English voice. It is a different model **and** a different
voice, which is why this file pairs them and why one WebSocket cannot serve both -- `speaker`,
`modelId` and `lang` are baked into the `/ws3` connect URL.

**Rime deleted an entire model between 2026-09-08 and 2026-09-09**, and it was the one this file
originally named. The catalogue went from 863 voices to 594; `arcana` disappeared with all 269 of
its voices, including the Hindi `anaya`, `anil` and `arya`, and Hebrew vanished from the service
altogether. `arcana/anaya` *still answered* over `/ws3` after being delisted -- which is precisely
the trap: an undocumented endpoint that merely happens to respond is exactly what RIME_EVIDENCE.md
Part 2 congratulated this project on avoiding when `mistv3` was verified. Hindi therefore moved to
`coda`, which is catalogued.

`nadi` over `taru` is a **grammatical** choice, not only a preference. Hindi marks gender on the
verb, and the templates say `करूँगी` and `सुझाऊँगी` -- feminine, matching a female voice, as `astra`
is on the English side. A male voice speaking those forms is a mistake any Hindi speaker hears
immediately, so switching to `taru` means editing the templates, not just this record.

**Latency is deliberately NOT quoted here any more.** The figures that were (`anaya` 1355-1716 ms)
were measured on 2026-09-08; on 2026-09-09 the whole service was about four times slower, English
included (`mistv3/astra` warm went from 382-508 ms to a 1928 ms median), so a Hindi/English
comparison taken that day says more about Rime's day than about either voice. RIME_EVIDENCE.md
Part 1c carries both sets with their dates. What survives the noise: **Hindi is materially slower to
first audio than English on this provider**, which is why the switch is entered only when a caller
asks for it -- an English call never pays for it.
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
    rime_model="coda",
    # Female, to agree with the feminine verb forms in `aether/hotel/tools_hi.py` and with `astra`
    # on the English side. `taru` is the other catalogued Hindi voice and is male.
    rime_voice="nadi",
    # The multilingual sibling of `base.en`. Same size, so the memory cost of holding both is
    # modest, and it is loaded lazily -- an English-only call never pays for it.
    whisper_model="base",
    whisper_code="hi",
)

SPANISH = Language(
    code="spa",
    name="Spanish",
    endonym="Español",
    # Spanish is one of the four `mistv3` speaks, so it runs on the same fast model as English --
    # unlike Hindi, which exists only on `coda`. `isa` is flagship, female (agreeing with the
    # feminine verb-free templates and with `astra` and `nadi` for a consistent persona), Mexican.
    rime_model="mistv3",
    rime_voice="isa",
    whisper_model="base",
    whisper_code="es",
)

LANGUAGES: dict[str, Language] = {lang.code: lang for lang in (ENGLISH, HINDI, SPANISH)}

DEFAULT = ENGLISH

# THE FIRST THING A CALLER HEARS, before any hotel greeting.
#
# A call opens by asking which language, and nothing else happens until the caller answers. That
# ordering is the point: the hotel greeting itself has to be spoken in *some* language, so greeting
# first and asking afterwards would have already made the choice for them -- and a Hindi-speaking
# caller would have to sit through an English greeting to be offered Hindi.
#
# English, because it is the only language a caller is likely to recognise the question in before
# they have told us anything, and because `base.en` is what the recogniser is running until they do.
SELECT_PROMPT = ("Welcome to AETHER, your hotel manager. "
                 "Which language would you prefer: English, Hindi, or Spanish?")

# Spoken once the caller has chosen, in the language they chose. This is the *real* hotel greeting,
# the one the product used to open with.
HOTEL_GREETING = {
    "eng": "You've reached AETHER, the hotel's manager. How may I help you?",
    "hin": "नमस्ते, आप AETHER से बात कर रहे हैं, होटल की मैनेजर। मैं आपकी क्या मदद कर सकती हूँ?",
    "spa": "Ha llamado a AETHER, la gerente del hotel. ¿En qué puedo ayudarle?",
}

# Asked again only while the caller has not yet chosen -- never once they have. Repeating the
# question every turn would be its own failure mode.
SELECT_RETRY = ("Sorry, I did not catch that. "
                "Would you prefer English, Hindi, or Spanish?")

# What AETHER says the moment it switches, in the language it has switched TO. Short on purpose: the
# caller asked for a language, not a speech, and the acknowledgement is also the proof that the new
# voice works.
ACKNOWLEDGEMENT = {
    "hin": "जी हाँ, मैं हिन्दी में बात करूँगी। बताइए, मैं आपकी क्या मदद कर सकती हूँ?",
    "spa": "Por supuesto, continuaré en español. ¿En qué puedo ayudarle?",
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
    (r"spanish", "spa"),
    (r"espanol", "spa"),
    (r"español", "spa"),
    (r"castellano", "spa"),
)

# "I don't speak Hindi" and "no Hindi please" are refusals, not requests. Without this, naming the
# language in order to decline it would switch into it.
_NEGATIONS = ("dont", "don t", "do not", "cannot", "can not", "cant", "can t",
              "no ", "not ", "without", "never")

# Asking about languages WITHOUT naming one -- "can we switch language", "what languages do you
# speak". The answer is the list, not a switch, because switching to an unnamed language is a guess.
_ASKS_FOR_OPTIONS = (
    "what language", "which language", "what languages", "which languages",
    "switch language", "switch the language", "change language", "change the language",
    "switch languages", "change languages", "other language", "another language",
    "languages do you", "languages can you", "language options",
)


# What each language is CALLED in each language. Offering "English, Hindi या Spanish" is the kind
# of half-localisation that tells a caller the Hindi is machine-made -- in Hindi the languages are
# अंग्रेज़ी, हिन्दी and स्पेनिश. A 3x3 table is small; the alternative is being wrong in two of three
# languages every time the list is read.
_NAME_IN: dict[str, dict[str, str]] = {
    "eng": {"eng": "English", "hin": "Hindi", "spa": "Spanish"},
    "hin": {"eng": "अंग्रेज़ी", "hin": "हिन्दी", "spa": "स्पेनिश"},
    "spa": {"eng": "inglés", "hin": "hindi", "spa": "español"},
}


def name_of(named: Language, *, spoken_in: Language | None = None) -> str:
    """What `named` is called in the language currently being spoken."""
    speaking = getattr(spoken_in, "code", None) or DEFAULT.code
    return _NAME_IN.get(speaking, _NAME_IN["eng"]).get(named.code, named.name)


def spoken_language_list(language: Language | None = None) -> str:
    """"English, Hindi or Spanish" -- in the language currently being spoken.

    Built from `LANGUAGES` rather than written out, so adding a fourth language updates the
    greeting, the switch offer and the console together instead of in three places that drift.
    """
    names = [name_of(lang, spoken_in=language) for lang in LANGUAGES.values()]
    joiner = {"hin": "या", "spa": "o"}.get(getattr(language, "code", None), "or")
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" {joiner} {names[-1]}"


def language_offer(language: Language | None = None) -> str:
    """What AETHER says when asked which languages it speaks, in the language it is speaking."""
    listed = spoken_language_list(language)
    code = getattr(language, "code", None)
    if code == "hin":
        return f"मैं {listed} में बात कर सकती हूँ। आप कौन सी भाषा पसंद करेंगे?"
    if code == "spa":
        return f"Puedo atenderle en {listed}. ¿Cuál prefiere?"
    return f"I can help you in {listed}. Which would you prefer?"


def asks_for_options(text: str) -> bool:
    """Is this asking WHICH languages exist, rather than asking for one of them?"""
    spoken = " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())
    return any(phrase in spoken for phrase in _ASKS_FOR_OPTIONS)


def names_language(text: str) -> Language | None:
    """The language this sentence names, or None. **Regardless of what is already active.**

    Separate from `detect_switch` because the two questions genuinely differ. Mid-conversation,
    "Hindi" while already speaking Hindi is an ordinary utterance and must not re-switch. But during
    language *selection* the caller is answering a question, and "English" -- which happens to match
    the default the recogniser is running -- is a real, deliberate choice that has to be accepted.
    Folding the two together made a caller who chose English get asked again forever.

    A sentence that names a language only to refuse it is still not a request for it.
    """
    spoken = " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())
    if not spoken or any(neg in spoken for neg in _NEGATIONS):
        return None
    for pattern, code in _SWITCH_WORDS:
        if re.search(rf"\b{pattern}\b", spoken):
            return LANGUAGES[code]
    return None


def detect_switch(text: str, current: Language) -> Language | None:
    """The language this sentence is asking to switch TO, or None if it is not asking.

    Returns None when the request names the language already in use, so repeating "Hindi" mid-Hindi
    conversation is an ordinary utterance rather than a pointless re-switch.
    """
    named = names_language(text)
    if named is None or named.code == current.code:
        return None
    return named


__all__ = ["ACKNOWLEDGEMENT", "DEFAULT", "ENGLISH", "HINDI", "HOTEL_GREETING", "LANGUAGES",
           "SELECT_PROMPT", "SELECT_RETRY", "SPANISH", "Language",
           "asks_for_options", "by_code", "detect_switch", "language_offer", "name_of",
           "names_language",
           "spoken_language_list"]


def by_code(code: str | None) -> Language:
    """Look up a language, falling back to English rather than raising.

    A bad code must never take a call down: the caller is on a phone, and the worst acceptable
    outcome of a misconfiguration is that they are answered in English.
    """
    if not code:
        return DEFAULT
    return LANGUAGES.get(str(code).strip().lower(), DEFAULT)
