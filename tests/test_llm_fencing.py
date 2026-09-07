"""The golden invariant applied to a slow LLM.

RULES.md R1: a stale result must never become spoken output. An LLM call takes real time, so the
user can interrupt while the model is still thinking. When that happens the answer is stale on
arrival — the user has moved on — and it must be discarded before it reaches Rime.

The old instant stub could never exercise this: it returned before an interruption was possible.
These tests use a deliberately slow fake LLM so the interruption lands mid-request, deterministically.

Assertions are on actual pipeline behaviour via the canonical trace — not on whether a
cancellation exception was raised. Cancellation is not relied upon anywhere: the fake provider
always returns a result, exactly as a real provider is allowed to.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from aether.audio.rime_ws import SpeakResult
from aether.events import EventType, GenerationStatus
from aether.spike import Day1Spike
from aether.trace import Trace


class SlowLLM:
    """Blocks until released, then answers anyway — like a provider that ignores cancellation."""

    name = "slow-fake"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def respond(self, user_text: str, history=None) -> str:
        self.started.set()
        assert self.release.wait(timeout=5.0), "test did not release the LLM"
        self.completed.set()
        return "STALE ANSWER FROM THE FENCED GENERATION"


class InstantLLM:
    name = "instant-fake"

    def respond(self, user_text: str, history=None) -> str:
        return "fresh answer"


class RecordingRime:
    """Stands in for the Rime client and records whether it was ever asked to speak."""

    name = "rime"
    transport = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._raises = None
        self.config = type("cfg", (), {"model": "mistv2", "voice": "astra"})()
        self.last_latency_ms = 1.0

    def speak(self, text, *, gate, gen, turn_id=None, is_valid=None):
        self.calls.append(text)
        if self._raises is not None:
            raise self._raises
        if is_valid is not None and not is_valid():
            return SpeakResult(accepted=False, completed=False, reason="fenced_midstream")
        pcm = np.zeros(gate.samplerate // 10, dtype=np.int16)
        accepted = gate.enqueue(pcm, turn_id=turn_id, gen=gen)
        return SpeakResult(
            accepted=bool(accepted),
            completed=bool(accepted),
            samples=len(pcm) if accepted else 0,
            reason="" if accepted else "gate_refused",
        )


@pytest.fixture
def spike(monkeypatch):
    """A Day1Spike with audio/STT stubbed out, but the REAL generation + fencing path."""
    trace = Trace()

    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: _FakeGate())
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _FakeMic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: _FakeSTT(trace))
    monkeypatch.setattr("aether.spike.build_llm", lambda: InstantLLM())
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: RecordingRime())

    s = Day1Spike(trace)
    s.rime = RecordingRime()
    return s, trace


class _FakeGate:
    samplerate = 48000

    def __init__(self):
        self.active_gen = None
        self.enqueued: list[str | None] = []
        self.is_ducked = False
        self.is_playing = True

    def set_active_generation(self, gen, *, turn_id=None):
        self.active_gen = gen

    def fence_generation(self, gen, *, reason="fenced"):
        self.active_gen = None

    def enqueue(self, pcm, *, turn_id=None, gen=None):
        if gen is not None and gen != self.active_gen:
            return False
        self.enqueued.append(gen)
        return True

    def request_duck(self, *a, **k): ...
    def request_stop(self, *a, **k): ...
    def request_resume(self, *a, **k): ...


class _FakeMic:
    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None

    def set_context(self, **k): ...


class _FakeSTT:
    def __init__(self, trace):
        self.trace = trace

    def transcribe(self, audio, *, turn_id=None, gen=None):
        self.trace.emit(EventType.TRANSCRIPT_FINAL, turn_id=turn_id, gen=gen, text="hello")
        return "hello"


# --- the required test ----------------------------------------------------------------

def test_llm_answer_for_a_fenced_generation_never_reaches_rime(spike):
    """Fence during an in-flight LLM request; the late answer must be discarded, not spoken."""
    s, trace = spike
    slow = SlowLLM()
    s.llm = slow

    # Generation N starts and its LLM request begins, on a worker so we can interrupt it.
    worker = threading.Thread(target=s.handle_utterance, args=(np.zeros(16000, np.int16), 0.0))
    worker.start()
    assert slow.started.wait(timeout=5.0), "LLM request never started"

    gen_id = s.gens.active.id
    assert gen_id == "G1"

    # USER INTERRUPTS while the model is still thinking.
    trace.emit(EventType.SPEECH_ONSET)
    s.gens.mark_fenced(gen_id)
    s.gate.fence_generation(gen_id)
    trace.emit(EventType.FENCE_REQUESTED, gen=gen_id, reason="meaningful_interruption")

    # The provider answers anyway.
    slow.release.set()
    worker.join(timeout=5.0)
    assert slow.completed.is_set(), "the fake LLM must have returned a result"

    # --- assertions from actual pipeline behaviour ---
    assert s.rime.calls == [], "a fenced generation's answer must never be synthesised"
    assert s.gate.enqueued == [], "nothing from the fenced generation may be queued for playback"

    discarded = [e for e in trace.all(EventType.RESULT_DISCARDED) if e.gen == gen_id]
    assert discarded, "the fenced LLM answer must be recorded as ResultDiscarded"
    assert discarded[0].fields["stage"] == "llm"
    assert discarded[0].fields["reason"] == "stale_generation_llm"

    spoken = [e for e in trace.all(EventType.RESPONSE_SPOKEN) if e.gen == gen_id]
    assert spoken == [], "ResponseSpoken must NOT be emitted for a fenced generation"

    assert s.gens.get(gen_id).status is GenerationStatus.FENCED
    assert trace.all(EventType.RESULT_LEAKED) == [], "no leak in safe mode"


def test_trace_order_shows_fence_before_discard_and_no_speech(spike):
    """The trace alone must tell the story: started -> fenced -> discarded, never spoken."""
    s, trace = spike
    slow = SlowLLM()
    s.llm = slow

    worker = threading.Thread(target=s.handle_utterance, args=(np.zeros(16000, np.int16), 0.0))
    worker.start()
    slow.started.wait(timeout=5.0)
    gen_id = s.gens.active.id

    s.gens.mark_fenced(gen_id)
    trace.emit(EventType.FENCE_REQUESTED, gen=gen_id, reason="meaningful_interruption")
    slow.release.set()
    worker.join(timeout=5.0)

    names = [e.type for e in trace.events]
    assert "TaskStarted" in names
    fence_i = names.index("FenceRequested")
    discard_i = names.index("ResultDiscarded")
    assert fence_i < discard_i, "the discard must follow the fence"
    assert "ResponseSpoken" not in names


# --- 17: the race, without relying on sleeps ------------------------------------------

def test_fence_landing_just_before_the_answer_returns_is_still_safe(spike):
    """Fence applied at the last possible moment: the check still catches it."""
    s, trace = spike

    class RaceLLM:
        name = "race-fake"

        def respond(self, user_text: str, history=None) -> str:
            # Fence lands inside the request, immediately before the return.
            s.gens.mark_fenced(s.gens.active.id)
            return "answer that raced the fence"

    s.llm = RaceLLM()
    s.handle_utterance(np.zeros(16000, np.int16), 0.0)

    assert s.rime.calls == []
    assert [e for e in trace.all(EventType.RESULT_DISCARDED)]
    assert trace.all(EventType.RESPONSE_SPOKEN) == []


def test_generation_that_is_never_fenced_does_reach_rime(spike):
    """The guard must not break the normal path — valid answers must still be spoken."""
    s, trace = spike
    s.llm = InstantLLM()

    s.handle_utterance(np.zeros(16000, np.int16), 0.0)

    assert s.rime.calls == ["fresh answer"], "a valid generation must reach Rime"
    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 1
    assert spoken[0].fields["provider"] == "rime", "TTS provider must remain Rime"
    assert spoken[0].gen == s.gens.active.id
    assert trace.all(EventType.RESULT_DISCARDED) == []


def test_second_generation_proceeds_normally_after_the_first_is_fenced(spike):
    """N is discarded, N+1 answers. Exactly one response is spoken, and it is N+1's."""
    s, trace = spike
    slow = SlowLLM()
    s.llm = slow

    worker = threading.Thread(target=s.handle_utterance, args=(np.zeros(16000, np.int16), 0.0))
    worker.start()
    slow.started.wait(timeout=5.0)
    g1 = s.gens.active.id
    s.gens.mark_fenced(g1)
    slow.release.set()
    worker.join(timeout=5.0)

    # The interruption becomes the next turn.
    s.llm = InstantLLM()
    s.handle_utterance(np.zeros(16000, np.int16), 0.0)
    g2 = s.gens.active.id

    assert g1 != g2
    assert s.rime.calls == ["fresh answer"], "only the new generation may be synthesised"
    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 1 and spoken[0].gen == g2
    assert [e.gen for e in trace.all(EventType.RESULT_DISCARDED)] == [g1]
