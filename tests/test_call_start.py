"""The first ten seconds of a phone call, pinned to two calls that went wrong on 2026-09-10.

Call one: the log said "greeting spoken", 12.5 seconds of audio left the pipeline, and the caller
heard none of the greeting. It had started one millisecond after AETHER's audio track was
published -- before the phone had subscribed to it.

Call two: the caller spoke and heard nothing back; the captured inbound audio was 33 seconds of
digital silence, peak 10. Nothing AETHER could do would have understood that call, but it could
have said something, and it could have told the operator what was wrong.

And on call one, the only word of the caller's that arrived was "menu" -- to which AETHER, newly
taught to ask for a repeat when it hears noise, replied "Sorry, I did not catch that."
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from aether.events import EventType
from aether.trace import Trace


# --- 1. greet only once the caller can hear it -------------------------------------------------------

class _Publication:
    """Stands in for a LiveKit LocalTrackPublication."""

    def __init__(self, behaviour: str):
        self.behaviour = behaviour

    async def wait_for_subscription(self) -> None:
        if self.behaviour == "subscribes":
            await asyncio.sleep(0.01)
        elif self.behaviour == "never":
            await asyncio.sleep(60)
        else:
            raise RuntimeError("the SDK could not say")


def test_the_greeting_waits_for_the_callers_phone_to_subscribe() -> None:
    from aether.telephony.agent import wait_until_heard

    assert asyncio.run(wait_until_heard(_Publication("subscribes"), 1.0, 0.0)) is True


def test_a_subscription_that_never_comes_greets_anyway_rather_than_leaving_silence() -> None:
    from aether.telephony.agent import wait_until_heard

    assert asyncio.run(wait_until_heard(_Publication("never"), 0.05, 0.0)) is False


def test_an_sdk_that_cannot_say_never_takes_the_call_down() -> None:
    from aether.telephony.agent import wait_until_heard

    assert asyncio.run(wait_until_heard(_Publication("raises"), 0.5, 0.0)) is False


def test_the_wait_happens_after_publishing_and_before_the_greeting() -> None:
    """Read as source, like the other ordering tests in `test_telephony.py`: the entry point needs a
    live LiveKit room, and the property is the ORDER of three statements in it."""
    from aether.telephony import agent

    src = inspect.getsource(agent)
    publish = src.index("publication = await ctx.room.local_participant.publish_track(")
    wait = src.index("heard = await wait_until_heard(publication)")
    greet = src.index("spoken = await asyncio.to_thread(speak_greeting, spike)")
    assert publish < wait < greet


# --- 2. say something if nothing arrives -------------------------------------------------------------

def _trace_at(*events: tuple[EventType, float]) -> Trace:
    trace = Trace()
    for kind, t in events:
        trace.emit(kind, t=t, text="x" if kind == EventType.TRANSCRIPT_FINAL else None)
    return trace


def test_silence_after_the_greeting_is_noticed() -> None:
    from aether.telephony.agent import caller_has_been_silent

    assert caller_has_been_silent(_trace_at(), since_t=1000.0, now_t=9500.0, window_ms=8000.0)


def test_a_transcript_means_the_caller_was_heard() -> None:
    from aether.telephony.agent import caller_has_been_silent

    trace = _trace_at((EventType.TRANSCRIPT_FINAL, 4000.0))
    assert not caller_has_been_silent(trace, since_t=1000.0, now_t=9500.0, window_ms=8000.0)


def test_it_does_not_speak_before_the_window_has_passed() -> None:
    from aether.telephony.agent import caller_has_been_silent

    assert not caller_has_been_silent(_trace_at(), since_t=1000.0, now_t=5000.0, window_ms=8000.0)


def test_a_noise_blip_does_not_count_as_the_caller_speaking() -> None:
    """The failed call's first "speech" was a 14-RMS blip. An onset with no transcript is not a
    caller, and must not suppress the line check."""
    from aether.telephony.agent import caller_has_been_silent

    trace = _trace_at((EventType.SPEECH_ONSET, 2700.0))
    assert caller_has_been_silent(trace, since_t=1000.0, now_t=9500.0, window_ms=8000.0)


def test_someone_mid_sentence_is_never_talked_over() -> None:
    from aether.telephony.agent import caller_has_been_silent

    trace = _trace_at((EventType.SPEECH_ONSET, 8800.0))
    assert not caller_has_been_silent(trace, since_t=1000.0, now_t=9500.0, window_ms=8000.0)


def test_the_line_check_is_spoken_once_when_the_caller_is_silent(monkeypatch) -> None:
    from aether.lang import LINE_CHECK
    from aether.telephony import agent

    said: list[str] = []
    monkeypatch.setattr(agent, "speak_greeting", lambda spike, text: said.append(text) or True)
    spoke = asyncio.run(agent.reprompt_if_silent(object(), Trace(), since_t=agent._now_ms(),
                                                 window_ms=10.0))
    assert spoke is True
    assert said == [LINE_CHECK]


def test_no_line_check_once_the_call_is_closing(monkeypatch) -> None:
    import threading

    from aether.telephony import agent

    said: list[str] = []
    monkeypatch.setattr(agent, "speak_greeting", lambda spike, text: said.append(text) or True)
    closing = threading.Event()
    closing.set()
    assert asyncio.run(agent.reprompt_if_silent(object(), Trace(), agent._now_ms(), closing,
                                                window_ms=10.0)) is False
    assert said == []


def test_the_line_check_repeats_the_question_and_is_speakable() -> None:
    """It covers a lost greeting as well as a silent line, so it has to carry the question."""
    import re

    from aether.lang import LINE_CHECK

    for word in ("English", "Hindi", "Spanish"):
        assert word in LINE_CHECK
    assert not re.search(r"\d", LINE_CHECK)


def test_the_line_check_is_wired_into_the_call() -> None:
    from aether.telephony import agent

    assert "asyncio.create_task(reprompt_if_silent(" in inspect.getsource(agent)


# --- 3. the one word that did arrive ----------------------------------------------------------------

@pytest.mark.parametrize(("said", "tool"), [
    ("menu", "menu_overview"),
    ("Menu.", "menu_overview"),
    ("the menu please", "menu_overview"),
    ("rooms", "list_room_types"),
])
def test_a_bare_noun_is_answered(said: str, tool: str) -> None:
    from aether.hotel.router import route

    decision = route(said)
    assert decision is not None and decision.tool == tool


@pytest.mark.parametrize("said", ["menu", "rooms", "price", "booking", "the menu"])
def test_a_hotel_word_is_never_treated_as_noise(said: str) -> None:
    from aether.hotel.clarify import sounds_like_a_recognition_failure

    assert not sounds_like_a_recognition_failure(said)


def test_the_real_failure_still_asks_for_a_repeat() -> None:
    from aether.hotel.clarify import sounds_like_a_recognition_failure

    for said in ("grr", "um", "", "q"):
        assert sounds_like_a_recognition_failure(said)


def test_the_bare_noun_rule_captures_nothing_larger() -> None:
    """Only a whole utterance that IS the noun. A sentence that mentions the menu is handled by the
    rules above, or deliberately left to the model."""
    from aether.hotel.router import route

    for said in ("is the food good", "where is the food court", "rooms with a view"):
        decision = route(said)
        assert decision is None or decision.reason not in ("bare menu", "bare rooms")
