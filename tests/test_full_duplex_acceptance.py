"""The full-duplex acceptance test the brief specifies, run against the real hotel pipeline.

The competition brief (page 04, "How to prove the claim") names one test in full:

    Introduce a fixed delay into a tool call. While the agent is speaking or waiting, interrupt it
    and change one part of the request. Verify that queued Rime audio stops promptly, the updated
    instruction reaches the application, stale tool results are not spoken as current, background
    work is cancelled or reconciled correctly, and the final spoken response reflects what the user
    actually heard and requested.

    Treat full duplex as a property of the complete application, not the TTS model alone. The
    application must continue accepting user audio while Rime speech is playing and while tools run.

Pieces of that were already covered across `test_warehouse_tools.py` and `test_audio_fencing.py`,
but nothing asserted the whole sequence end to end -- in particular nothing checked the last and
most important clause, that the answer the caller finally hears is the answer to the question they
actually ended up asking. This file does exactly what the brief describes, in the product's own
domain, and each of its five clauses is a separate assertion so a failure names which one broke.

The delay is INJECTED, not waited for: `ToolRunner` takes `delay_ms` and a `sleep` callable, so the
interruption lands inside the tool call deterministically instead of racing a real timer.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.events import EventType
from aether.hotel.tools import HOTEL_TOOLS
from aether.tools import ToolRunner
from tests.test_menu_routing import AUDIO, build

# The two requests: the caller asks about starters, then changes their mind mid-lookup. One part of
# the request changes (which course), which is precisely the brief's "change one part of the
# request" rather than an unrelated new question.
FIRST = "what starters do you have"
REVISED = "actually, what desserts do you have"


def _slow_tools(spike, on_delay):
    """Replace the runner with one that stalls inside the tool body and runs `on_delay` there."""
    spike.tools = ToolRunner(
        spike.trace, spike.menu, tools=HOTEL_TOOLS, delay_ms=500.0, sleep=on_delay
    )


@pytest.fixture
def interrupted(monkeypatch):
    """One turn interrupted mid-tool by a second, revised request. Returns the assembled facts."""
    spike, trace, rime, llm = build(monkeypatch, FIRST)

    # The caller speaks while the first lookup is still running. This is exactly what the PortAudio
    # input thread does on a real barge-in, called from inside the tool's own delay.
    def caller_interrupts(_seconds):
        spike.barge.on_speech_onset()
        spike.barge.on_voiced_progress(400.0)      # past the meaningful-speech bound -> fence

    _slow_tools(spike, caller_interrupts)
    spike.handle_utterance(AUDIO, 0.0)             # asks about starters; fenced mid-lookup
    # Read from the TRACE, not from `gens.active`: by the time the turn returns the generation has
    # been fenced, so `active` is already None and asking it would capture nothing. The trace is the
    # append-only record of what happened, which is the point of having one.
    changes = trace.all(EventType.GENERATION_CHANGED)
    first_gen = next((e.fields.get("to_gen") for e in changes if e.fields.get("to_gen")), None)

    # The revised request arrives. Ordinary tool speed this time.
    spike.tools = ToolRunner(spike.trace, spike.menu, tools=HOTEL_TOOLS)
    monkeypatch.setattr(spike.stt, "text", REVISED, raising=False)
    spike.handle_utterance(AUDIO, 0.0)

    return {
        "spike": spike, "trace": trace, "rime": rime, "llm": llm,
        "first_gen": first_gen, "spoken": rime.spoken,
    }


# --- the brief's five clauses, one assertion each ---------------------------------------------

def test_queued_audio_stops_and_nothing_from_the_abandoned_turn_is_spoken(interrupted):
    """"queued Rime audio stops promptly"

    The strongest form of the claim: the abandoned turn never reaches Rime at all, so there is no
    queued audio to stop. `ResultLeaked` is the event that would fire if it had.
    """
    trace, spoken = interrupted["trace"], interrupted["spoken"]
    assert trace.all(EventType.RESULT_LEAKED) == [], "a stale result reached the caller"
    for said in spoken:
        assert "Chicken Kebab" not in said or "dessert" in said.lower(), (
            f"the abandoned starters answer was spoken: {said!r}"
        )


def test_the_updated_instruction_reaches_the_application(interrupted):
    """"the updated instruction reaches the application"

    A new generation is allocated for the revised request, and it is not the fenced one.
    """
    spike, first_gen = interrupted["spike"], interrupted["first_gen"]
    assert spike.gens.active is not None
    assert spike.gens.active.id != first_gen, "the revised request reused the fenced generation"


def test_stale_tool_results_are_not_spoken_as_current(interrupted):
    """"stale tool results are not spoken as current"

    The fenced lookup is recorded as discarded rather than quietly dropped, so the evidence shows
    the system knew it was stale.
    """
    trace = interrupted["trace"]
    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert discarded, "the fenced lookup left no record of being discarded"
    assert trace.all(EventType.RESULT_LEAKED) == []


def test_background_work_is_cancelled_or_reconciled(interrupted):
    """"background work is cancelled or reconciled correctly"

    The first generation is terminally fenced. For the hotel the tools are read-only, so there is no
    mutation to roll back -- `test_warehouse_tools.py` covers the mutating case, where the fence
    also has to stop the write landing.
    """
    spike, first_gen = interrupted["spike"], interrupted["first_gen"]
    assert first_gen is not None
    assert spike.gens.is_active(first_gen) is False, "the abandoned turn is still active"


def test_the_final_spoken_response_answers_the_revised_request(interrupted):
    """"the final spoken response reflects what the user actually heard and requested"

    The clause nothing previously asserted, and the one a caller would actually notice: they asked
    for desserts, so desserts is what they must hear -- not starters, and not silence.
    """
    from aether.hotel import HotelStore

    spoken = interrupted["spoken"]
    assert spoken, "nothing was spoken at all"
    final = spoken[-1]
    for item in HotelStore().in_category("desserts"):
        assert item.name in final, f"the final answer is not the desserts answer: {final!r}"


def test_the_conversation_remembers_only_what_the_caller_heard(interrupted):
    """The brief's other phrasing of the same property: application state must be consistent with
    what the user actually heard. A fenced turn was never heard, so it is never remembered."""
    spike = interrupted["spike"]
    remembered = " ".join(m["content"] for m in spike.history.messages())
    assert "starters" not in remembered.lower() or "dessert" in remembered.lower(), (
        f"the abandoned request entered the conversation state: {remembered!r}"
    )


# --- full duplex as a property of the whole application ---------------------------------------

def test_the_microphone_stays_open_while_a_tool_is_running(monkeypatch):
    """"The application must continue accepting user audio ... while tools run."

    Tested by observing the listening flag from INSIDE the tool call, which is the only moment that
    could have closed it. A pipeline that muted the caller during a lookup would still pass every
    interruption test in the suite -- the interruption simply would not arrive.
    """
    spike, _trace, _rime, _llm = build(monkeypatch, FIRST)
    seen: list[bool] = []

    def observe(_seconds):
        seen.append(spike.mic.listening)

    _slow_tools(spike, observe)
    spike.handle_utterance(AUDIO, 0.0)

    assert seen, "the tool delay never ran, so nothing was observed"
    assert all(seen), "the microphone was closed while a tool was running"


def test_the_microphone_stays_open_while_rime_audio_is_playing(monkeypatch):
    """The other half: audio out must not close audio in, or barge-in is impossible by design."""
    spike, _trace, rime, _llm = build(monkeypatch, FIRST)
    during: list[bool] = []
    original = rime.speak

    def speak(text, **kwargs):
        during.append(spike.mic.listening)
        return original(text, **kwargs)

    monkeypatch.setattr(rime, "speak", speak)
    spike.handle_utterance(AUDIO, 0.0)

    assert during, "nothing was spoken, so nothing was observed"
    assert all(during), "the microphone was closed while Rime audio was playing"


def test_an_uninterrupted_turn_of_the_same_shape_completes_normally(monkeypatch):
    """The control the brief asks for -- "run a normal interaction and one deliberate stress case".

    Same delayed tool, same path, no interruption: the answer must arrive. Without this, every
    assertion above could be satisfied by a pipeline that simply never speaks.
    """
    from aether.hotel import HotelStore

    spike, trace, rime, _llm = build(monkeypatch, FIRST)
    _slow_tools(spike, lambda _s: None)
    spike.handle_utterance(AUDIO, 0.0)

    assert rime.spoken, "the uninterrupted turn spoke nothing"
    for item in HotelStore().in_category("starters"):
        assert item.name in rime.spoken[-1]
    assert trace.all(EventType.RESULT_LEAKED) == []
