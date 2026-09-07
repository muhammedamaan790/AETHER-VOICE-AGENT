"""An empty or failed LLM response must never reach Rime, and must never kill the voice loop.

Regression for a live failure: Gemini returned an empty string, that empty string was passed
straight to Rime, Rime answered 400 Bad Request, and the exception unwound past the run loop --
which only catches KeyboardInterrupt -- ending the session.

A thinking model can legitimately produce no text at all: if its whole output budget goes on
internal reasoning it finishes with no text parts, and the adapters normalise that to "".

The correct behaviour is a safe, silent failure: no synthesis, no history, no invented answer, and
the session survives to take the next turn.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.rime_ws import SpeakResult
from aether.events import EventType
from aether.spike import Day1Spike
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)


class ScriptedLLM:
    """Returns scripted replies; a reply may be empty. Records that it was called."""

    name = "scripted-fake"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def respond(self, user_text, history=None):
        self.calls += 1
        return self._replies.pop(0) if self._replies else ""


class ExplodingLLM:
    name = "exploding-fake"

    def __init__(self, exc):
        self._exc = exc

    def respond(self, user_text, history=None):
        raise self._exc


class RecordingRime:
    name = "rime"
    transport = "fake"

    def __init__(self, raises=None):
        self.calls: list[str] = []
        self._raises = raises
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


class _FakeGate:
    samplerate = 48000

    def __init__(self):
        self.active_gen = None
        self.is_ducked = False
        self.is_playing = True
        self.enqueued: list[str | None] = []

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
    def __init__(self, trace, texts):
        self.trace = trace
        self._texts = list(texts)

    def transcribe(self, audio, *, turn_id=None, gen=None):
        text = self._texts.pop(0) if self._texts else "hello"
        self.trace.emit(EventType.TRANSCRIPT_FINAL, turn_id=turn_id, gen=gen, text=text)
        return text


def make_session(monkeypatch, transcripts, llm, rime=None):
    trace = Trace()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: _FakeGate())
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _FakeMic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: _FakeSTT(trace, transcripts))
    monkeypatch.setattr("aether.spike.build_llm", lambda: llm)
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: RecordingRime())
    s = Day1Spike(trace)
    s.rime = rime or RecordingRime()
    return s, trace


# --- the reported failure --------------------------------------------------------------

@pytest.mark.parametrize("empty", ["", "   ", "\n", "\t  \n"])
def test_empty_llm_response_is_never_sent_to_rime(monkeypatch, empty):
    """The exact live failure: an empty reply must not reach the TTS call at all."""
    llm = ScriptedLLM([empty])
    s, trace = make_session(monkeypatch, ["What are the pre-priority orders?"], llm)

    s.handle_utterance(AUDIO, 0.0)

    assert llm.calls == 1, "the LLM was called"
    assert s.rime.calls == [], "an empty reply must never be handed to Rime"
    assert s.gate.enqueued == [], "nothing may be queued for playback"
    assert trace.all(EventType.RESPONSE_SPOKEN) == [], "nothing was spoken"


def test_empty_llm_response_is_recorded_as_discarded(monkeypatch):
    llm = ScriptedLLM([""])
    s, trace = make_session(monkeypatch, ["a question"], llm)

    s.handle_utterance(AUDIO, 0.0)

    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert len(discarded) == 1
    assert discarded[0].fields["reason"] == "empty_llm_response"
    assert discarded[0].fields["stage"] == "llm"
    assert discarded[0].fields["provider"] == "scripted-fake"


def test_empty_llm_response_does_not_enter_history(monkeypatch):
    """Level-1 semantics hold: an unspoken turn is not a completed turn."""
    llm = ScriptedLLM([""])
    s, _ = make_session(monkeypatch, ["a question"], llm)

    s.handle_utterance(AUDIO, 0.0)

    assert s.history.messages() == [], "an empty, unspoken turn must not be remembered"


def test_no_answer_is_invented_for_an_empty_response(monkeypatch):
    """Requirement: do not fabricate an assistant response. Silence, not substitution."""
    llm = ScriptedLLM([""])
    s, trace = make_session(monkeypatch, ["a question"], llm)

    s.handle_utterance(AUDIO, 0.0)

    assert s.rime.calls == []
    assert s.history.messages() == []
    spoken_texts = [e.fields.get("text") for e in trace.all(EventType.RESPONSE_SPOKEN)]
    assert spoken_texts == []


def test_session_survives_an_empty_response_and_the_next_turn_works(monkeypatch):
    """The voice loop must stay alive: turn 2 still completes normally."""
    llm = ScriptedLLM(["", "Paris is the capital of France."])
    s, trace = make_session(monkeypatch, ["bad turn", "What is the capital of France?"], llm)

    s.handle_utterance(AUDIO, 0.0)   # empty -> skipped
    s.handle_utterance(AUDIO, 0.0)   # normal -> spoken

    assert s.rime.calls == ["Paris is the capital of France."]
    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 1
    assert spoken[0].fields["provider"] == "rime"
    assert s.history.messages() == [
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "Paris is the capital of France."},
    ], "only the completed turn is remembered"


# --- provider failures must not kill the loop either ------------------------------------

def test_llm_provider_error_does_not_kill_the_session(monkeypatch):
    """A 503 from the provider (observed live) must be survivable, not fatal."""
    llm = ExplodingLLM(RuntimeError("503 UNAVAILABLE"))
    s, trace = make_session(monkeypatch, ["a question"], llm)

    s.handle_utterance(AUDIO, 0.0)   # must not raise

    assert s.rime.calls == []
    assert s.history.messages() == []
    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert discarded and discarded[0].fields["reason"] == "llm_error"
    assert discarded[0].fields["error"] == "RuntimeError"


def test_tts_failure_does_not_kill_the_session(monkeypatch):
    """The original crash was a Rime 400 unwinding past the run loop."""
    llm = ScriptedLLM(["something sayable"])
    rime = RecordingRime(raises=RuntimeError("400 Bad Request"))
    s, trace = make_session(monkeypatch, ["a question"], llm, rime=rime)

    s.handle_utterance(AUDIO, 0.0)   # must not raise

    assert rime.calls == ["something sayable"], "Rime was attempted"
    assert s.history.messages() == [], "a turn that failed to speak is not remembered"
    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert discarded and discarded[0].fields["reason"] == "tts_error"
    assert trace.all(EventType.RESPONSE_SPOKEN) == []


def test_fencing_still_takes_priority_over_the_empty_check(monkeypatch):
    """A fenced generation is discarded as stale, not as empty -- fencing stays authoritative."""
    llm = ScriptedLLM([""])
    s, trace = make_session(monkeypatch, ["a question"], llm)

    original = llm.respond

    def fence_then_respond(user_text, history=None):
        result = original(user_text, history)
        s.gens.mark_fenced(s.gens.active.id)
        return result

    llm.respond = fence_then_respond
    s.handle_utterance(AUDIO, 0.0)

    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert len(discarded) == 1
    assert discarded[0].fields["reason"] == "stale_generation_llm", (
        "the fence check must run before the empty check"
    )
