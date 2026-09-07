"""The deterministic interruption classifier, and what the pipeline does with each verdict.

Two layers, and the second is the one that matters:

* **the classifier** -- pure, no state, no engine. Given a sentence and whether something is
  running, which of the six classes is this?
* **the wired pipeline** -- a real `Day1Spike` with fake IO, driven through `handle_utterance`,
  asserting on the canonical trace events and on the generation registry rather than on prose.

The property under test throughout is not "does it recognise phrases". It is that a
misclassification can only ever cost recall, never correctness: everything the classifier is not
certain about falls through to replacement, which fences, which is what AETHER did before the
classifier existed.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.classify import Classification, classify, normalise
from aether.events import EventType, InterruptionClass, TransitionReason
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)


# ============================ the classifier ============================

def test_normalise_strips_punctuation_and_leading_filler():
    assert normalise("  Uh, mm-hm! ") == "uh mm hm"
    assert normalise("Stop.") == "stop"


@pytest.mark.parametrize("said", ["mm-hm", "yeah", "okay", "right", "uh huh", "go on", "I see"])
def test_backchannels_are_recognised_only_against_something_to_encourage(said):
    assert classify(said, in_flight=True).cls is InterruptionClass.BACKCHANNEL
    assert classify(said, in_flight=False).cls is InterruptionClass.NEW_TASK, (
        '"yes" said into silence answers a question AETHER asked; it must start a turn'
    )


@pytest.mark.parametrize("said", ["stop", "cancel that", "forget it", "never mind", "no thanks"])
def test_cancellations_are_recognised_in_either_state(said):
    assert classify(said, in_flight=True).cls is InterruptionClass.CANCEL
    assert classify(said, in_flight=False).cls is InterruptionClass.CANCEL


@pytest.mark.parametrize("said", [
    "are you still there", "how long will this take", "what are you doing", "did you get that",
])
def test_status_queries_are_recognised_while_a_task_runs(said):
    assert classify(said, in_flight=True).cls is InterruptionClass.STATUS_QUERY


def test_a_status_query_into_silence_is_ordinary_conversation():
    """"Can you hear me?" on a bad line is a question, not a progress request."""
    assert classify("can you hear me", in_flight=False).cls is InterruptionClass.NEW_TASK


@pytest.mark.parametrize("said", [
    "actually make it vegetarian", "sorry I meant the paneer", "no I meant the mains",
    "make it the vegan one", "instead can I have the biryani",
])
def test_explicit_corrections_are_refinements(said):
    assert classify(said, in_flight=True).cls is InterruptionClass.REFINEMENT


def test_anything_else_over_a_running_task_replaces_it():
    """THE SAFE FALLBACK. Uncertain refinement-versus-replacement lands here, and this fences."""
    decision = classify("what desserts do you have", in_flight=True)
    assert decision.cls is InterruptionClass.REPLACEMENT
    assert decision.rule == "default_in_flight"


def test_nothing_running_means_nothing_was_interrupted():
    assert classify("what starters do you have", in_flight=False).cls is InterruptionClass.NEW_TASK


# --- the property that makes this safe to ship ---

@pytest.mark.parametrize("said", [
    "how do I stop by the front desk",
    "is the kitchen still open",
    "cancel my booking for tomorrow please",
    "okay so what mains do you have",
    "yes I would like the butter chicken",
    "right, how much is the kebab",
])
def test_a_real_sentence_is_never_swallowed_by_a_closed_set(said):
    """Whole-utterance matching, not substring matching.

    Every one of these CONTAINS a cancel or backchannel word. Substring matching would let an
    ordinary question destroy a task or lose a turn, which is a worse failure than not recognising
    a phrase at all.
    """
    decision = classify(said, in_flight=True)
    assert decision.cls is not InterruptionClass.CANCEL
    assert decision.cls is not InterruptionClass.BACKCHANNEL


def test_every_menu_question_the_demo_asks_still_reaches_a_turn():
    """The classifier must not compromise the ordinary hotel path."""
    from aether.hotel.router import route

    for said in ("what starters do you have", "how much is the chicken kebab",
                 "do you have vegetarian mains", "is the seafood platter available",
                 "what desserts do you have", "do you have vegan options",
                 "is the chicken kebab spicy", "i have a nut allergy what can i eat"):
        for in_flight in (True, False):
            decision = classify(said, in_flight=in_flight)
            assert not decision.protects_task, f"{said!r} must be answered, not withheld"
        assert route(said) is not None, f"{said!r} must still route to a menu tool"


def test_the_transition_reason_matches_the_class():
    assert classify("actually make it vegan", in_flight=True).transition is TransitionReason.REFINEMENT
    assert classify("what desserts", in_flight=True).transition is TransitionReason.REPLACEMENT
    assert classify("what desserts", in_flight=False).transition is TransitionReason.NEW_TASK
    assert classify("stop", in_flight=True).transition is None


def test_the_classifier_holds_no_state_and_fences_nothing():
    """Structural guard: routing a judgement and applying one must stay separate."""
    import inspect
    import io
    import tokenize

    import aether.classify as mod

    # CODE only. The docstring names the primitives precisely to say it does not use them.
    src = inspect.getsource(mod)
    code = " ".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    for forbidden in ("fence_now", "mark_fenced", "GenerationRegistry", "AudioGate",
                      "begin_turn", "Trace"):
        assert forbidden not in code, f"the classifier must never touch {forbidden}"
    assert isinstance(classify("hello", in_flight=False), Classification)


def test_all_six_classes_are_reachable():
    """No class is declared supported that the function cannot actually produce."""
    produced = {
        classify("mm hm", in_flight=True).cls,
        classify("actually make it vegan", in_flight=True).cls,
        classify("what desserts do you have", in_flight=True).cls,
        classify("are you still there", in_flight=True).cls,
        classify("cancel that", in_flight=True).cls,
        classify("what starters do you have", in_flight=False).cls,
    }
    assert produced == set(InterruptionClass), "all six, or the taxonomy is a claim not a fact"


# ============================ the wired pipeline ============================

class _Mic:
    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None
        self.listening = True
        self.samplerate = 16000

    def set_listening(self, value):
        self.listening = bool(value)

    def set_context(self, **k): ...


class _STT:
    """Returns the next queued utterance, so one rig can drive a whole conversation."""

    def __init__(self, *texts):
        self.texts = list(texts)

    def transcribe(self, audio, *, turn_id=None, gen=None):
        return self.texts.pop(0) if self.texts else ""


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
        return "Certainly."


def build(monkeypatch, *utterances):
    """A real `Day1Spike` with fake IO: real classifier, real registry, real gate, real fencing."""
    from aether.audio.player import AudioGate
    from aether.spike import HANDS_FREE, Day1Spike

    trace = Trace()
    rime, llm = _Rime(), _LLM()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: AudioGate(trace))
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _Mic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: _STT(*utterances))
    monkeypatch.setattr("aether.spike.build_llm", lambda: llm)
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: rime)
    spike = Day1Spike(trace, input_mode=HANDS_FREE)
    spike.rime = rime
    monkeypatch.setattr(spike, "_streaming_enabled", False)
    return spike, trace, rime, llm


def classes(trace) -> list[str]:
    return [e.fields["interruption_class"] for e in trace.all(EventType.INTERRUPTION_CLASSIFIED)]


# --- E: BACKCHANNEL --------------------------------------------------------------------

def test_a_backchannel_does_not_destroy_the_answer(monkeypatch):
    """The defect the classifier exists to fix: "mm-hm" used to kill the answer it encouraged."""
    spike, trace, rime, _llm = build(monkeypatch, "what starters do you have", "mm-hm")

    spike.handle_utterance(AUDIO, 0.0)
    answering = spike.gens.active.id
    assert spike.gate.is_playing, "the answer is still playing out of the gate"

    spike.handle_utterance(AUDIO, 0.0)

    assert trace.all(EventType.BACKCHANNEL_DETECTED), "the backchannel must be named"
    assert spike.gens.active.id == answering, "the SAME generation is still active"
    assert trace.all(EventType.FENCE_REQUESTED) == [], "nothing was fenced"
    assert len(rime.spoken) == 1, "no second answer was synthesised"
    assert classes(trace)[-1] == "BACKCHANNEL"


def test_a_backchannel_with_nothing_playing_is_treated_as_a_turn(monkeypatch):
    """"Yes" into silence answers a question AETHER asked. It must not be swallowed."""
    spike, trace, _rime, llm = build(monkeypatch, "yes")
    spike.handle_utterance(AUDIO, 0.0)
    assert trace.all(EventType.BACKCHANNEL_DETECTED) == []
    assert llm.calls == ["yes"]


# --- C: STATUS_QUERY -------------------------------------------------------------------

def test_a_status_query_leaves_the_task_state_intact(monkeypatch):
    spike, trace, rime, _llm = build(monkeypatch, "what starters do you have", "are you still there")

    spike.handle_utterance(AUDIO, 0.0)
    answering = spike.gens.active.id
    spike.handle_utterance(AUDIO, 0.0)

    assert classes(trace)[-1] == "STATUS_QUERY"
    assert spike.gens.active.id == answering, "the active generation is unchanged"
    assert trace.all(EventType.FENCE_REQUESTED) == []
    assert trace.all(EventType.TASK_REPLACED) == []
    assert len(rime.spoken) == 1


# --- D: CANCEL -------------------------------------------------------------------------

def test_cancel_fences_and_starts_no_successor(monkeypatch):
    spike, trace, rime, _llm = build(monkeypatch, "what starters do you have", "stop")

    spike.handle_utterance(AUDIO, 0.0)
    cancelled = spike.gens.active.id
    spike.handle_utterance(AUDIO, 0.0)

    resolved = trace.all(EventType.CANCELLATION_RESOLVED)
    assert len(resolved) == 1
    assert resolved[0].fields["cancelled"] is True
    assert resolved[0].fields["successor_task"] is None
    assert spike.gens.active is None, "the cancelled generation is not replaced, it is ended"
    assert spike.gens.get(cancelled).status.value == "fenced"
    assert len(rime.spoken) == 1, "cancelling speaks nothing new"
    assert trace.all(EventType.RESULT_LEAKED) == []


def test_cancel_is_distinguishable_from_a_plain_fence(monkeypatch):
    """The evidence must be able to tell "the caller said stop" from "a new question arrived"."""
    spike, trace, _rime, _llm = build(monkeypatch, "what starters do you have", "forget it")
    spike.handle_utterance(AUDIO, 0.0)
    spike.handle_utterance(AUDIO, 0.0)

    reasons = [e.fields["reason"] for e in trace.all(EventType.FENCE_REQUESTED)]
    assert reasons == ["cancelled_by_caller"]


def test_cancel_with_nothing_running_is_an_honest_no_op(monkeypatch):
    spike, trace, rime, llm = build(monkeypatch, "never mind")
    spike.handle_utterance(AUDIO, 0.0)

    resolved = trace.all(EventType.CANCELLATION_RESOLVED)
    assert len(resolved) == 1
    assert resolved[0].fields["cancelled"] is False, "nothing was cancelled, and it says so"
    assert resolved[0].fields["had_task_in_flight"] is False
    assert rime.spoken == [] and llm.calls == []


# --- B / F: REPLACEMENT and NEW_TASK ---------------------------------------------------

def test_a_replacement_fences_the_old_generation_and_answers_the_new_question(monkeypatch):
    spike, trace, rime, _llm = build(
        monkeypatch, "what starters do you have", "what desserts do you have")

    spike.handle_utterance(AUDIO, 0.0)
    first = spike.gens.active.id
    spike.handle_utterance(AUDIO, 0.0)
    second = spike.gens.active.id

    assert second != first
    assert spike.gens.get(first).status.value == "fenced"
    assert classes(trace)[-1] == "REPLACEMENT"
    replaced = trace.all(EventType.TASK_REPLACED)
    assert [e.fields["reason"] for e in replaced] == ["replacement"]
    assert "gulab jamun" in rime.spoken[-1], "the new question really was answered"
    assert trace.all(EventType.RESULT_LEAKED) == []


def test_a_first_question_is_a_new_task_and_replaces_nothing(monkeypatch):
    spike, trace, _rime, _llm = build(monkeypatch, "what starters do you have")
    spike.handle_utterance(AUDIO, 0.0)

    assert classes(trace) == ["NEW_TASK"]
    assert trace.all(EventType.TASK_REPLACED) == [], "nothing was displaced"


def test_a_refinement_is_labelled_as_one_and_still_fences(monkeypatch):
    """Both fence -- AETHER cannot salvage. The label is honest, the behaviour is conservative."""
    spike, trace, _rime, _llm = build(
        monkeypatch, "what starters do you have", "actually what desserts do you have")

    spike.handle_utterance(AUDIO, 0.0)
    first = spike.gens.active.id
    spike.handle_utterance(AUDIO, 0.0)

    assert classes(trace)[-1] == "REFINEMENT"
    assert [e.fields["reason"] for e in trace.all(EventType.TASK_REPLACED)] == ["refinement"]
    assert spike.gens.get(first).status.value == "fenced"
    assert trace.all(EventType.RESULT_SALVAGED) == [], (
        "salvage is NOT implemented and must not be claimed"
    )
    assert trace.all(EventType.TASK_REPLACED)[-1].fields["records_salvaged"] == 0


# --- the empty-transcript defect the restructure fixed ---------------------------------

def test_a_noise_burst_that_transcribes_to_nothing_no_longer_kills_the_answer(monkeypatch):
    """Transcribing before allocating is what fixed this: allocation is what fences."""
    spike, trace, rime, _llm = build(monkeypatch, "what starters do you have", "")

    spike.handle_utterance(AUDIO, 0.0)
    answering = spike.gens.active.id
    spike.handle_utterance(AUDIO, 0.0)

    assert spike.gens.active.id == answering
    assert trace.all(EventType.FENCE_REQUESTED) == []
    assert len(rime.spoken) == 1
