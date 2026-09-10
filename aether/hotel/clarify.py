"""When the router does not understand: offer the nearest thing, or ask for a repeat.

TWO FAILURES THIS REPLACES, both visible in `evidence/demo-run.jsonl`.

The recogniser returned *"How much is deluxe heat?"* for "deluxe suite", and *"How much is a daily
speed?"* for something nobody can now reconstruct. The router matched neither, so both went to the
model, which improvised politely and changed the subject. A caller who said a real thing clearly
was told, in effect, that they had said something else -- and had no idea which word had been
misheard, so their next attempt was a guess.

What a person on a hotel desk does instead is one of exactly two things:

* **"Did you mean the Deluxe King?"** when the sound was close to something real. The caller says
  yes and gets their answer, one turn later, with no repetition.
* **"Sorry, could you say that again?"** when it was not close to anything. Which is a *useful*
  reply: it tells the caller the line, not the hotel, is the problem.

WHY THIS IS NOT FUZZY ROUTING. Nothing here answers a question. It proposes a candidate and waits
for a yes -- so a wrong guess costs one turn, and can never put a wrong price into the caller's ear.
That distinction is the whole reason a similarity threshold is acceptable at all in a system whose
central claim is that hotel facts are deterministic: `route()` stays exact, and this sits beside it.

WHY NOT SEND IT TO THE MODEL EITHER. The model is still the right answer for a real question the
database cannot serve -- R8b.3 -- and that path is untouched. This fires only on input that looks
like a RECOGNITION failure rather than a question, and the two are told apart by evidence rather
than by vibes: a near-miss against a known name, or a fragment too short to be a question at all.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from ._foreign import to_router_language
from ._text import normalise as _normalise
from .db import HotelStore

_STORE = HotelStore()

# How close a heard phrase must be to a real name before AETHER will offer it.
#
# It errs HIGH on purpose. The alternative to a suggestion is a repeat request, which is never
# wrong -- only slower -- so a missed suggestion costs one turn and a wrong one costs the caller's
# trust. Dropping to 0.68 makes unrelated dishes start scoring against each other.
#
# The mishearings a high threshold cannot reach are covered by anchor words instead (see
# `_anchors`), which is a sharper instrument than a looser threshold: "deluxe heat" scores 0.64
# against "deluxe king" and no safe threshold would ever catch it, but "deluxe" names exactly one
# room in this hotel.
_THRESHOLD = 0.72

# ONE content word, and nothing else. Two words is already enough to be a real question the model
# can field politely -- "another question", "any vacancies", "room service" -- and intercepting
# those would make AETHER ask people to repeat themselves for no reason. This started at two words
# and was narrowed after it swallowed a dozen perfectly ordinary fixtures across the suite, which
# is the same signal a real caller would have given, arriving earlier and for free.
_TOO_SHORT_TO_BE_A_QUESTION = 2

# Words that carry no content on their own. A transcript made only of these is noise, however long.
_FILLER = frozenset({
    "um", "uh", "er", "erm", "hmm", "mm", "ah", "oh", "so", "well", "like", "just",
    "the", "a", "an", "is", "it", "and", "or", "you", "i", "me", "my", "we", "to", "of",
})


@dataclass(frozen=True)
class Suggestion:
    """A candidate AETHER is willing to OFFER, never to assume."""

    phrase: str          # what to say back: "Deluxe King"
    heard: str           # what the recogniser produced: "deluxe heat"
    tool: str            # the tool a yes would run
    params: dict         # ...with these arguments
    score: float


def _candidates() -> list[tuple[str, str, dict]]:
    """Everything a caller could plausibly be naming, as (spoken name, tool, params).

    Built from the database, so a dish added tomorrow is suggestible without touching this file.
    """
    out: list[tuple[str, str, dict]] = []
    for item in _STORE.menu():
        out.append((item.name, "price_of", {"dish": item.name.lower()}))
    for room_type in _STORE.room_types():
        out.append((room_type.name, "room_price", {"room_type": room_type.name}))
    for category in _STORE.categories():
        out.append((category, "list_category", {"category": category}))
    for service in _STORE.services():
        out.append((service.name, "service_hours", {"service": service.name}))
    for policy in _STORE.policies():
        out.append((policy.topic.replace("_", " "), "hotel_policy", {"topic": policy.topic}))
    return out


_CANDIDATE_CACHE: list[tuple[str, str, dict]] | None = None


def _all_candidates() -> list[tuple[str, str, dict]]:
    global _CANDIDATE_CACHE
    if _CANDIDATE_CACHE is None:
        _CANDIDATE_CACHE = _candidates()
    return _CANDIDATE_CACHE


def _phrases(spoken: str) -> list[str]:
    """Every one-to-four-word run in the sentence.

    A mishearing is local: "how much is deluxe heat" is a perfectly good sentence with one broken
    noun phrase in it. Comparing the WHOLE sentence to "Deluxe King" scores far too low, so the
    comparison has to be against the part that could be a name.
    """
    words = [w for w in spoken.split() if w not in _FILLER]
    runs: list[str] = []
    for size in (4, 3, 2, 1):
        for i in range(len(words) - size + 1):
            runs.append(" ".join(words[i:i + size]))
    return runs


def _anchors() -> dict[str, tuple[str, str, dict]]:
    """Words that belong to exactly one thing in this hotel, and what they point at.

    THE CASE THAT MADE THIS NECESSARY. The recogniser heard "deluxe heat" for "Deluxe King" on a
    real call. Whole-phrase similarity scores that pair at 0.64 -- the two halves are "deluxe" and
    a word with no letters in common -- so a threshold high enough to be safe can never catch it.

    But "deluxe" names exactly one room type in this hotel, and a caller who says it is telling you
    a great deal. A word that is unique across the entire vocabulary is a strong anchor; a word
    like "paneer", which two dishes share, is not an anchor at all and deliberately produces no
    suggestion, because guessing between two real dishes is worse than asking.
    """
    seen: dict[str, list[tuple[str, str, dict]]] = {}
    for name, tool, params in _all_candidates():
        for word in name.lower().split():
            if len(word) >= 5:
                seen.setdefault(word, []).append((name, tool, params))
    return {word: rows[0] for word, rows in seen.items() if len(rows) == 1}


_ANCHOR_CACHE: dict[str, tuple[str, str, dict]] | None = None


def _all_anchors() -> dict[str, tuple[str, str, dict]]:
    global _ANCHOR_CACHE
    if _ANCHOR_CACHE is None:
        _ANCHOR_CACHE = _anchors()
    return _ANCHOR_CACHE


def nearest(text: str) -> Suggestion | None:
    """The closest real thing to what was heard, or None if nothing is close enough.

    Two ways in, and a suggestion needs only one of them: overall similarity above the threshold,
    or a word that belongs to exactly one thing in the hotel. Ambiguity yields None in both paths,
    because the fallback -- asking the caller to repeat -- is never wrong, only slower.
    """
    spoken = to_router_language(_normalise(text))
    if not spoken:
        return None

    best: Suggestion | None = None
    runner_up = 0.0
    for phrase in _phrases(spoken):
        for name, tool, params in _all_candidates():
            score = difflib.SequenceMatcher(None, phrase, name.lower()).ratio()
            if score < _THRESHOLD:
                continue
            if best is None or score > best.score:
                if best is not None and best.phrase != name:
                    runner_up = best.score
                best = Suggestion(phrase=name, heard=phrase, tool=tool,
                                  params=params, score=round(score, 3))
            elif name != best.phrase:
                runner_up = max(runner_up, score)

    # Two different real things scoring almost the same is not a near-miss, it is a coin toss.
    if best is not None and runner_up and best.score - runner_up < 0.05:
        return None
    if best is not None:
        return best

    # No overall match. Fall back to a distinctive word.
    anchors = _all_anchors()
    for word in spoken.split():
        row = anchors.get(word)
        if row is not None:
            name, tool, params = row
            return Suggestion(phrase=name, heard=word, tool=tool, params=params, score=0.0)
    return None


# Short things people say that are NOT failures to be heard. A one-word turn is usually a speech
# act, not noise, and answering "hello" with "could you say that again?" is the single most robotic
# thing this feature could do. Three languages, because the caller may already have switched.
_SPEECH_ACTS = frozenset({
    "hello", "hallo", "hi", "hey", "yes", "yeah", "no", "ok", "okay", "sure", "please",
    "thanks", "thank", "sorry", "bye", "goodbye", "morning", "afternoon", "evening", "help",
    "wait", "hold", "stop", "nothing", "nevermind",
    "namaste", "namaskar", "haan", "nahi", "shukriya", "dhanyavaad", "theek", "ji",
    "नमस्ते", "हाँ", "नहीं", "धन्यवाद", "ठीक", "जी", "माफ़", "रुकिए",
    "hola", "gracias", "adios", "adiós", "buenos", "buenas", "vale", "perdon", "perdón",
    "espere", "nada",
})

# What a recogniser emits when it heard sound but no speech. Whisper is known to hallucinate
# "thanks for watching" and "bye" over silence, and both were in this set for an afternoon -- which
# meant a caller who simply said "thanks" was asked to repeat themselves. Ambiguous politeness is
# now a speech act, not noise: mistaking a real word for noise is worse than the reverse, because
# the caller hears the machine fail to understand a word a child would.
_NOISE = frozenset({"mm", "mhm", "hm", "uh", "um", "er", "erm"})


def sounds_like_a_recognition_failure(text: str) -> bool:
    """Whether this is better answered with "say that again" than with a model reply.

    DELIBERATELY NARROW, and narrowed once already. The first version asked only whether the
    fragment was short and carried no hotel word, which caught "hello" -- and answering a greeting
    with "could you say that again?" is the most robotic thing this feature could possibly do.

    Three conditions now, all of which must hold: nothing but filler and noise, or a fragment of at
    most two content words that is neither a speech act nor anything the hotel knows about. A real
    question the database cannot serve -- "what is your star rating" -- still reaches the model,
    which is where R8b.3 says it belongs.
    """
    spoken = _normalise(text)
    if not spoken:
        return True

    words = spoken.split()
    if all(w in _FILLER or w in _NOISE for w in words):
        return True

    content = [w for w in words if w not in _FILLER]
    if not content:
        return True
    if any(w in _SPEECH_ACTS for w in content):
        return False
    return len(content) < _TOO_SHORT_TO_BE_A_QUESTION and not _has_content(content)


# Words that are about the hotel even though they name no particular thing in it. A caller who says
# just "menu" is asking for the menu -- a real call on 2026-09-10 got "Sorry, I did not catch that"
# for exactly that word, because this check only knew the hotel's proper names ("Chicken Kebab",
# "Deluxe King") and "menu" is not one of them. Asking someone to repeat the clearest word they said
# is the worst version of this feature.
_HOTEL_WORDS = frozenset({
    "menu", "menus", "food", "dish", "dishes", "meal", "meals", "eat", "drink", "drinks",
    "breakfast", "lunch", "dinner", "starter", "starters", "main", "mains", "dessert", "desserts",
    "room", "rooms", "suite", "suites", "stay", "night", "nights", "table", "tables",
    "price", "prices", "rate", "rates", "cost", "bill", "book", "booking", "bookings",
    "reserve", "reservation", "reservations", "cancel", "order", "service", "services",
    "check", "checkin", "checkout", "hotel", "pool", "gym", "spa", "parking", "wifi", "taxi",
    "reception", "housekeeping", "laundry", "language", "english", "hindi", "spanish",
})


def _has_content(words: list[str]) -> bool:
    """Whether any word is a thing this hotel knows about -- by name, or by what it is."""
    if any(word in _HOTEL_WORDS for word in words):
        return True
    joined = " ".join(words)
    for name, _tool, _params in _all_candidates():
        if name.lower() in joined:
            return True
    return False




# --- confirming a suggestion ---------------------------------------------------------------------

# "Yes" in the three languages AETHER speaks, plus what the recogniser tends to return for each.
# Whole words, because "no" inside "know" and "si" inside "sister" are exactly the mistake the
# router spent a whole commit removing.
_YES = ("yes", "yeah", "yep", "yup", "correct", "right", "that one", "please", "sure", "ok",
        "okay", "exactly", "haan", "haa", "ji", "ji haan", "हाँ", "जी", "जी हाँ", "सही",
        "si", "sí", "claro", "eso", "exacto", "correcto", "vale")

_NO = ("no", "nope", "nah", "wrong", "not that", "different", "nahi", "नहीं", "नही", "ना",
       "no gracias", "incorrecto", "otra")


def _said(spoken: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", spoken) for w in words)


def confirms(text: str) -> bool | None:
    """True for yes, False for no, None for anything else.

    None matters as much as the other two: a caller who answers "did you mean the Deluxe King?"
    with "how much is the chicken kebab" has moved on, and the pending suggestion must be dropped
    rather than argued with.
    """
    spoken = _normalise(text)
    if not spoken:
        return None
    yes, no = _said(spoken, _YES), _said(spoken, _NO)
    if yes and not no:
        return True
    if no and not yes:
        return False
    return None
