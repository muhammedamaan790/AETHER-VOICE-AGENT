"""Pre-registered acceptance tests.

Written on Day 1, BEFORE the demo, per PHASES.md. Specifications and result placeholders live in
RIME_EVIDENCE.md Part 3.

Rules that apply here:
  - Assertions use the canonical event vocabulary from `aether.events` (RULES.md R3).
  - Safe-mode scenarios all assert that no `ResultLeaked` event appears (RULES.md R1.4).
  - No expected number is written in as if it were measured (RULES.md R10).
  - **No real-phone behaviour is asserted anywhere in this file.** Every scenario runs on the local
    pipeline with fake IO. The call path is unvalidated, and a green test here is not evidence
    about a telephone.

Six of the eight scenarios now run. The two that remain skipped are skipped because the feature
genuinely does not exist, and each `skip` says which one -- a test that quietly asserts nothing is
worse than a test that says what is missing.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.events import EventType, InterruptionClass
from aether.hotel import HotelStore, say_price
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)


# ============================ the rig ============================
#
# A real `Day1Spike`: real classifier, real GenerationRegistry, real AudioGate, real ToolRunner,
# real fencing. Only the four IO edges are faked -- microphone, STT, LLM and Rime -- because those
# are hardware and network, not behaviour under test.

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
        return "We are open until eleven."


def build(monkeypatch, *utterances):
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


def only_class(trace) -> str:
    events = trace.all(EventType.INTERRUPTION_CLASSIFIED)
    assert events, "the turn must have been classified"
    return events[-1].fields["interruption_class"]


def no_leak(trace) -> None:
    assert trace.all(EventType.RESULT_LEAKED) == [], "RULES.md R1.4: stale output must never leak"


# --- A: REFINEMENT -----------------------------------------------------------------

def test_refinement_creates_new_generation_and_fences_old(monkeypatch):
    """Active task -> the caller changes a constraint -> new generation -> the old result cannot speak.

    Asserts: class == REFINEMENT; G2 active and G1 fenced; the refinement is recorded as its own
    transition rather than as a plain replacement; no ResultLeaked.

    Salvage is NOT asserted, and `ResultSalvaged` is asserted ABSENT: AETHER holds no partial
    results, so there is nothing to reuse and claiming a reuse count would be inventing evidence
    (RULES.md R8).
    """
    spike, trace, _rime, _llm = build(
        monkeypatch, "what starters do you have", "actually what desserts do you have")

    spike.handle_utterance(AUDIO, 0.0)
    first = spike.gens.active.id
    spike.handle_utterance(AUDIO, 0.0)
    second = spike.gens.active.id

    assert only_class(trace) == InterruptionClass.REFINEMENT.value
    assert second != first
    assert spike.gens.get(first).status.value == "fenced"
    assert spike.gens.get(second).status.value == "active"
    assert [e.fields["reason"] for e in trace.all(EventType.TASK_REPLACED)] == ["refinement"]
    assert trace.all(EventType.RESULT_SALVAGED) == []
    no_leak(trace)


# --- B: REPLACEMENT ----------------------------------------------------------------

def test_replacement_fences_old_generation_and_runs_new_task(monkeypatch):
    """Active task -> the caller replaces it -> the old generation is fenced -> the new task runs.

    Asserts: class == REPLACEMENT; FenceRequested/GenerationChanged carry G1 -> G2; the new task
    completes and is spoken; no ResultLeaked.
    """
    spike, trace, rime, _llm = build(
        monkeypatch, "what starters do you have", "how much is the chicken kebab")

    spike.handle_utterance(AUDIO, 0.0)
    first = spike.gens.active.id
    spike.handle_utterance(AUDIO, 0.0)
    second = spike.gens.active.id

    assert only_class(trace) == InterruptionClass.REPLACEMENT.value
    assert spike.gens.get(first).status.value == "fenced"
    changes = trace.all(EventType.GENERATION_CHANGED)
    assert changes[-1].fields["from_gen"] == first
    assert changes[-1].fields["to_gen"] == second
    kebab = HotelStore().find_item("chicken kebab")
    assert rime.spoken[-1] == f"The {kebab.name} is {say_price(kebab.price)}."
    assert len(trace.all(EventType.RESPONSE_SPOKEN)) == 2
    no_leak(trace)


# --- C: STATUS_QUERY ---------------------------------------------------------------

def test_status_query_answers_without_destroying_task_state(monkeypatch):
    """Active task -> the caller asks about progress -> task state intact.

    Asserts: class == STATUS_QUERY; the active generation is unchanged; no FenceRequested and no
    TaskReplaced; the task in flight is untouched.

    The specification originally said "status spoken from live task state". It is NOT spoken, and
    that is a deliberate narrowing rather than an omission: speaking over an answer already in
    flight would need a second audio path able to bypass the gate's single active generation, and
    putting a hole in that guarantee to say "just a moment" is not a trade worth making. The class
    protects the task; it does not talk over it.
    """
    spike, trace, rime, _llm = build(
        monkeypatch, "what starters do you have", "are you still there")

    spike.handle_utterance(AUDIO, 0.0)
    running = spike.gens.active.id
    spoken_before = len(rime.spoken)
    spike.handle_utterance(AUDIO, 0.0)

    assert only_class(trace) == InterruptionClass.STATUS_QUERY.value
    assert spike.gens.active.id == running, "the active generation is unchanged"
    assert trace.all(EventType.FENCE_REQUESTED) == []
    assert trace.all(EventType.TASK_REPLACED) == []
    assert len(rime.spoken) == spoken_before
    no_leak(trace)


# --- D: CANCEL ---------------------------------------------------------------------

def test_cancel_resolves_and_blocks_obsolete_output(monkeypatch):
    """Active task -> the caller cancels -> cancellation resolves -> obsolete output cannot speak.

    Asserts: class == CANCEL; CancellationResolved emitted; G1 fenced with no successor task; a
    late G1 result is refused by the gate; cancellation is distinguishable from plain fencing by
    its reason in the trace.
    """
    spike, trace, rime, _llm = build(monkeypatch, "what starters do you have", "cancel that")

    spike.handle_utterance(AUDIO, 0.0)
    cancelled = spike.gens.active.id
    spoken_before = len(rime.spoken)
    spike.handle_utterance(AUDIO, 0.0)

    assert only_class(trace) == InterruptionClass.CANCEL.value
    resolved = trace.all(EventType.CANCELLATION_RESOLVED)
    assert len(resolved) == 1 and resolved[0].fields["successor_task"] is None
    assert spike.gens.get(cancelled).status.value == "fenced"
    assert spike.gens.active is None, "a cancellation has no successor generation"
    assert len(rime.spoken) == spoken_before

    # A late result for the cancelled generation cannot reach the speaker.
    assert spike.gate.enqueue(np.ones(160, dtype=np.int16), gen=cancelled) is False

    assert [e.fields["reason"] for e in trace.all(EventType.FENCE_REQUESTED)] == [
        "cancelled_by_caller"
    ], "a cancellation must not look like an ordinary replacement in the evidence"
    no_leak(trace)


# --- E: BACKCHANNEL ----------------------------------------------------------------

def test_backchannel_does_not_destroy_the_task(monkeypatch):
    """"mhm" / "yeah" must not destroy the task.

    Asserts: BackchannelDetected; no AudioStopped; the active generation is unchanged; no
    FenceRequested.

    AudioDucked -> AudioResumed is NOT asserted here, and the reason is a real property of the
    mode rather than a gap: hands-free (the default, and what the phone path runs) never ducks,
    because ducking without a fence behind it silently discards the utterance. The audible result
    is stronger, not weaker -- the answer never dips at all.
    """
    spike, trace, rime, _llm = build(monkeypatch, "what starters do you have", "mm-hm")

    spike.handle_utterance(AUDIO, 0.0)
    running = spike.gens.active.id
    spoken_before = len(rime.spoken)
    spike.handle_utterance(AUDIO, 0.0)

    assert trace.all(EventType.BACKCHANNEL_DETECTED), "the backchannel must be named"
    assert only_class(trace) == InterruptionClass.BACKCHANNEL.value
    assert spike.gens.active.id == running
    assert trace.all(EventType.FENCE_REQUESTED) == []
    assert trace.all(EventType.AUDIO_STOPPED) == []
    assert len(rime.spoken) == spoken_before
    no_leak(trace)


# --- F: NEW_TASK -------------------------------------------------------------------

def test_new_task_is_answered_and_logged_distinctly(monkeypatch):
    """A general question is answered without being mislabelled refinement or replacement.

    Asserts: class == NEW_TASK; no TaskReplaced, because nothing was displaced; the question is
    answered via the LLM knowledge path rather than by the menu router; no ResultLeaked.
    """
    spike, trace, rime, llm = build(monkeypatch, "what time do you close")
    spike.handle_utterance(AUDIO, 0.0)

    assert only_class(trace) == InterruptionClass.NEW_TASK.value
    assert trace.all(EventType.TASK_REPLACED) == [], "nothing was displaced"
    assert llm.calls == ["what time do you close"], "the model handles what the menu cannot"
    assert rime.spoken == ["We are open until eleven."]
    no_leak(trace)


# --- G: FORCED STALE RESULT (controlled, two modes) --------------------------------

@pytest.mark.skip(reason="AETHER_UNSAFE_MODE is parsed by RuntimeConfig and honoured nowhere: "
                         "there is no code path that lets a stale result reach output, so the "
                         "control condition cannot be run. Building one purely to fail is not "
                         "worth putting a bypass through the gate (RULES.md R11).")
def test_unsafe_mode_leaks_stale_result():
    """Control condition. AETHER_UNSAFE_MODE=1 -- the stale result reaches output."""
    raise NotImplementedError


def test_safe_mode_blocks_the_same_stale_result(monkeypatch):
    """A result produced for a fenced generation is blocked at every layer that could speak it.

    Asserts: ResultDiscarded emitted; no ResultLeaked; nothing new was synthesised. Run without
    the unsafe control condition, which does not exist -- see the skip above.
    """
    spike, trace, rime, _llm = build(monkeypatch, "what starters do you have")

    original = spike.tools.run

    def fence_then_run(*args, **kwargs):
        """Fence DURING the lookup: the caller has moved on before the answer exists."""
        result = original(*args, **kwargs)
        spike.barge.fence_now(reason="test_forced_stale")
        return result

    monkeypatch.setattr(spike.tools, "run", fence_then_run)
    spike.handle_utterance(AUDIO, 0.0)

    assert rime.spoken == [], "a fenced lookup must never reach Rime"
    assert trace.all(EventType.RESULT_DISCARDED), "and the discard must be recorded"
    assert spike.history.messages() == [], "nor may it become conversational context"
    no_leak(trace)


# --- H: GENERAL Q&A REFINEMENT (zero-salvage case) ---------------------------------

@pytest.mark.skip(reason="ResultSalvaged is not implemented and is deliberately not faked. AETHER "
                         "holds no partial-result store, so records_reused would be 0 on every "
                         "run -- an event emitted only to look like evidence (RULES.md R8). "
                         "Scenario A already asserts the fence, and asserts the event ABSENT.")
def test_general_qa_refinement_salvages_nothing_and_says_so():
    """A correction that changes query identity: nothing is reusable, and that is reported."""
    raise NotImplementedError
