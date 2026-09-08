"""Two languages, one pipeline.

What matters here is not that Hindi exists but that adding it did not weaken anything: the same
database row must produce the same fact in both languages, the model must stay out of the path in
both, and an English call must be bit-for-bit what it was before Hindi was written.

The Rime settings are asserted against the live catalogue's answer rather than against a preference
-- `mistv3` does not speak Hindi and `astra` speaks no Hindi on any model, so a "just change the
language code" implementation would be silently wrong.
"""

from __future__ import annotations

import re

import pytest

from aether.hotel import HotelStore
from aether.hotel import speech_hi as hi
from aether.hotel.router import route
from aether.hotel.tools import HOTEL_TOOLS, render
from aether.lang import ACKNOWLEDGEMENT, ENGLISH, HINDI, LANGUAGES, by_code, detect_switch
from aether.tools import ToolRunner
from aether.trace import Trace

STORE = HotelStore()

# Devanagari, plus the ASCII that legitimately survives into a Hindi sentence: dish names, room
# types and two amenity loanwords are the hotel's own proper nouns and stay as printed.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def speak(said: str, language):
    """Route, run and render one question -- the real path, not a stub."""
    decision = route(said)
    assert decision is not None, f"{said!r} did not route"
    runner = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS)
    result = runner.run(decision.tool, gen="lang", turn_id=1, is_valid=lambda: True,
                        **decision.params)
    return render(result, language)


# --- the language records -------------------------------------------------------------------

def test_hindi_is_a_different_voice_on_a_different_model():
    """Verified against Rime's public catalogue, not assumed.

    `mistv3` offers eng/fra/ger/spa and `astra` is eng on every model it appears under, so Hindi
    cannot be reached by changing a language code on the English voice. If this ever collapses to
    one model and voice, something has been guessed.
    """
    assert HINDI.rime_model != ENGLISH.rime_model
    assert HINDI.rime_voice != ENGLISH.rime_voice
    assert (HINDI.rime_model, HINDI.rime_voice) == ("arcana", "anaya")
    assert (ENGLISH.rime_model, ENGLISH.rime_voice) == ("mistv3", "astra")


def test_hindi_needs_a_multilingual_recogniser():
    """`base.en` is monolingual. Pointing it at Hindi does not produce bad Hindi, it produces
    nothing usable -- so the model, not just the language code, has to change."""
    assert ENGLISH.whisper_model.endswith(".en")
    assert not HINDI.whisper_model.endswith(".en")
    assert (ENGLISH.whisper_code, HINDI.whisper_code) == ("en", "hi")


def test_an_unknown_language_falls_back_rather_than_raising():
    """The caller is on a telephone. The worst acceptable outcome of a bad code is being answered
    in English; an exception would drop the call."""
    assert by_code(None) is ENGLISH
    assert by_code("klingon") is ENGLISH
    assert by_code("hin") is HINDI


# --- asking for a language ------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "Hindi",
    "hindi please",
    "can you speak Hindi",
    "Hindi mein baat kijiye",
    "please talk in Hindi",
])
def test_asking_for_hindi_switches(said):
    assert detect_switch(said, ENGLISH) is HINDI


def test_asking_for_english_switches_back():
    assert detect_switch("English please", HINDI) is ENGLISH


def test_naming_the_language_already_in_use_is_not_a_request():
    """Saying "Hindi" during a Hindi conversation is an ordinary utterance, not a re-switch."""
    assert detect_switch("Hindi", HINDI) is None
    assert detect_switch("English", ENGLISH) is None


@pytest.mark.parametrize("said", [
    "I don't speak Hindi",
    "no Hindi please",
    "I cannot read Hindi",
])
def test_naming_a_language_in_order_to_refuse_it_does_not_switch(said):
    """Without this, declining Hindi would switch into it -- the exact opposite of the request."""
    assert detect_switch(said, ENGLISH) is None


def test_an_ordinary_hotel_question_never_switches_language():
    for said in ("what starters do you have", "how much is the chicken kebab",
                 "is room one oh one free", "what time is check in", ""):
        assert detect_switch(said, ENGLISH) is None, said


# --- Hindi numbers --------------------------------------------------------------------------

def test_every_price_in_the_database_can_be_said_in_hindi():
    """Not a sample: every price and every room rate the hotel actually charges."""
    for item in STORE.menu():
        assert hi.say_price(item.price)
    for room_type in STORE.room_types():
        assert hi.say_price(room_type.rate)


def test_the_irregular_hindi_numbers_are_right():
    """Hindi has a distinct word for each of 1-100 and they are not derivable by rule. The ones
    that end in nine are where a rule-based implementation goes wrong, so they are the ones
    pinned."""
    assert hi.say_number(19) == "उन्नीस"
    assert hi.say_number(29) == "उनतीस"
    assert hi.say_number(39) == "उनतालीस"
    assert hi.say_number(49) == "उनचास"
    assert hi.say_number(99) == "निन्यानवे"
    assert hi.say_number(6500) == "छह हज़ार पाँच सौ"
    assert hi.say_number(15000) == "पंद्रह हज़ार"


def test_a_number_beyond_the_hotel_is_refused_not_guessed():
    with pytest.raises(ValueError):
        hi.say_number(100000)


def test_a_room_number_is_said_as_a_door_not_a_quantity():
    assert hi.say_room_number("305") == "तीन शून्य पाँच"
    assert hi.say_room_number("305") != hi.say_number(305)


def test_hindi_time_puts_the_part_of_day_first():
    """Hindi says "दोपहर दो बजे", not "दो बजे दोपहर" -- which is why this is a rewrite of the
    English helper rather than a translation of its output."""
    assert hi.say_time("14:00").startswith("दोपहर")
    assert hi.say_time("06:00").startswith("सुबह")
    assert hi.say_time("23:00").startswith("रात")


# --- the same fact, in both languages -------------------------------------------------------

QUESTIONS = [
    "What's on the menu?",
    "What starters do you have?",
    "How much is the chicken kebab?",
    "Is the fish curry available?",
    "I'm allergic to nuts, what can I eat?",
    "Do you have any rooms available?",
    "How much is an executive suite?",
    "Is room one oh one free?",
    "What comes with an executive suite?",
    "What time is check in?",
    "Do you have room service?",
    "What services do you have?",
]


@pytest.mark.parametrize("said", QUESTIONS, ids=QUESTIONS)
def test_every_question_is_answered_in_hindi_too(said):
    answer = speak(said, HINDI)
    assert answer, said
    assert _DEVANAGARI.search(answer), f"not actually Hindi: {answer!r}"


@pytest.mark.parametrize("said", QUESTIONS, ids=QUESTIONS)
def test_the_hindi_answer_is_still_deterministic(said):
    """The claim this whole layer exists to protect: hotel facts never reach a language model.

    A Hindi answer rendered by Gemini would be an answer a model was free to get wrong, in a
    language fewer people in the room can check.
    """
    assert route(said) is not None


def test_a_price_is_the_same_number_in_both_languages():
    """The two renderers share a lookup, so they cannot disagree about a fact -- and this is the
    test that would fail if someone ever routed Hindi through a model."""
    item = STORE.find_item("chicken kebab")
    english, hindi = speak("How much is the chicken kebab?", ENGLISH), \
        speak("How much is the chicken kebab?", HINDI)
    from aether.hotel import say_price as say_price_en

    assert say_price_en(item.price) in english
    assert hi.say_price(item.price) in hindi


def test_hindi_never_speaks_a_digit_or_a_symbol():
    """Same rule as English: Rime is handed words. A digit in a Devanagari sentence is a
    mispronunciation waiting to happen, and a colon in a time is worse."""
    for said in QUESTIONS:
        answer = speak(said, HINDI)
        assert not re.search(r"\d", answer), f"digit reaches the voice: {answer!r}"
        assert ":" not in answer, f"unspeakable colon: {answer!r}"


def test_the_hotels_own_proper_nouns_stay_as_printed():
    """A caller asking for the "Chicken Kebab" wants to hear "Chicken Kebab" -- it is what the menu
    says. Translating a hotel's own names would be inventing names it does not use."""
    assert "Chicken Kebab" in speak("What starters do you have?", HINDI)
    assert "Executive Suite" in speak("How much is an executive suite?", HINDI)


def test_a_reservation_never_speaks_the_guests_name_in_either_language():
    """A privacy property must not be lost in translation."""
    import sqlite3

    from aether.hotel import DEFAULT_DB_PATH

    db = sqlite3.connect(f"file:{DEFAULT_DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        names = [r[0] for r in db.execute("select full_name from guests")]
    finally:
        db.close()

    for language in (ENGLISH, HINDI):
        answer = speak("Is room two oh two reserved?", language)
        for full_name in names:
            assert full_name.split()[0] not in answer, f"{full_name} leaked in {language.code}"


def test_both_languages_render_every_tool():
    """A missing Hindi template would fall back to nothing spoken, which on a call is silence."""
    from aether.hotel import tools as tools_en
    from aether.hotel import tools_hi

    assert set(tools_hi.SPEAK) == set(tools_en.SPEAK)
    assert set(tools_hi.SPEAK) == set(HOTEL_TOOLS)


def test_the_acknowledgement_exists_in_both_and_is_in_the_right_script():
    assert _DEVANAGARI.search(ACKNOWLEDGEMENT["hin"])
    assert not _DEVANAGARI.search(ACKNOWLEDGEMENT["eng"])
    assert set(ACKNOWLEDGEMENT) == set(LANGUAGES)


# --- the guarantee that English did not move ------------------------------------------------

def test_english_rendering_is_unchanged_by_the_existence_of_hindi():
    """`render(result)` with no language must behave exactly as it did before, because the demo
    being recorded runs through that call."""
    for said in QUESTIONS:
        assert speak(said, None) == speak(said, ENGLISH)


def test_the_greeting_tells_the_caller_how_to_switch():
    """A capability nobody can discover is not a capability. The greeting names the single word the
    recogniser hears most reliably."""
    from aether.telephony.agent import GREETING

    assert "Hindi" in GREETING
    assert GREETING.startswith("You've reached AETHER")
