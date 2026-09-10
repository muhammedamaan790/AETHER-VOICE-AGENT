"""What "it" refers to, and the three ways that must not go wrong.

The demo sheet carried a rule telling the presenter never to use a pronoun, because "how much is the
chicken kebab?" followed by "is it available tonight?" answered *"we have forty-one rooms free"* --
confidently, about the wrong table. A rule telling a human to avoid a defect is not a fix.

Memory is also where a deterministic router starts guessing, so most of this file is about the times
it must NOT fire. Three properties, in order of how much damage getting them wrong would do:

1. **A named subject always wins.** Memory fills a gap; it never argues with the sentence.
2. **A fenced turn leaves no subject.** "It" can only mean something the caller actually heard --
   the golden invariant applied to reference instead of to output.
3. **Uncertainty falls through to the model.** No referring word, or nothing remembered, means the
   sentence is left exactly as it was.
"""

from __future__ import annotations

import pytest

from aether.events import EventType
from aether.hotel.context import Subject, refers_back, resolve
from aether.hotel.router import route, subject_of
from tests.test_menu_routing import AUDIO, build


def _remember(question: str) -> Subject:
    """The subject left behind by answering `question`."""
    subject = Subject()
    subject.remember(*subject_of(route(question), question))
    return subject


# --- the defect itself --------------------------------------------------------------------------

def test_a_pronoun_after_a_dish_question_is_still_about_the_dish() -> None:
    """The exact sequence from the demo sheet's warning."""
    subject = _remember("how much is the chicken kebab")
    assert subject.phrase == "chicken kebab"

    decision = route("is it available tonight", subject)
    assert decision is not None
    assert decision.tool == "check_availability", f"answered with {decision.tool}"
    assert decision.params["dish"] == "chicken kebab"


@pytest.mark.parametrize(("follow_up", "tool"), [
    ("does it contain nuts", "check_allergens"),
    ("is it available", "check_availability"),
    ("tell me about it", "describe_item"),
    ("what is in that", "describe_item"),
])
def test_follow_ups_about_a_remembered_dish(follow_up: str, tool: str) -> None:
    subject = _remember("how much is the chicken kebab")
    decision = route(follow_up, subject)
    assert decision is not None, f"{follow_up!r} fell through"
    assert decision.tool == tool
    assert decision.params.get("dish") == "chicken kebab"


def test_a_room_type_can_be_referred_back_to() -> None:
    subject = _remember("how much is an executive suite")
    assert subject.kind == "room_type"
    decision = route("what comes with it", subject)
    assert decision is not None and decision.tool == "room_amenities"
    assert decision.params["room_type"] == "Executive Suite"


# --- the sentence in front of you always wins ----------------------------------------------------

@pytest.mark.parametrize(("said", "expected_dish"), [
    ("how much is the paneer tikka", "paneer tikka"),
    ("is the fish curry available", "fish curry"),
])
def test_a_named_subject_beats_the_remembered_one(said: str, expected_dish: str) -> None:
    """Memory fills a gap. It never overrides what the caller actually said."""
    subject = _remember("how much is the chicken kebab")
    decision = route(said, subject)
    assert decision is not None
    assert decision.params["dish"] == expected_dish, "the remembered subject overrode a named one"


def test_a_different_kind_of_question_is_not_captured_by_the_subject() -> None:
    subject = _remember("how much is the chicken kebab")
    for said, tool in [("do you have parking", "hotel_policy"),
                       ("what time is check in", "check_in_out"),
                       ("what rooms do you have", "list_room_types")]:
        decision = route(said, subject)
        assert decision is not None and decision.tool == tool, f"{said!r} -> {decision}"


# --- uncertainty falls through -------------------------------------------------------------------

def test_a_sentence_with_no_referring_word_is_left_alone() -> None:
    """An unmarked fragment is not a reference. Guessing what it points at is the confident-error
    class this whole router is built to avoid, so it goes to the model instead."""
    subject = _remember("how much is the chicken kebab")
    assert resolve("what about tomorrow", subject) == "what about tomorrow"


def test_nothing_remembered_means_nothing_substituted() -> None:
    assert resolve("is it available", Subject()) == "is it available"
    assert route("is it available", Subject()) != route("is chicken kebab available")


def test_the_router_without_a_subject_behaves_exactly_as_before() -> None:
    """Every existing routing test calls `route(text)` with one argument. The subject parameter
    must be genuinely optional, or this feature silently rewrote the whole suite's meaning."""
    for said in ["what starters do you have", "how much is the chicken kebab",
                 "is it available tonight", "do you have a swimming pool"]:
        assert route(said) == route(said, None)


def test_the_word_one_is_not_treated_as_a_reference() -> None:
    """"one" appears in "room one zero one" and "one night". Treating it as a pronoun would make
    room numbers ambiguous with dishes."""
    assert not refers_back("is room one zero one free")
    assert not refers_back("we are staying one night")


# --- a fenced turn leaves nothing behind ----------------------------------------------------------

def test_a_spoken_turn_commits_its_subject(monkeypatch) -> None:
    spike, _trace, _rime, _llm = build(monkeypatch, "how much is the chicken kebab")
    spike.handle_utterance(AUDIO, 0.0)
    assert spike.subject.phrase == "chicken kebab"


def test_an_interrupted_turn_leaves_no_subject_to_refer_back_to(monkeypatch) -> None:
    """The invariant, applied to reference. The caller never heard the answer, so "it" cannot mean
    it -- exactly as the abandoned turn never enters `history`."""
    from aether.hotel.tools import HOTEL_TOOLS
    from aether.tools import ToolRunner

    spike, trace, _rime, _llm = build(monkeypatch, "how much is the chicken kebab")

    def caller_interrupts(_seconds):
        spike.barge.on_speech_onset()
        spike.barge.on_voiced_progress(400.0)

    spike.tools = ToolRunner(spike.trace, spike.menu, tools=HOTEL_TOOLS,
                             delay_ms=500.0, sleep=caller_interrupts)
    spike.handle_utterance(AUDIO, 0.0)

    assert trace.all(EventType.RESULT_LEAKED) == []
    assert not spike.subject, "a turn the caller never heard became what 'it' refers to"


def test_a_fenced_turns_subject_cannot_be_committed_by_the_next_turn(monkeypatch) -> None:
    """The subtle leak: the fenced turn returns before the commit, so its pending subject would
    still be sitting there when the NEXT turn reaches the spoken boundary."""
    from aether.hotel.tools import HOTEL_TOOLS
    from aether.tools import ToolRunner

    spike, _trace, _rime, _llm = build(monkeypatch, "how much is the chicken kebab")

    def caller_interrupts(_seconds):
        spike.barge.on_speech_onset()
        spike.barge.on_voiced_progress(400.0)

    spike.tools = ToolRunner(spike.trace, spike.menu, tools=HOTEL_TOOLS,
                             delay_ms=500.0, sleep=caller_interrupts)
    spike.handle_utterance(AUDIO, 0.0)          # fenced; nothing heard

    # A second turn the model handles, so it commits without setting a subject of its own.
    spike.tools = ToolRunner(spike.trace, spike.menu, tools=HOTEL_TOOLS)
    monkeypatch.setattr(spike.stt, "text", "hello how are you", raising=False)
    spike.handle_utterance(AUDIO, 0.0)

    assert spike.subject.phrase != "chicken kebab", (
        "the abandoned turn's subject was committed by a later turn"
    )


def test_the_subject_updates_across_two_spoken_turns(monkeypatch) -> None:
    """It is the LAST thing heard, not the first."""
    spike, _trace, _rime, _llm = build(monkeypatch, "how much is the chicken kebab")
    spike.handle_utterance(AUDIO, 0.0)
    assert spike.subject.phrase == "chicken kebab"

    monkeypatch.setattr(spike.stt, "text", "how much is an executive suite", raising=False)
    spike.handle_utterance(AUDIO, 0.0)
    assert spike.subject.phrase == "executive suite"
