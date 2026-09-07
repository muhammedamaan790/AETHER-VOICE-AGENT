"""Deterministic menu routing, and the six phrases the demo depends on.

Two things are under test:

* **the router** -- does this sentence name a tool, and the right one? It must answer only when
  confident and return None otherwise, because a wrong tool call is worse than a slower answer.
* **the wired path** -- `Day1Spike._menu_answer` running the real `ToolRunner` against the real
  menu, with real fencing.

STT output is not tidy, so the phrasings here are deliberately messy: filler words, plurals,
missing punctuation, and the way people actually ask on a phone.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.events import EventType
from aether.hotel.router import Route, normalise, route
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)


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
    """"The chicken kebab?" on a phone means "tell me about it", and price is the useful answer."""
    decision = route("the chicken kebab")
    assert decision is not None and decision.tool == "price_of"


@pytest.mark.parametrize(("said", "expected_diet"), [
    ("do you have vegetarian mains", "vegetarian"),
    ("any vegan options", "vegan"),
    ("i want something non vegetarian", "non-vegetarian"),
])
def test_dietary_questions_route_to_find_by_diet(said, expected_diet):
    decision = route(said)
    assert decision is not None and decision.tool == "find_by_diet"
    assert decision.params["diet"] == expected_diet


def test_a_diet_question_carries_the_category_when_given():
    decision = route("do you have vegetarian mains")
    assert decision.params.get("category") == "mains"


@pytest.mark.parametrize("said", [
    "is the seafood platter available",
    "do you still have the seafood platter",
    "is the seafood platter sold out",
])
def test_availability_questions_route_to_check_availability(said):
    decision = route(said)
    assert decision is not None and decision.tool == "check_availability"
    assert decision.params["dish"] == "seafood platter"


def test_allergen_questions_win_over_price():
    """Specificity order matters: "does X contain nuts" also looks price-ish and list-ish."""
    decision = route("does the butter chicken contain nuts")
    assert decision is not None and decision.tool == "check_allergens"
    assert decision.params["dish"] == "butter chicken"


def test_the_longest_dish_name_wins():
    """"paneer butter masala" must not be answered as "paneer tikka" -- both contain "paneer"."""
    decision = route("how much is the paneer butter masala")
    assert decision.params["dish"] == "paneer butter masala"


def test_spice_questions_route_to_find_by_spice():
    decision = route("do you have anything mild")
    assert decision is not None and decision.tool == "find_by_spice"
    assert decision.params["spice"] == "mild"


@pytest.mark.parametrize("said", [
    "what time do you close",
    "can i book a table for eight",
    "hello",
    "is there parking",
    "my name is daniel",
    "",
    "   ",
])
def test_anything_the_router_is_not_confident_about_falls_through(said):
    """None means "let the model handle it". A wrong tool is worse than a slower answer."""
    assert route(said) is None


def test_the_router_only_names_a_tool_and_never_runs_one():
    """Routing and execution stay separate so the router can never bypass fencing."""
    import io
    import inspect
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
    assert "MenuStore" not in code, "it reads the fixture, it does not own state"
    assert isinstance(route("what starters do you have"), Route)


# ============================ the wired path ============================

class _Mic:
    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None
        self.listening = True

    def set_listening(self, value):
        self.listening = value

    def set_context(self, **k): ...


class _STT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio, *, turn_id=None, gen=None):
        return self.text


class _Rime:
    name, transport = "rime", "fake"

    def __init__(self):
        self.spoken: list[str] = []
        self.config = type("c", (), {"model": "mistv3", "voice": "astra"})()
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
    """A real Day1Spike with fake IO -- real router, real ToolRunner, real menu, real fencing."""
    from aether.audio.player import AudioGate
    from aether.spike import HANDS_FREE, Day1Spike

    trace = Trace()
    rime, llm = _Rime(), _LLM()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: AudioGate(trace))
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _Mic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: _STT(said))
    monkeypatch.setattr("aether.spike.build_llm", lambda: llm)
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: rime)
    spike = Day1Spike(trace, input_mode=HANDS_FREE)
    spike.rime = rime
    monkeypatch.setattr(spike, "_streaming_enabled", False)
    return spike, trace, rime, llm


# --- the six phrases the brief requires ---

def test_what_starters_do_you_have(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "what starters do you have")
    spike.handle_utterance(AUDIO, 0.0)
    assert rime.spoken and "chicken kebab" in rime.spoken[0]
    assert llm.calls == [], "a menu fact must not reach the model"


def test_how_much_is_the_chicken_kebab(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "how much is the chicken kebab")
    spike.handle_utterance(AUDIO, 0.0)
    assert rime.spoken == ["The chicken kebab is three hundred and eighty rupees."]
    assert llm.calls == []


def test_do_you_have_vegetarian_mains(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "do you have vegetarian mains")
    spike.handle_utterance(AUDIO, 0.0)
    assert rime.spoken and "dal makhani" in rime.spoken[0]
    assert "butter chicken" not in rime.spoken[0]
    assert llm.calls == []


def test_is_the_seafood_platter_available(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "is the seafood platter available")
    spike.handle_utterance(AUDIO, 0.0)
    assert rime.spoken and "not available" in rime.spoken[0]
    assert llm.calls == []


def test_an_unknown_menu_item_is_admitted_not_invented(monkeypatch):
    """The router does not know "lobster thermidor", so the model takes it -- and must not lie."""
    spike, _t, rime, llm = build(monkeypatch, "how much is the lobster thermidor")
    spike.handle_utterance(AUDIO, 0.0)
    assert llm.calls, "an unrecognised dish falls through to the model"
    assert rime.spoken == ["I can help with that."]


def test_conversational_fallback_reaches_the_model(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "what time do you close")
    spike.handle_utterance(AUDIO, 0.0)
    assert llm.calls == ["what time do you close"]


# --- observability and fencing on the menu path ---

def test_the_menu_path_is_observable_in_the_trace(monkeypatch):
    spike, trace, _rime, _llm = build(monkeypatch, "what starters do you have")
    spike.handle_utterance(AUDIO, 0.0)

    kinds = [e.type for e in trace.events]
    assert "TaskStarted" in kinds and "ResultReceived" in kinds
    assert "ResponseSpoken" in kinds
    received = trace.last(EventType.RESULT_RECEIVED)
    assert received.fields["task"] == "list_category"
    assert received.fields["mutates"] is False


def test_a_menu_turn_is_remembered_like_any_other(monkeypatch):
    spike, _t, _rime, _llm = build(monkeypatch, "how much is the chicken kebab")
    spike.handle_utterance(AUDIO, 0.0)
    assert spike.history.turn_count() == 1


def test_a_fenced_menu_lookup_speaks_nothing(monkeypatch):
    """The golden invariant on the menu path."""
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


# ============================ the two questions the phrase list exposed ============================
#
# Both were real defects found by walking the demo's required phrases rather than by testing what
# was already built. "Is the chicken kebab spicy?" named a dish with no price words, so the
# bare-dish rule answered a question about heat with a price. "I have a nut allergy, what can I
# eat?" named no dish at all, so it fell through to the model -- which is the one class of menu
# question that must never be answered from model knowledge.

@pytest.mark.parametrize("said", [
    "is the chicken kebab spicy",
    "how hot is the chicken kebab",
    "is the chicken kebab mild",
])
def test_asking_how_hot_a_dish_is_no_longer_returns_its_price(said):
    decision = route(said)
    assert decision is not None and decision.tool == "spice_of"
    assert decision.params["dish"] == "chicken kebab"


@pytest.mark.parametrize(("said", "allergen"), [
    ("i have a nut allergy what can i eat", "nuts"),
    ("im allergic to shellfish", "shellfish"),
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
    assert route("what fish do you have") is None


def test_naming_a_dish_still_wins_over_the_allergy_rule():
    """"Does the butter chicken contain nuts" is about that dish, not about the whole menu."""
    decision = route("does the butter chicken contain nuts")
    assert decision.tool == "check_allergens"


def test_an_allergy_question_can_still_be_narrowed_to_a_category():
    decision = route("i cannot eat gluten what mains do you have")
    assert decision.tool == "safe_for"
    assert decision.params["category"] == "mains"


def test_is_the_chicken_kebab_spicy(monkeypatch):
    spike, _t, rime, llm = build(monkeypatch, "is the chicken kebab spicy")
    spike.handle_utterance(AUDIO, 0.0)
    assert rime.spoken == ["The chicken kebab is medium spiced."]
    assert llm.calls == [], "a menu fact must not reach the model"


def test_a_nut_allergy_is_answered_from_the_fixture_not_the_model(monkeypatch):
    """The one menu answer that could actually hurt somebody. It never goes to a language model."""
    spike, _t, rime, llm = build(monkeypatch, "i have a nut allergy what can i eat")
    spike.handle_utterance(AUDIO, 0.0)

    said = rime.spoken[0]
    assert llm.calls == []
    assert "avoiding nuts" in said
    for unsafe in ("mushroom galouti", "beetroot carpaccio", "butter chicken",
                   "jackfruit rendang", "paneer butter masala", "pistachio kulfi"):
        assert unsafe not in said, f"{unsafe} contains nuts and must not be suggested"


def test_a_long_menu_answer_is_capped_so_it_can_be_said_on_a_phone(monkeypatch):
    """Eight dish names read aloud is a wall of speech. The remainder is counted, not dropped."""
    spike, _t, rime, _llm = build(monkeypatch, "what starters do you have")
    spike.handle_utterance(AUDIO, 0.0)
    said = rime.spoken[0]
    assert "plus two more" in said
    assert "chilli garlic squid" not in said, "the tail is summarised, not read out"
