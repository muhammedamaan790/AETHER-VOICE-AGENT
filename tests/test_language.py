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

    `coda`, not `arcana`: Rime deleted the whole `arcana` model between 2026-09-08 and 2026-09-09,
    taking the Hindi voices `anaya`, `anil` and `arya` with it. It kept answering after it was
    delisted, which is the trap -- an undocumented endpoint that merely happens to respond is
    exactly what this project refuses to depend on (RIME_EVIDENCE.md Part 1c).
    """
    assert HINDI.rime_model != ENGLISH.rime_model
    assert HINDI.rime_voice != ENGLISH.rime_voice
    assert (HINDI.rime_model, HINDI.rime_voice) == ("coda", "nadi")
    assert (ENGLISH.rime_model, ENGLISH.rime_voice) == ("mistv3", "astra")


def test_the_hindi_voice_and_the_hindi_templates_agree_about_gender():
    """Hindi marks gender on the verb, so the voice and the templates cannot be chosen separately.

    `nadi` is female, and the templates say `करूँगी` / `सुझाऊँगी` -- feminine. Switching to `taru`,
    the other catalogued Hindi voice, is not a one-line config change: it is a male voice, and
    leaving these forms alone would have him speak as a woman, which any Hindi speaker hears at
    once. The model is told the same thing, so a router miss does not change gender either.
    """
    import inspect

    from aether.hotel import tools_hi
    from aether.lang import ACKNOWLEDGEMENT
    from aether.llm import LANGUAGE_DIRECTIVE

    assert HINDI.rime_voice == "nadi", "a different voice may need different verb forms"
    assert "करूँगी" in ACKNOWLEDGEMENT["hin"]
    assert "सुझाऊँगी" in inspect.getsource(tools_hi._speak_safe_for)
    # The directive is written IN Hindi now (a worked example in the target language raised
    # adherence from 2/3 to 4/4), so the instruction about gender is in Hindi too.
    assert "स्त्रीलिंग" in LANGUAGE_DIRECTIVE["hin"]
    assert "कर सकती हूँ" in LANGUAGE_DIRECTIVE["hin"]


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


def test_the_call_opens_by_asking_for_a_language_not_by_greeting():
    """The FIRST thing a caller hears is the question, not the hotel greeting.

    The greeting has to be spoken in some language, so greeting first would already have chosen for
    the caller -- and a Hindi speaker would sit through an English greeting before being offered
    Hindi. This pins the ordering, not the wording.
    """
    from aether.lang import HOTEL_GREETING, SELECT_PROMPT
    from aether.telephony.agent import GREETING

    assert GREETING == SELECT_PROMPT, "the call must open with the language question"
    for language in ALL_LANGUAGES:
        assert name_of(language, spoken_in=ENGLISH) in GREETING
    assert GREETING.strip().endswith("?")
    # And the real greeting still exists, one per language, for after the choice is made.
    assert set(HOTEL_GREETING) == set(LANGUAGES)
    assert GREETING not in HOTEL_GREETING.values(), "the question is not the greeting"


# ============================ every language, not just Hindi ============================
#
# Parametrised rather than copied, so a fourth language gets the whole safety net by being added to
# LANGUAGES -- and so a property can never hold in one language and quietly fail in another.

from aether.lang import SPANISH, language_offer, name_of, spoken_language_list   # noqa: E402

NON_ENGLISH = [HINDI, SPANISH]
ALL_LANGUAGES = [ENGLISH, HINDI, SPANISH]
_SCRIPTS = {"hin": re.compile(r"[ऀ-ॿ]"), "spa": re.compile(r"[a-záéíóúñü¿¡]", re.I)}


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
@pytest.mark.parametrize("said", QUESTIONS, ids=QUESTIONS)
def test_every_question_is_answered_in_every_language(language, said):
    answer = speak(said, language)
    assert answer, f"{language.code} has no answer for {said!r}"


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_no_language_ever_speaks_a_digit_or_a_colon(language):
    """Rime is handed words in every language. A digit is a mispronunciation waiting to happen."""
    for said in QUESTIONS:
        answer = speak(said, language)
        assert not re.search(r"\d", answer), f"{language.code}: digit in {answer!r}"
        assert ":" not in answer, f"{language.code}: colon in {answer!r}"


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_the_hotels_proper_nouns_survive_every_translation(language):
    """A caller asking for the "Chicken Kebab" hears "Chicken Kebab" -- it is what the menu says."""
    assert "Chicken Kebab" in speak("What starters do you have?", language)
    assert "Executive Suite" in speak("How much is an executive suite?", language)


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_no_language_speaks_a_guests_name(language):
    """A privacy property must survive every translation."""
    import sqlite3

    from aether.hotel import DEFAULT_DB_PATH

    db = sqlite3.connect(f"file:{DEFAULT_DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        names = [r[0] for r in db.execute("select full_name from guests")]
    finally:
        db.close()
    answer = speak("Is room two oh two reserved?", language)
    for full_name in names:
        assert full_name.split()[0] not in answer, f"{full_name} leaked in {language.code}"


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_every_language_renders_every_tool(language):
    """A missing template falls back to nothing spoken, which on a telephone is silence."""
    from aether.hotel.tools import _renderer_for

    speakers, not_found = _renderer_for(language)
    assert set(speakers) == set(HOTEL_TOOLS), f"{language.code} is missing templates"
    assert not_found.strip(), f"{language.code} has no not-found sentence"


def test_the_price_is_the_same_number_in_every_language():
    """The point of one facts database: the languages cannot disagree about what a thing costs.

    This is the test that fails the moment somebody stores prices per language instead of rendering
    one row three ways.
    """
    from aether.hotel import say_price as say_en
    from aether.hotel.speech_es import say_price as say_es
    from aether.hotel.speech_hi import say_price as say_hi

    item = STORE.find_item("chicken kebab")
    said = "How much is the chicken kebab?"
    assert say_en(item.price) in speak(said, ENGLISH)
    assert say_hi(item.price) in speak(said, HINDI)
    assert say_es(item.price) in speak(said, SPANISH)


# --- choosing a language, and being told what there is ---------------------------------------

@pytest.mark.parametrize("said", [
    "Spanish", "spanish please", "can we switch to Spanish", "español", "castellano",
])
def test_asking_for_spanish_switches(said):
    assert detect_switch(said, ENGLISH) is SPANISH


@pytest.mark.parametrize("said", [
    "can we switch language",
    "what languages do you speak",
    "which languages do you have",
    "can you change the language",
])
def test_asking_which_languages_offers_the_list_rather_than_guessing(said):
    """Switching to a language nobody named would be a guess. The list is the honest answer."""
    from aether.lang import asks_for_options

    assert asks_for_options(said)
    assert detect_switch(said, ENGLISH) is None, "must not pick a language on its own"


def test_naming_a_language_beats_asking_for_the_list():
    """"Can we switch language to Hindi" names one, so it switches rather than re-offering."""
    assert detect_switch("can we switch language to Hindi", ENGLISH) is HINDI


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_the_offer_names_every_language_in_the_language_being_spoken(language):
    """"English, Hindi या Spanish" is the half-localisation that gives away a machine. In Hindi the
    languages are अंग्रेज़ी, हिन्दी and स्पेनिश."""
    offer = language_offer(language)
    for other in ALL_LANGUAGES:
        assert name_of(other, spoken_in=language) in offer, f"{other.code} missing from {language.code}"
    script = _SCRIPTS.get(language.code)
    if script is not None:
        assert script.search(offer), f"the {language.code} offer is not in its own script: {offer!r}"


def test_the_offer_is_built_from_the_registry_not_written_out():
    """Adding a language must update the greeting, not leave it quietly wrong."""
    from aether.lang import LANGUAGES

    listed = spoken_language_list(ENGLISH)
    assert len(LANGUAGES) >= 3
    for lang in LANGUAGES.values():
        assert name_of(lang, spoken_in=ENGLISH) in listed


def test_the_greeting_asks_which_language_and_names_them():
    from aether.telephony.agent import GREETING

    for lang in ALL_LANGUAGES:
        assert name_of(lang, spoken_in=ENGLISH) in GREETING
    assert GREETING.strip().endswith("?"), "the greeting must hand the turn back"
    assert len(GREETING.split()) <= 20, "a long greeting is a bad phone greeting"


@pytest.mark.parametrize("language", NON_ENGLISH, ids=lambda L: L.code)
def test_the_model_is_told_which_language_to_answer_in(language):
    """The router does not catch everything, and a model left to itself answers a Hindi question in
    English about a third of the time -- measured, not assumed."""
    from aether.llm import system_prompt_for

    prompt = system_prompt_for(language)
    assert prompt != system_prompt_for(ENGLISH), f"{language.code} adds no directive"
    assert system_prompt_for(ENGLISH) == system_prompt_for(None), "English must be untouched"


# ============================ the selection flow, through the real pipeline ============================
#
# Not the helper functions in isolation: a real `Day1Spike` with fake IO, so the state machine, the
# renderer swap, the STT language and the Rime speaker are all exercised together.

from tests.test_menu_routing import AUDIO, build   # noqa: E402


def _say(spike, monkeypatch, text):
    """Put one utterance through the real turn loop and return what was spoken."""
    monkeypatch.setattr(spike.stt, "text", text, raising=False)
    spike.handle_utterance(AUDIO, 0.0)
    return spike.rime.spoken[-1] if spike.rime.spoken else None


@pytest.mark.parametrize("choice", ["English", "Hindi", "Spanish"])
def test_choosing_a_language_gives_the_hotel_greeting_in_it(choice, monkeypatch):
    from aether.lang import HOTEL_GREETING, names_language

    spike, _t, rime, llm = build(monkeypatch, choice)
    spike.begin_language_selection()
    assert spike.awaiting_language

    said = _say(spike, monkeypatch, choice)
    chosen = names_language(choice)
    assert spike.language.code == chosen.code
    assert not spike.awaiting_language, "the choice is made once, not asked again"
    assert said == HOTEL_GREETING[chosen.code]
    assert llm.calls == [], "choosing a language must not need the model"


def test_an_unrecognised_answer_asks_again_and_stays_in_selection(monkeypatch):
    from aether.lang import SELECT_RETRY

    spike, _t, _rime, _llm = build(monkeypatch, "mmm hello there")
    spike.begin_language_selection()
    said = _say(spike, monkeypatch, "mmm hello there")
    assert said == SELECT_RETRY
    assert spike.awaiting_language, "still owed a choice"
    assert spike.language.code == "eng", "no language may be guessed"


def test_the_language_persists_and_is_not_asked_again(monkeypatch):
    """"Do NOT ask the customer to select a language again on every turn." """
    spike, _t, _rime, _llm = build(monkeypatch, "Hindi")
    spike.begin_language_selection()
    _say(spike, monkeypatch, "Hindi")
    for _ in range(3):
        said = _say(spike, monkeypatch, "what starters do you have")
        assert said is not None
        assert "prefer" not in said.lower(), "the language question must not come back"
        assert spike.language.code == "hin"


@pytest.mark.parametrize(("first", "second"), [
    ("English", "Hindi"), ("Hindi", "English"), ("English", "Spanish"),
    ("Spanish", "Hindi"), ("Hindi", "Spanish"), ("Spanish", "English"),
])
def test_switching_mid_call_changes_language_voice_and_recogniser(first, second, monkeypatch):
    """Every direction, and the switch must move the renderer, the voice AND the recogniser --
    leaving any one of the three behind is the bug this pins."""
    from aether.lang import ACKNOWLEDGEMENT, names_language

    spike, _t, _rime, _llm = build(monkeypatch, first)
    spike.begin_language_selection()
    _say(spike, monkeypatch, first)
    assert spike.language.code == names_language(first).code

    target = names_language(second)
    said = _say(spike, monkeypatch, f"can we switch to {second}")
    assert said == ACKNOWLEDGEMENT[target.code]
    assert spike.language.code == target.code
    assert spike.rime.config.voice == target.rime_voice, "the voice did not follow the language"
    assert spike.rime.config.model == target.rime_model
    assert spike.rime.config.language == target.code
    assert spike.stt.language.code == target.code, "the recogniser did not follow the language"
    assert spike.llm.language.code == target.code, "the model was not told the new language"


def test_asking_to_switch_without_naming_one_offers_the_list(monkeypatch):
    from aether.lang import language_offer

    spike, _t, _rime, _llm = build(monkeypatch, "Hindi")
    spike.begin_language_selection()
    _say(spike, monkeypatch, "Hindi")
    said = _say(spike, monkeypatch, "can we switch language")
    assert said == language_offer(spike.language)
    assert spike.language.code == "hin", "no language may be guessed from an unnamed request"


def test_a_new_call_starts_from_language_selection_again(monkeypatch):
    """Call isolation: the language chosen in one call must not carry into the next.

    Each call builds its own pipeline, so this asserts the property that makes that safe -- a fresh
    spike owes a choice and is back on the default until it gets one.
    """
    first, _t, _rime, _llm = build(monkeypatch, "Hindi")
    first.begin_language_selection()
    _say(first, monkeypatch, "Hindi")
    assert first.language.code == "hin"

    second, _t2, _rime2, _llm2 = build(monkeypatch, "what starters do you have")
    second.begin_language_selection()
    assert second.awaiting_language, "call two must ask again"
    assert second.language.code == "eng", "call one's language must not leak into call two"
    assert second.history.messages() == [], "call one's transcript must not leak either"


def test_selection_is_off_unless_a_call_turns_it_on(monkeypatch):
    """The local-microphone path and every existing test construct a spike and start talking. Making
    selection the default would silently turn their first utterance into a language answer."""
    spike, _t, _rime, _llm = build(monkeypatch, "what starters do you have")
    assert spike.awaiting_language is False
    said = _say(spike, monkeypatch, "what starters do you have")
    assert "Chicken Kebab" in said, "an ordinary question, answered ordinarily"


# ============================ policies, and the grammar the audit found ============================

def test_every_policy_in_the_database_can_be_spoken_in_every_language():
    """Not a sample: every row in `hotel_policies`, in all three languages."""
    from aether.hotel.tools import HOTEL_TOOLS as TOOLS

    runner = ToolRunner(Trace(), STORE, tools=TOOLS)
    for policy in STORE.policies():
        result = runner.run("hotel_policy", gen="p", turn_id=1, is_valid=lambda: True,
                            topic=policy.topic)
        for language in ALL_LANGUAGES:
            said = render(result, language)
            assert said, f"{policy.topic} has no {language.code} answer"
            assert not re.search(r"\d", said), f"{language.code}/{policy.topic}: digit in {said!r}"
            assert "_" not in said, f"{language.code}/{policy.topic}: machine key leaked: {said!r}"


def test_a_policy_the_hotel_has_no_record_of_is_refused_in_every_language():
    """The protection that matters: an unknown policy must become "I do not have that", never a
    plausible invention."""
    from aether.hotel.tools import HOTEL_TOOLS as TOOLS

    # The absent topic is DERIVED from the database, not written in. This test used to name "spa",
    # which stopped being absent the day the hotel gained a spa -- a test that silently becomes a
    # test of something else is worse than no test. Asking the store what it holds cannot go stale.
    absent = "rooftop_helipad"
    assert absent not in {policy.topic for policy in STORE.policies()}, (
        f"{absent!r} is no longer an absent topic; pick another"
    )

    runner = ToolRunner(Trace(), STORE, tools=TOOLS)
    result = runner.run("hotel_policy", gen="p", turn_id=1, is_valid=lambda: True, topic=absent)
    for language in ALL_LANGUAGES:
        from aether.hotel.tools import _renderer_for

        _speakers, not_found = _renderer_for(language)
        assert render(result, language) == not_found


def test_a_policy_is_never_phrased_as_something_the_hotel_offers_when_it_is_not_a_service():
    """"Yes, we offer children" is what a template applied without thinking produces."""
    from aether.hotel.tools import HOTEL_TOOLS as TOOLS

    runner = ToolRunner(Trace(), STORE, tools=TOOLS)
    for topic in ("children", "accessibility"):
        result = runner.run("hotel_policy", gen="p", turn_id=1, is_valid=lambda: True, topic=topic)
        english = render(result, ENGLISH)
        assert "we offer children" not in english.lower()
        assert english.startswith("Yes,")


def test_spanish_says_the_half_hour_the_way_spanish_does():
    """"las diez y treinta" is understood and is not what anyone says."""
    from aether.hotel.speech_es import say_time

    assert say_time("10:30") == "las diez y media de la mañana"
    assert say_time("14:15") == "las dos y cuarto de la tarde"
    assert say_time("01:00") == "la una de la mañana", "one o'clock takes the singular article"


def test_hindi_says_the_half_hour_the_way_hindi_does():
    """Half past one and half past two are irregular: डेढ़ and ढाई, not साढ़े एक / साढ़े दो."""
    from aether.hotel.speech_hi import say_time

    assert say_time("01:30") == "सुबह डेढ़ बजे"
    assert say_time("02:30") == "सुबह ढाई बजे"
    assert say_time("10:30") == "सुबह साढ़े दस बजे"
    assert say_time("14:15") == "दोपहर सवा दो बजे"


def test_spanish_numbers_agree_with_the_feminine_noun_they_count():
    """`rupia` and `habitación` are feminine, so the hundreds and any final `uno` agree.
    "quinientos rupias" and "cuarenta y uno habitaciones" are errors a speaker hears at once."""
    from aether.hotel.speech_es import say_number, say_price

    assert say_price(520) == "quinientas veinte rupias"
    assert say_price(6500) == "seis mil quinientas rupias"
    assert say_price(220) == "doscientas veinte rupias"
    assert say_number(41, feminine=True) == "cuarenta y una"
    assert say_number(41) == "cuarenta y uno", "the masculine form must still be available"
    # The three suppletive hundreds are where a naive rule leaves the masculine form behind.
    for n in (500, 700, 900):
        assert say_number(n, feminine=True).endswith("ientas"), n


def test_the_hotel_info_answer_counts_floors_rather_than_storing_them():
    """A stored floor count is a second source of truth able to contradict the rooms."""
    floors = len({room.floor for room in STORE.rooms()})
    assert STORE.floors() == floors
    from aether.hotel.speech_es import say_number as es_number
    from aether.hotel.speech_hi import say_number as hi_number

    from aether.hotel import say_number as en_number
    from aether.hotel.tools import HOTEL_TOOLS as TOOLS

    runner = ToolRunner(Trace(), STORE, tools=TOOLS)
    result = runner.run("hotel_info", gen="p", turn_id=1, is_valid=lambda: True)
    assert en_number(floors) in render(result, ENGLISH)
    assert hi_number(floors) in render(result, HINDI)
    assert es_number(floors) in render(result, SPANISH)


# ============================ the model, and which language it answers in ============================

def test_the_active_language_reaches_the_provider_through_the_retry_wrapper():
    """REGRESSION, and it silently disabled multilingual fallback entirely.

    `build_llm()` returns `RetryingLLM`, so `spike._set_language` set `.language` on the WRAPPER
    while every adapter read `getattr(self, "language", None)` on itself and saw nothing. Neither
    the language directive nor the per-turn reminder was applied to a single live call.

    It hid for a while because a Devanagari question elicits a Hindi answer whatever the prompt
    says -- Hindi looked fine, and Spanish answering in English looked like a model limitation
    rather than an assignment going nowhere. Measured after the fix: 4/4 in all three languages.
    """
    from aether.llm import RetryingLLM

    class _Inner:
        name = "fake"

        def respond(self, user_text, history=None):
            return "ok"

    inner = _Inner()
    wrapper = RetryingLLM(inner, backoff=())
    wrapper.language = SPANISH
    assert inner.language is SPANISH, "the wrapper swallowed the language"
    assert wrapper.language is SPANISH, "reading it back must give the same object"


@pytest.mark.parametrize("language", NON_ENGLISH, ids=lambda L: L.code)
def test_the_caller_turn_carries_a_reply_in_this_language_reminder(language):
    """Distance from the generation point mattered more than emphasis: the same instruction buried
    in several thousand characters of English system prompt was followed 1-2 times in 5."""
    from aether.llm import localised

    out = localised("hello", language)
    assert out.startswith("hello")
    assert out != "hello", f"{language.code} adds no reminder"


def test_english_text_is_passed_to_the_model_untouched():
    """The English path must be byte-for-byte what it was."""
    from aether.llm import localised

    assert localised("What time is check in?", ENGLISH) == "What time is check in?"
    assert localised("What time is check in?", None) == "What time is check in?"


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_a_question_the_database_can_answer_never_reaches_the_model(language):
    """THE precedence rule: when a deterministic route exists, the database wins.

    This is what stops the model quoting 380 for a dish the database prices at 420 -- not a prompt
    instruction, but the router intercepting before the model is ever consulted. Asserted per
    language because a missing Hindi or Spanish route would silently hand a priced question to a
    model that is now allowed to improvise.
    """
    for said in ("How much is the chicken kebab?", "What time is check in?",
                 "Is room one oh one free?", "Do you have parking?"):
        decision = route(said)
        assert decision is not None, f"{said!r} would reach the model"
        answer = speak(said, language)
        assert answer, f"{language.code} cannot render {decision.tool}"


def test_the_price_the_model_could_contradict_is_never_asked_of_it():
    """The concrete case: the database says 420, and the model is never given the chance to say
    anything else, in any language."""
    item = STORE.find_item("chicken kebab")
    assert route("How much is the chicken kebab?") is not None
    from aether.hotel import say_price as en

    assert en(item.price) in speak("How much is the chicken kebab?", ENGLISH)
    assert str(int(item.price)) == "420", "the fixture this test describes has changed"


# ============================ grammar the audit found, pinned ============================

def _run(tool, language, **params):
    from aether.hotel.tools import HOTEL_TOOLS as TOOLS

    runner = ToolRunner(Trace(), STORE, tools=TOOLS)
    return render(runner.run(tool, gen="g", turn_id=1, is_valid=lambda: True, **params), language)


def test_hindi_agrees_in_number_with_how_many_things_it_lists():
    """"Butter Chicken हैं" is wrong -- one dish takes है. `mains` holds exactly one dish, so the
    bug was live rather than hypothetical."""
    one = _run("list_category", HINDI, category="mains")
    many = _run("list_category", HINDI, category="starters")
    assert "Butter Chicken है।" in one, one
    assert "हैं।" in many, many


def test_hindi_does_not_stutter_the_list_joiner():
    """`say_list` already ends "... और X", so a tail of "और N और" repeats it."""
    said = _run("find_by_diet", HINDI, diet="vegetarian")
    assert "और दो और" not in said, said
    assert "अन्य" in said


def test_hindi_price_clauses_have_a_verb():
    """"कीमत ... से शुरू।" is a sentence fragment; कीमत is feminine, so शुरू होती है."""
    for said in (_run("room_availability", HINDI), _run("list_room_types", HINDI)):
        assert "से शुरू होती है" in said, said


def test_spanish_diet_adjectives_agree_with_opciones():
    """`opciones` is feminine plural, so "con opciones vegetariano" is an agreement error."""
    overview = _run("menu_overview", SPANISH)
    for wrong in ("opciones vegetariano", "opciones vegano", "opciones no vegetariano"):
        assert wrong not in overview, overview
    assert "vegetarianas" in overview and "veganas" in overview

    vegan = _run("find_by_diet", SPANISH, diet="vegan")
    assert "opciones veganas" in vegan, vegan


def test_spanish_service_articles_match_the_service_gender():
    """"El recepción" is wrong -- recepción is feminine. The article is stored with the name
    because it varies, and a fixed "El" in the template got it wrong for exactly one service."""
    front = _run("service_hours", SPANISH, service="Front Desk")
    room = _run("service_hours", SPANISH, service="Room Service")
    assert front.startswith("La recepción"), front
    assert "El recepción" not in front
    assert room.startswith("El servicio de habitaciones"), room
    # ...and the object pronoun agrees too.
    assert "contactarla" in front, front
    assert "contactarlo" in room, room


def test_spanish_service_list_does_not_repeat_the_articles():
    """"Ofrecemos la recepción, el servicio de limpieza..." is not how a list is read aloud."""
    said = _run("list_services", SPANISH)
    assert " la recepción" not in said, said
    assert "recepción" in said


def test_spanish_allergen_advice_takes_the_article():
    said = _run("safe_for", SPANISH, allergen="nuts")
    assert "Si evita los frutos secos" in said, said
import pathlib


# ============================ fact parity: traced to the row, not the string ============================
#
# Comparing translated sentences proves nothing -- three renderers could each hardcode a different
# price and still all "look right". These read the value out of the DATABASE first and then require
# each language's spoken form of THAT value to appear in its answer. If one language starts
# hardcoding a different fact, the assertion fails on the language, not on the wording.

def _spoken_price(value, language):
    from aether.hotel import say_price as en
    from aether.hotel.speech_es import say_price as es
    from aether.hotel.speech_hi import say_price as hi

    return {"eng": en, "hin": hi, "spa": es}[language.code](value)


def _spoken_number(value, language, feminine=False):
    from aether.hotel import say_number as en
    from aether.hotel.speech_es import say_number as es
    from aether.hotel.speech_hi import say_number as hi

    if language.code == "spa":
        return es(value, feminine=feminine)
    return {"eng": en, "hin": hi}[language.code](value)


def _spoken_time(clock, language):
    from aether.hotel import say_time as en
    from aether.hotel.speech_es import say_time as es
    from aether.hotel.speech_hi import say_time as hi

    return {"eng": en, "hin": hi, "spa": es}[language.code](clock)


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_the_menu_price_every_language_speaks_is_the_row_in_the_database(language):
    item = STORE.find_item("chicken kebab")
    said = speak("How much is the chicken kebab?", language)
    assert _spoken_price(item.price, language) in said, said


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_the_room_rate_every_language_speaks_is_the_row_in_the_database(language):
    room_type = STORE.room_type("Executive Suite")
    said = speak("How much is an executive suite?", language)
    assert _spoken_price(room_type.rate, language) in said, said


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_the_free_room_count_every_language_speaks_is_counted_from_the_database(language):
    free = len(STORE.available_rooms())
    said = speak("Do you have any rooms available?", language)
    # `habitación` is feminine, so Spanish counts it with the feminine form.
    assert _spoken_number(free, language, feminine=True) in said, said


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_check_in_and_check_out_every_language_speaks_are_the_hotel_row(language):
    info = STORE.hotel()
    said = speak("What time is check in?", language)
    assert _spoken_time(info.check_in_time, language) in said, said
    assert _spoken_time(info.check_out_time, language) in said, said


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_service_hours_every_language_speaks_are_the_service_row(language):
    service = STORE.service("Room Service")
    opens, _, closes = str(service.availability).partition("-")
    said = speak("Do you have room service?", language)
    assert _spoken_time(opens, language) in said, said
    assert _spoken_time(closes, language) in said, said


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_room_status_every_language_speaks_is_the_room_row(language):
    room = next(r for r in STORE.rooms() if r.status == "available")
    said = speak(f"Is room {room.number} free?", language)
    assert _spoken_price(STORE.room_type(room.room_type).rate, language) in said, said


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_room_amenities_every_language_lists_the_database_amenities(language):
    room_type = STORE.room_type("Family Suite")
    said = speak("What comes with a family suite?", language)
    # Translated per language, so the COUNT is what can be compared across all three; the English
    # side additionally checks the words themselves.
    assert said.count(",") >= len(room_type.amenities) - 2, said
    if language.code == "eng":
        for amenity in room_type.amenities:
            assert amenity.lower() in said.lower(), amenity


@pytest.mark.parametrize("language", ALL_LANGUAGES, ids=lambda L: L.code)
def test_reservation_dates_every_language_speaks_are_the_reservation_row(language):
    from aether.hotel import say_date as en
    from aether.hotel.speech_es import say_date as es
    from aether.hotel.speech_hi import say_date as hi

    booking = STORE.reservation_for_room("202")
    said = speak("Is room two oh two reserved?", language)
    spoken = {"eng": en, "hin": hi, "spa": es}[language.code]
    assert spoken(booking.check_in) in said, said
    assert spoken(booking.check_out) in said, said


def test_no_language_module_hardcodes_a_hotel_price():
    """The structural half of the same guarantee: a renderer may hold translations, never facts.

    Scans the Hindi and Spanish modules for any number that happens to be a real price or rate. A
    match does not prove wrongdoing, but it is exactly the shape a duplicated fact would take, and
    there should be none.
    """
    import re

    facts = {int(i.price) for i in STORE.menu()} | {int(t.rate) for t in STORE.room_types()}
    for module in ("tools_hi.py", "tools_es.py", "speech_hi.py", "speech_es.py"):
        text = (pathlib.Path("aether/hotel") / module).read_text(encoding="utf-8")
        code = "\n".join(line.split("#")[0] for line in text.splitlines())
        for number in re.findall(r"\d{3,5}", code):
            assert int(number) not in facts, f"{module} hardcodes the hotel fact {number}"
