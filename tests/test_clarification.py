"""Offering the nearest thing, and asking for a repeat, instead of changing the subject.

Both failures are in `evidence/demo-run.jsonl`. The recogniser returned "How much is deluxe heat?"
and "How much is a daily speed?"; neither routed, both went to the model, and the model improvised
politely and moved on. The caller had said something real, was answered about something else, and
had no way to tell which word had been misheard.

The rule that keeps this safe is that **nothing here answers a question**. A suggestion is a
question back, so a wrong guess costs one turn and can never put a wrong price in a caller's ear.
That is what makes a similarity threshold acceptable at all in a system whose central claim is
determinism -- and it is the first thing these tests pin.
"""

from __future__ import annotations

import pytest

from aether.events import EventType
from aether.hotel.clarify import (
    confirms,
    nearest,
    sounds_like_a_recognition_failure,
)
from aether.lang import DID_YOU_MEAN, ENGLISH, HINDI, NEVER_MIND, SAY_AGAIN, SPANISH
from tests.test_menu_routing import AUDIO, build


# --- suggesting ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("heard", "expected"), [
    ("how much is the chicken kebap", "Chicken Kebab"),
    ("do you have panir tika", "Paneer Tikka"),
    ("how much is the executive suit", "Executive Suite"),
    ("what about the swiming pool", "swimming pool"),
    # The one from the recorded call. Whole-phrase similarity scores this at 0.64 and no safe
    # threshold reaches it -- it is caught by "deluxe" naming exactly one room in this hotel.
    ("how much is deluxe heat", "Deluxe King"),
])
def test_a_mishearing_is_offered_back_as_the_real_name(heard: str, expected: str) -> None:
    suggestion = nearest(heard)
    assert suggestion is not None, f"{heard!r} produced no suggestion"
    assert suggestion.phrase == expected


@pytest.mark.parametrize("heard", [
    "what is your star rating",          # a real question, just not one the database holds
    "is there a temple nearby",
    "how much is a daily speed",         # nothing close to anything: ask for a repeat instead
    "how much is paneer something",      # "paneer" names TWO dishes -- guessing between them is worse
])
def test_nothing_is_offered_when_nothing_is_close_enough(heard: str) -> None:
    assert nearest(heard) is None


def test_an_ambiguous_word_produces_no_suggestion() -> None:
    """"paneer" is in two dishes. A coin toss dressed as a suggestion is worse than a repeat
    request, because the caller cannot tell it was a coin toss."""
    assert nearest("i would like the paneer") is None


def test_the_suggestion_carries_the_tool_a_yes_would_run() -> None:
    """A suggestion is not a sentence, it is a deferred decision."""
    suggestion = nearest("how much is the chicken kebap")
    assert suggestion is not None
    assert suggestion.tool == "price_of"
    assert suggestion.params == {"dish": "chicken kebab"}


# --- asking for a repeat -------------------------------------------------------------------------

@pytest.mark.parametrize("said", ["", "   ", "um", "hmm uh", "grr", "the fish", "q"])
def test_unintelligible_input_asks_for_a_repeat(said: str) -> None:
    assert sounds_like_a_recognition_failure(said)


@pytest.mark.parametrize("said", [
    "hello", "hi there", "thanks", "bye", "namaste", "hola", "नमस्ते",
    "what is your star rating", "is there a temple nearby", "another question",
])
def test_real_speech_is_never_treated_as_a_mishearing(said: str) -> None:
    """The eager-direction failure, which is the one that would make AETHER feel broken.

    Answering "hello" with "could you say that again?" is the most robotic thing this feature could
    do, and the first version of the heuristic did exactly that. Politeness is a speech act, not
    noise -- "thanks" was briefly classified as noise because Whisper hallucinates it over silence,
    which meant a courteous caller got asked to repeat themselves.
    """
    assert not sounds_like_a_recognition_failure(said)


# --- confirming ----------------------------------------------------------------------------------

@pytest.mark.parametrize(("said", "expected"), [
    ("yes", True), ("yes please", True), ("yeah that one", True), ("correct", True),
    ("haan", True), ("हाँ", True), ("ji haan", True),
    ("si", True), ("sí", True), ("claro", True),
    ("no", False), ("nope", False), ("no that is wrong", False),
    ("nahi", False), ("नहीं", False), ("no gracias", False),
    ("how much is the chicken kebab", None), ("", None),
])
def test_yes_and_no_are_understood_in_every_language(said: str, expected: bool | None) -> None:
    assert confirms(said) is expected


def test_devanagari_no_survives_normalisation() -> None:
    """"नहीं" normalised to "नह" while `clarify` had its own copy of `normalise` that stripped
    combining marks -- so a Hindi caller's "no" was neither a yes nor a no. One shared `_text`
    module now, which is why this is a regression test and not a unit test."""
    assert confirms("नहीं") is False


# --- end to end through the real pipeline ---------------------------------------------------------

def test_a_mishearing_is_answered_with_a_question_not_a_guess(monkeypatch) -> None:
    spike, _trace, rime, llm = build(monkeypatch, "how much is the chicken kebap")
    spike.handle_utterance(AUDIO, 0.0)

    assert rime.spoken, "nothing was said at all"
    assert rime.spoken[-1] == DID_YOU_MEAN["eng"].format("Chicken Kebab")
    assert llm.calls == [], "a mishearing must not reach the model"
    # And crucially: no price was spoken. The caller has been asked, not told.
    assert "rupees" not in rime.spoken[-1]


def test_a_yes_then_answers_the_question(monkeypatch) -> None:
    from aether.hotel import HotelStore, say_price

    spike, _trace, rime, _llm = build(monkeypatch, "how much is the chicken kebap")
    spike.handle_utterance(AUDIO, 0.0)

    monkeypatch.setattr(spike.stt, "text", "yes please", raising=False)
    spike.handle_utterance(AUDIO, 0.0)

    item = HotelStore().find_item("chicken kebab")
    assert say_price(item.price) in rime.spoken[-1], f"the yes was not honoured: {rime.spoken[-1]!r}"


def test_a_no_drops_the_offer_without_guessing_again(monkeypatch) -> None:
    """A second guess after a rejected first is how a caller ends up arguing with a machine."""
    spike, _trace, rime, _llm = build(monkeypatch, "how much is the chicken kebap")
    spike.handle_utterance(AUDIO, 0.0)

    monkeypatch.setattr(spike.stt, "text", "no", raising=False)
    spike.handle_utterance(AUDIO, 0.0)

    assert rime.spoken[-1] == NEVER_MIND["eng"]
    assert spike.suggestion is None


def test_moving_on_drops_the_offer_rather_than_arguing_with_it(monkeypatch) -> None:
    """The caller who answers "did you mean X?" with a different question entirely."""
    from aether.hotel import HotelStore

    spike, _trace, rime, _llm = build(monkeypatch, "how much is the chicken kebap")
    spike.handle_utterance(AUDIO, 0.0)

    monkeypatch.setattr(spike.stt, "text", "what starters do you have", raising=False)
    spike.handle_utterance(AUDIO, 0.0)

    for item in HotelStore().in_category("starters"):
        assert item.name in rime.spoken[-1]
    assert spike.suggestion is None


def test_an_unintelligible_turn_asks_for_a_repeat_rather_than_changing_the_subject(monkeypatch):
    spike, _trace, rime, llm = build(monkeypatch, "grr")
    spike.handle_utterance(AUDIO, 0.0)

    assert rime.spoken[-1] == SAY_AGAIN["eng"]
    assert llm.calls == [], "unintelligible audio must not become a model prompt"


def test_a_real_question_the_database_cannot_answer_still_reaches_the_model(monkeypatch) -> None:
    """R8b.3 is untouched. This feature intercepts recognition failures, not hard questions."""
    spike, _trace, _rime, llm = build(monkeypatch, "what is your star rating")
    spike.handle_utterance(AUDIO, 0.0)
    assert llm.calls == ["what is your star rating"]


# --- the invariant, applied to the offer ------------------------------------------------------

def test_an_interrupted_offer_cannot_be_confirmed_later(monkeypatch) -> None:
    """The caller never heard "did you mean the Chicken Kebab?", so their next "yes" is not an
    answer to it. Same rule as history and the conversational subject, same reason."""
    spike, trace, rime, _llm = build(monkeypatch, "how much is the chicken kebap")

    # Fenced while the OFFER is being spoken, not during a tool call -- the clarification path runs
    # no tool, which is exactly why it needed its own test. The caller talks over "did you mean...".
    original = rime.speak

    def interrupt_then_speak(text, **kwargs):
        spike.barge.on_speech_onset()
        spike.barge.on_voiced_progress(400.0)
        return original(text, **kwargs)

    monkeypatch.setattr(rime, "speak", interrupt_then_speak)
    spike.handle_utterance(AUDIO, 0.0)

    assert spike.suggestion is None, "an offer the caller never heard became live"
    assert trace.all(EventType.RESULT_LEAKED) == []

    # And a later "yes" therefore answers nothing, rather than confirming an unheard offer.
    monkeypatch.setattr(rime, "speak", original)
    monkeypatch.setattr(spike.stt, "text", "yes please", raising=False)
    spike.handle_utterance(AUDIO, 0.0)
    assert "rupees" not in (rime.spoken[-1] if rime.spoken else ""), (
        "a yes confirmed an offer the caller never heard"
    )


@pytest.mark.parametrize("language", [ENGLISH, HINDI, SPANISH], ids=lambda lang: lang.code)
def test_every_language_has_all_three_sentences(language) -> None:
    """R8b.6: an English-only capability is a defect. Asking someone to repeat themselves in a
    language they did not choose is a particularly bad one."""
    for table in (DID_YOU_MEAN, SAY_AGAIN, NEVER_MIND):
        assert language.code in table, f"{language.code} is missing a clarification sentence"
        assert table[language.code].strip(), f"{language.code} has an empty sentence"
    assert "{}" in DID_YOU_MEAN[language.code], "the suggestion has nowhere to go"


@pytest.mark.parametrize("language", [HINDI, SPANISH], ids=lambda lang: lang.code)
def test_the_clarification_is_spoken_in_the_callers_language(language, monkeypatch) -> None:
    spike, _trace, rime, _llm = build(monkeypatch, "grr")
    spike._set_language(language)
    spike.handle_utterance(AUDIO, 0.0)
    assert rime.spoken[-1] == SAY_AGAIN[language.code]
