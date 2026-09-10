"""Deterministic routing, wired into the real turn loop.

`test_hotel_db.py` owns the data: which question reaches which tool, and whether the spoken answer
matches the database row. This file owns what only the assembled pipeline can show --

* a hotel question is answered **without the model**, with real fencing and real history;
* the router stays conservative, so a sentence it is not sure about still reaches the model;
* a fenced lookup speaks nothing, and leaks nothing.

Expectations are read from the database rather than written down, so a test cannot pass while the
data says otherwise. STT output is not tidy, so the phrasings here are deliberately messy: filler
words, plurals, missing punctuation, and the way people actually ask on a phone.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.events import EventType
from aether.hotel import HotelStore, say_price
from aether.hotel.router import Route, normalise, route
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)
STORE = HotelStore()


# ============================ the router ============================

def test_normalise_survives_stt_output():
    assert normalise("  What STARTERS, do you have?? ") == "what starters do you have"


@pytest.mark.parametrize("said", [
    "what starters do you have",
    "What starters do you have?",
    "which appetisers are available",
    "tell me about the starters",
    "do you have any starters",
])
def test_category_questions_route_to_list_category(said):
    decision = route(said)
    assert decision is not None and decision.tool == "list_category"
    assert decision.params["category"] == "starters"


@pytest.mark.parametrize("said", [
    "how much is the chicken kebab",
    "what is the price of the chicken kebab",
    "how much does the chicken kebab cost",
])
def test_price_questions_route_to_price_of(said):
    decision = route(said)
    assert decision is not None and decision.tool == "price_of"
    assert decision.params["dish"] == "chicken kebab"


def test_a_bare_dish_name_is_treated_as_a_price_question():
    """A bare dish name on a phone means "tell me about it", and price is the useful answer."""
    decision = route("the chicken kebab")
    assert decision is not None and decision.tool == "price_of"


@pytest.mark.parametrize(("said", "expected_diet"), [
    ("do you have vegan options", "vegan"),
    ("i want something non vegetarian", "non-vegetarian"),
])
def test_dietary_questions_route_to_find_by_diet(said, expected_diet):
    decision = route(said)
    assert decision is not None and decision.tool == "find_by_diet"
    assert decision.params["diet"] == expected_diet


def test_a_diet_question_carries_the_category_when_given():
    decision = route("do you have vegetarian mains")
    assert decision is not None and decision.tool == "find_by_diet"


@pytest.mark.parametrize("said", [
    "is the fish curry available",
    "do you still have the fish curry",
    "is the fish curry sold out",
])
def test_availability_questions_route_to_check_availability(said):
    decision = route(said)
    assert decision is not None and decision.tool == "check_availability"
    assert decision.params["dish"] == "fish curry"


def test_allergen_questions_win_over_price():
    """Specificity order matters: "does X contain nuts" also looks price-ish and list-ish."""
    decision = route("does the butter chicken contain nuts")
    assert decision is not None and decision.tool == "check_allergens"
    assert decision.params["dish"] == "butter chicken"


def test_the_longest_dish_name_wins():
    """"paneer butter masala" must not be answered as "paneer tikka" -- both contain "paneer"."""
    decision = route("how much is the paneer butter masala")
    assert decision is not None and decision.params["dish"] == "paneer butter masala"


def test_a_question_about_a_dish_itself_reads_its_description():
    """The database records a description, not a heat rating, so that is what comes back."""
    for said in ("is the chicken kebab spicy", "how hot is the chicken kebab",
                 "tell me about the chicken kebab"):
        decision = route(said)
        assert decision is not None and decision.tool == "describe_item", said


def test_an_explicit_price_question_is_not_derailed_by_a_stray_spice_word():
    """REGRESSION. Ordering the description rule before price made every price question containing
    "hot" or "medium" answer the wrong question -- and STT inserts those words readily."""
    for said in ("how much is the hot chicken kebab",
                 "how much is the medium chicken kebab",
                 "what is the price of the hot butter chicken"):
        decision = route(said)
        assert decision is not None and decision.tool == "price_of", said


def test_there_is_no_rule_for_filtering_the_menu_by_spice():
    """The database records no spice level, and filtering on a column that does not exist is not
    something to fake. Such a question reaches the model, which is given the whole menu."""
    from aether.hotel.tools import HOTEL_TOOLS

    assert route("do you have anything mild") is None
    assert "find_by_spice" not in HOTEL_TOOLS


@pytest.mark.parametrize(("said", "allergen"), [
    ("i have a nut allergy what can i eat", "nuts"),
    ("i cannot eat gluten", "gluten"),
    ("is there anything without dairy", "dairy"),
])
def test_an_allergy_with_no_dish_named_routes_to_safe_for(said, allergen):
    decision = route(said)
    assert decision is not None and decision.tool == "safe_for"
    assert decision.params["allergen"] == allergen


def test_an_allergen_alone_is_not_an_allergy_question():
    """"Do you have any fish?" is a menu browse. Requiring an avoidance cue keeps them apart."""
    assert route("do you have any fish") is None


def test_naming_a_dish_still_wins_over_the_allergy_rule():
    assert route("does the butter chicken contain nuts").tool == "check_allergens"


def test_a_vague_allergy_question_still_goes_to_the_model():
    """Answering "any food allergies?" with a list of courses would be a confident non-answer to
    the one question where that is dangerous."""
    assert route("do you have any food allergies information") is None


@pytest.mark.parametrize("said", [
    "can i book a table for eight",
    "hello",
    "my name is daniel",
    "is the food good",
    "where is the food court",
    "",
    "   ",
])
def test_anything_the_router_is_not_confident_about_falls_through(said):
    """None means "let the model handle it". A wrong tool is worse than a slower answer."""
    assert route(said) is None


def test_the_router_only_names_a_tool_and_never_runs_one():
    """Routing and execution stay separate so the router can never bypass fencing."""
    import inspect
    import io
    import tokenize

    import aether.hotel.router as mod

    # CODE only. The docstring names `ToolRunner` precisely to say the router does not use it,
    # and a raw-source grep would trip over that explanation.
    src = inspect.getsource(mod)
    code = " ".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    assert "ToolRunner" not in code, "the router must not execute anything"
    assert isinstance(route("what starters do you have"), Route)


# ============================ the wired path ============================

class _Mic:
    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None
        self.listening = True
        self.samplerate = 16000
        self.frame_samples = 320

    def set_listening(self, value):
        self.listening = bool(value)

    def set_context(self, **k): ...


class _STT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio, *, turn_id=None, gen=None):
        return self.text


class _Rime:
    """A fake speaker that still knows which language it was built for.

    The language matters: the real `build_tts(language=...)` returns a DIFFERENT speaker per
    language, because Rime bakes speaker/model/lang into the connect URL. A fake that ignored the
    argument would report `astra` on a Hindi call and quietly hide a switch that never happened --
    so it carries the same config the real one would.

    All the fakes for a session share one `spoken` list, so a test can read everything said across a
    language switch from the object it was handed.
    """

    name, transport = "rime", "fake"

    def __init__(self, language=None, spoken=None):
        from aether.lang import DEFAULT

        language = language or DEFAULT
        self.spoken: list[str] = [] if spoken is None else spoken
        self.config = type("c", (), {
            "model": language.rime_model,
            "voice": language.rime_voice,
            "language": language.code,
        })()
        self.last_latency_ms = 1.0

    def speak(self, text, *, gate, gen, turn_id=None, is_valid=None):
        from aether.audio.rime_ws import SpeakResult

        self.spoken.append(text)
        if is_valid is not None and not is_valid():
            return SpeakResult(accepted=False, completed=False, reason="fenced_midstream")
        pcm = np.zeros(gate.samplerate // 10, dtype=np.int16)
        ok = gate.enqueue(pcm, turn_id=turn_id, gen=gen)
        return SpeakResult(accepted=bool(ok), completed=bool(ok), samples=len(pcm) if ok else 0)


class _LLM:
    name = "fake-llm"

    def __init__(self):
        self.calls: list[str] = []

    def respond(self, user_text, history=None):
        self.calls.append(user_text)
        return "I can help with that."


def build(monkeypatch, said):
    """A real Day1Spike with fake IO -- real router, real ToolRunner, real database, real fencing."""
    from aether.audio.player import AudioGate
    from aether.spike import HANDS_FREE, Day1Spike

    trace = Trace()
    rime, llm = _Rime(), _LLM()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: AudioGate(trace))
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _Mic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: _STT(said))
    monkeypatch.setattr("aether.spike.build_llm", lambda: llm)

    def _fake_tts(_trace, samplerate=48000, language=None):
        # One speaker per language, exactly as the real factory does, sharing the `spoken` list so
        # a caller still sees everything said across a switch.
        return rime if language is None else _Rime(language, spoken=rime.spoken)

    monkeypatch.setattr("aether.spike.build_tts", _fake_tts)
    spike = Day1Spike(trace, input_mode=HANDS_FREE)
    spike.rime = rime
    monkeypatch.setattr(spike, "_streaming_enabled", False)
    return spike, trace, rime, llm


def test_what_starters_do_you_have(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "what starters do you have")
    spike.handle_utterance(AUDIO, 0.0)
    for item in STORE.in_category("starters"):
        assert item.name in rime.spoken[0]
    assert llm.calls == [], "a hotel fact must not reach the model"


def test_how_much_is_the_chicken_kebab(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "how much is the chicken kebab")
    spike.handle_utterance(AUDIO, 0.0)
    item = STORE.find_item("chicken kebab")
    assert rime.spoken == [f"The {item.name} is {say_price(item.price)}."]
    assert llm.calls == []


def test_do_you_have_vegetarian_mains(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "do you have vegetarian mains")
    spike.handle_utterance(AUDIO, 0.0)
    assert "Butter Chicken" not in rime.spoken[0], "a non-vegetarian dish must not appear"
    assert llm.calls == []


def test_a_sold_out_dish_is_reported_as_such(monkeypatch):
    sold_out = next(i for i in STORE.menu() if not i.available)
    spike, _t, rime, llm = build(monkeypatch, f"is the {sold_out.name.lower()} available")
    spike.handle_utterance(AUDIO, 0.0)
    assert "not available" in rime.spoken[0]
    assert llm.calls == []


def test_a_room_question_is_answered_without_the_model(monkeypatch):
    room = next(r for r in STORE.rooms() if r.status == "available")
    spike, _t, rime, llm = build(monkeypatch, f"is room {room.number} free")
    spike.handle_utterance(AUDIO, 0.0)
    assert "free" in rime.spoken[0]
    assert llm.calls == []


def test_check_in_time_is_answered_without_the_model(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "what time is check in")
    spike.handle_utterance(AUDIO, 0.0)
    assert "Check-in" in rime.spoken[0]
    assert llm.calls == []


def test_an_unknown_menu_item_is_admitted_not_invented(monkeypatch):
    """The router does not know "lobster thermidor", so the model takes it -- and must not lie."""
    spike, _t, rime, llm = build(monkeypatch, "how much is the lobster thermidor")
    spike.handle_utterance(AUDIO, 0.0)
    assert llm.calls, "an unrecognised dish falls through to the model"
    assert rime.spoken == ["I can help with that."]


def test_conversational_fallback_reaches_the_model(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "can i book a table for eight")
    spike.handle_utterance(AUDIO, 0.0)
    assert llm.calls == ["can i book a table for eight"]


# --- observability and fencing on the hotel path ---

def test_the_hotel_path_is_observable_in_the_trace(monkeypatch):
    spike, trace, _rime, _llm = build(monkeypatch, "what starters do you have")
    spike.handle_utterance(AUDIO, 0.0)

    kinds = [e.type for e in trace.events]
    assert "TaskStarted" in kinds and "ResultReceived" in kinds
    assert "ResponseSpoken" in kinds
    received = trace.last(EventType.RESULT_RECEIVED)
    assert received.fields["task"] == "list_category"
    assert received.fields["mutates"] is False, "the database is read-only"


def test_a_hotel_turn_is_remembered_like_any_other(monkeypatch):
    spike, _t, _rime, _llm = build(monkeypatch, "how much is the chicken kebab")
    spike.handle_utterance(AUDIO, 0.0)
    assert spike.history.turn_count() == 1


def test_a_fenced_lookup_speaks_nothing(monkeypatch):
    """The golden invariant on the database path."""
    spike, trace, rime, _llm = build(monkeypatch, "what starters do you have")

    original = spike.tools.run

    def fence_then_run(*args, **kwargs):
        result = original(*args, **kwargs)
        spike.gens.mark_fenced(spike.gens.active.id if spike.gens.active else None)
        return result

    monkeypatch.setattr(spike.tools, "run", fence_then_run)
    spike.handle_utterance(AUDIO, 0.0)

    assert rime.spoken == [], "a fenced turn must not reach Rime"
    assert trace.all(EventType.RESULT_LEAKED) == []
    assert spike.history.messages() == [], "and must not be remembered"


# ============================ broad menu questions ============================

BROAD_MENU_QUESTIONS = [
    "What type of dishes are available?",
    "What type of dishes are available on the table?",
    "What dishes do you have?",
    "What food do you have?",
    "What's on the menu?",
    "What can I order?",
    "What food is available?",
    "What kind of dishes do you serve?",
    "What are your dining options?",
    "What do you have to eat?",
    "Can you tell me about the food?",
]


@pytest.mark.parametrize("said", BROAD_MENU_QUESTIONS)
def test_a_broad_menu_question_routes_to_the_menu(said):
    decision = route(said)
    assert decision is not None, f"{said!r} must not need the model"
    assert decision.tool == "menu_overview"


def test_what_type_of_dishes_are_available(monkeypatch):
    """The exact sentence from a live call, through the real pipeline."""
    spike, _t, rime, llm = build(monkeypatch, "What type of dishes are available on the table?")
    spike.handle_utterance(AUDIO, 0.0)
    assert llm.calls == []
    said = rime.spoken[0].lower()
    for category in STORE.categories():
        assert category.lower() in said, category


def test_the_overview_never_asks_which_restaurant(monkeypatch):
    """The specific wrong answer from a live call. One hotel, one menu, nothing to choose."""
    spike, _t, rime, _llm = build(monkeypatch, "What's on the menu?")
    spike.handle_utterance(AUDIO, 0.0)
    said = rime.spoken[0].lower()
    for wrong in ("which restaurant", "which outlet", "which branch", "let me know which"):
        assert wrong not in said


def test_the_overview_is_short_enough_to_say_on_a_phone(monkeypatch):
    spike, _t, rime, _llm = build(monkeypatch, "What food do you have?")
    spike.handle_utterance(AUDIO, 0.0)
    said = rime.spoken[0]
    assert len(said.split()) <= 40, said
    assert "Chicken Kebab" not in said, "courses and diets, not a recitation of the menu"


# ============================ the hallucinated dessert menu ============================
#
# From a real call. The caller asked for the desserts, `base.en` transcribed "dessert" as "Desert",
# the router matched nothing, the model answered from nothing -- and invented three dishes and
# three prices. None existed.

@pytest.mark.parametrize("said", [
    "Tell me all the possible things available in Desert.",     # the exact STT output
    "tell me all the possible things available in dessert",     # and spelled correctly
    "what deserts do you have",
    "show me the desserts",
    "give me the dessert options",
    "read me the dessert menu",
    "what kind of desserts do you serve",
])
def test_a_dessert_question_never_reaches_the_model(said):
    decision = route(said)
    assert decision is not None, f"{said!r} must be answered from the database"
    assert decision.tool in ("list_category", "menu_overview")


def test_the_stt_misspelling_maps_to_desserts():
    """One missing letter must not be able to cause a fabricated menu."""
    decision = route("what is available in desert")
    assert decision is not None and decision.params.get("category") == "desserts"


def test_the_real_desserts_are_what_gets_spoken(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "Tell me all the possible things available in Desert.")
    spike.handle_utterance(AUDIO, 0.0)

    said = rime.spoken[0]
    assert llm.calls == []
    for item in STORE.in_category("desserts"):
        assert item.name in said
    for invented in ("fudge cake", "apple crumble", "fruit salad"):
        assert invented not in said.lower(), f"{invented} does not exist"


def test_the_model_is_told_it_cannot_complete_transactions():
    """"I have added the chocolate fudge cake to your order" -- there is no order system, and the
    database is read-only."""
    from aether.llm import SYSTEM_PROMPT

    p = SYSTEM_PROMPT.lower()
    assert "cannot complete transactions" in p
    for claim in ("added", "placed", "booked", "confirmed"):
        assert claim in p
