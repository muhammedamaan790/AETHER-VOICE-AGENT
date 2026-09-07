"""Session conversation history (Level 1) and its interaction with generation fencing.

Two invariants, both tested:

  NEGATIVE  a fenced generation contributes nothing to history, so a later turn can never see a
            stale answer the user never heard.
  POSITIVE  a completed/spoken turn becomes usable context, so contextual follow-ups work.

The negative case alone would be satisfied by never recording anything, so the positive case is
tested just as hard.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from aether.conversation import ConversationHistory
from aether.audio.rime_ws import SpeakResult
from aether.events import EventType
from aether.llm import _prior_turns
from aether.spike import Day1Spike
from aether.trace import Trace


# --- fakes ----------------------------------------------------------------------------

class RecordingLLM:
    """Captures the history it was handed, and answers from a scripted list."""

    name = "recording-fake"

    def __init__(self, answers: list[str]):
        self._answers = list(answers)
        self.seen_history: list[list[dict]] = []

    def respond(self, user_text, history=None):
        self.seen_history.append([dict(m) for m in (history or [])])
        return self._answers.pop(0) if self._answers else "no more answers"


class SlowLLM:
    """Blocks until released, then answers anyway -- a provider that ignores cancellation."""

    name = "slow-fake"

    def __init__(self, answer: str = "STALE ANSWER"):
        self.answer = answer
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen_history: list[list[dict]] = []

    def respond(self, user_text, history=None):
        self.seen_history.append([dict(m) for m in (history or [])])
        self.started.set()
        assert self.release.wait(timeout=5.0), "test did not release the LLM"
        return self.answer


class RecordingRime:
    name = "rime"
    transport = "fake"

    def __init__(self):
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


class _FakeGate:
    samplerate = 48000

    def __init__(self):
        self.active_gen = None
        self.is_ducked = False
        self.is_playing = True
        self.enqueued: list[str | None] = []
        self.refuse = False

    def set_active_generation(self, gen, *, turn_id=None):
        self.active_gen = gen

    def fence_generation(self, gen, *, reason="fenced"):
        self.active_gen = None

    def enqueue(self, pcm, *, turn_id=None, gen=None):
        if self.refuse or (gen is not None and gen != self.active_gen):
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
    """Returns scripted transcripts, one per turn."""

    def __init__(self, trace, texts):
        self.trace = trace
        self._texts = list(texts)

    def transcribe(self, audio, *, turn_id=None, gen=None):
        text = self._texts.pop(0) if self._texts else ""
        self.trace.emit(EventType.TRANSCRIPT_FINAL, turn_id=turn_id, gen=gen, text=text)
        return text


def make_session(monkeypatch, transcripts, llm):
    trace = Trace()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: _FakeGate())
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _FakeMic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: _FakeSTT(trace, transcripts))
    monkeypatch.setattr("aether.spike.build_llm", lambda: llm)
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: RecordingRime())
    s = Day1Spike(trace)
    s.rime = RecordingRime()
    return s, trace


AUDIO = np.zeros(16000, np.int16)


# --- A: normal completed conversation, and the follow-up actually works ---------------

def test_completed_turn_is_committed_and_next_turn_receives_it(monkeypatch):
    llm = RecordingLLM([
        "The three priority orders are 1042, 1047, and 1051.",
        "Order 1042 is the first priority order.",
    ])
    s, trace = make_session(
        monkeypatch,
        ["What are the three priority orders?", "Just tell me the first one."],
        llm,
    )

    assert s.history.messages() == [], "a session starts empty"

    s.handle_utterance(AUDIO, 0.0)   # turn 1
    assert llm.seen_history[0] == [], "the first turn has no prior context"
    assert s.history.messages() == [
        {"role": "user", "content": "What are the three priority orders?"},
        {"role": "assistant", "content": "The three priority orders are 1042, 1047, and 1051."},
    ]

    s.handle_utterance(AUDIO, 0.0)   # turn 2 -- the contextual follow-up
    assert llm.seen_history[1] == [
        {"role": "user", "content": "What are the three priority orders?"},
        {"role": "assistant", "content": "The three priority orders are 1042, 1047, and 1051."},
    ], "turn 2 must receive turn 1's completed exchange as context"

    assert s.history.turn_count() == 2
    assert s.rime.calls == [
        "The three priority orders are 1042, 1047, and 1051.",
        "Order 1042 is the first priority order.",
    ]


def test_context_grows_across_three_turns(monkeypatch):
    llm = RecordingLLM(["a1", "a2", "a3"])
    s, _ = make_session(monkeypatch, ["q1", "q2", "q3"], llm)
    for _ in range(3):
        s.handle_utterance(AUDIO, 0.0)

    assert [len(h) for h in llm.seen_history] == [0, 2, 4]
    assert llm.seen_history[2][-1] == {"role": "assistant", "content": "a2"}


# --- B: fenced DURING the LLM call ----------------------------------------------------

def test_generation_fenced_during_llm_contributes_nothing_to_history(monkeypatch):
    slow = SlowLLM("The three priority orders are 1042, 1047, and 1051.")
    s, trace = make_session(monkeypatch, ["What are the three priority orders?", "Anything?"], slow)

    worker = threading.Thread(target=s.handle_utterance, args=(AUDIO, 0.0))
    worker.start()
    assert slow.started.wait(timeout=5.0)

    g1 = s.gens.active.id
    s.gens.mark_fenced(g1)           # user interrupts, existing fencing does its job
    slow.release.set()               # provider answers anyway
    worker.join(timeout=5.0)

    assert s.history.messages() == [], "a fenced generation contributes NOTHING -- not even the user text"
    assert s.rime.calls == []
    assert [e.gen for e in trace.all(EventType.RESULT_DISCARDED)] == [g1]
    assert trace.all(EventType.RESPONSE_SPOKEN) == []


def test_next_generation_cannot_see_the_fenced_answer(monkeypatch):
    slow = SlowLLM("STALE: 1042, 1047, and 1051.")
    s, _ = make_session(monkeypatch, ["What are the priority orders?", "Just the first one."], slow)

    worker = threading.Thread(target=s.handle_utterance, args=(AUDIO, 0.0))
    worker.start()
    slow.started.wait(timeout=5.0)
    s.gens.mark_fenced(s.gens.active.id)
    slow.release.set()
    worker.join(timeout=5.0)

    fresh = RecordingLLM(["The first priority order is 1042."])
    s.llm = fresh
    s.handle_utterance(AUDIO, 0.0)

    assert fresh.seen_history[0] == [], "G2 must not see G1's stale answer"
    flat = str(fresh.seen_history[0])
    assert "STALE" not in flat


# --- C: fenced AFTER the LLM returns, before the completed/spoken boundary --------------

def test_fenced_after_llm_before_tts_is_not_committed(monkeypatch):
    """LLM succeeded, but the turn never became spoken -- so it is not part of the conversation."""
    llm = RecordingLLM(["An answer that was never heard."])
    s, trace = make_session(monkeypatch, ["a question", "next"], llm)

    real_speak = s.rime.speak

    def fence_then_speak(text, **kw):
        s.gens.mark_fenced(s.gens.active.id)   # interruption lands during synthesis
        return real_speak(text, **kw)

    s.rime.speak = fence_then_speak
    s.handle_utterance(AUDIO, 0.0)

    assert s.history.messages() == []
    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert discarded and discarded[0].fields["stage"] == "tts"
    assert trace.all(EventType.RESPONSE_SPOKEN) == []


def test_not_committed_when_the_output_gate_refuses_the_audio(monkeypatch):
    """The gate is the completed/spoken boundary. If it refuses, nothing was heard."""
    llm = RecordingLLM(["An answer the gate rejected."])
    s, trace = make_session(monkeypatch, ["a question"], llm)
    s.gate.refuse = True

    s.handle_utterance(AUDIO, 0.0)

    assert s.history.messages() == [], "history must follow the gate, not the LLM"
    assert trace.all(EventType.RESPONSE_SPOKEN) == []


def test_llm_returning_successfully_is_not_by_itself_a_completed_turn(monkeypatch):
    """Guards the rule directly: 'the LLM returned' != 'the assistant turn completed'."""
    llm = RecordingLLM(["answer"])
    s, _ = make_session(monkeypatch, ["q"], llm)
    s.gate.refuse = True

    s.handle_utterance(AUDIO, 0.0)
    assert len(llm.seen_history) == 1, "the LLM was called"
    assert s.history.messages() == [], "yet nothing was committed"


# --- D: session isolation --------------------------------------------------------------

def test_history_does_not_leak_between_sessions(monkeypatch):
    llm_a = RecordingLLM(["answer A"])
    session_a, _ = make_session(monkeypatch, ["question A"], llm_a)
    session_a.handle_utterance(AUDIO, 0.0)
    assert session_a.history.turn_count() == 1

    llm_b = RecordingLLM(["answer B"])
    session_b, _ = make_session(monkeypatch, ["question B"], llm_b)

    assert session_b.history.messages() == [], "session B starts empty"
    session_b.handle_utterance(AUDIO, 0.0)
    assert llm_b.seen_history[0] == [], "session B must not receive session A's context"
    assert session_a.history.turn_count() == 1, "session A is unaffected"


def test_two_histories_are_independent_objects():
    a, b = ConversationHistory(), ConversationHistory()
    a.commit_turn("q", "a")
    assert len(a) == 2 and len(b) == 0


# --- fencing must never modify or delete history ---------------------------------------

def test_fencing_does_not_touch_already_committed_history(monkeypatch):
    """Fencing invalidates output; it does not rewrite the past (RULES.md R5).

    A completed turn stays in history even when the NEXT generation is fenced.
    """
    llm = RecordingLLM(["The three priority orders are 1042, 1047, and 1051."])
    s, _ = make_session(monkeypatch, ["What are the three priority orders?", "second question"], llm)

    s.handle_utterance(AUDIO, 0.0)          # turn 1 completes and is spoken
    committed = s.history.messages()
    assert len(committed) == 2

    # Turn 2 starts and is fenced mid-LLM.
    slow = SlowLLM("never heard")
    s.llm = slow
    worker = threading.Thread(target=s.handle_utterance, args=(AUDIO, 0.0))
    worker.start()
    slow.started.wait(timeout=5.0)
    g2 = s.gens.active.id
    s.gens.mark_fenced(g2)
    s.gate.fence_generation(g2)
    slow.release.set()
    worker.join(timeout=5.0)

    assert s.history.messages() == committed, "fencing must neither delete nor modify history"
    # ...and the fenced turn saw the earlier completed turn as context, which is correct:
    # it was fenced, not un-remembered.
    assert slow.seen_history[0] == committed


def test_history_exposes_no_way_to_delete_a_committed_turn():
    """Append-only: there is no clear()/pop()/remove() surface for fencing to reach for."""
    h = ConversationHistory()
    for forbidden in ("clear", "pop", "remove", "delete", "truncate", "rewrite"):
        assert not hasattr(h, forbidden), f"ConversationHistory must not expose {forbidden}()"


def test_fence_generation_never_references_history():
    """Structural guard: the fencing machinery must not know history exists."""
    import inspect

    from aether.audio import player
    from aether.supervisor import generations

    for module in (player, generations):
        src = inspect.getsource(module)
        assert "history" not in src.lower(), (
            f"{module.__name__} must not reference conversation history -- "
            "fencing operates on audio and results only"
        )


# --- E: sanitization -- only role/content reaches the model ---------------------------

def test_history_contains_only_role_and_content(monkeypatch):
    llm = RecordingLLM(["a1", "a2"])
    s, _ = make_session(monkeypatch, ["q1", "q2"], llm)
    s.handle_utterance(AUDIO, 0.0)
    s.handle_utterance(AUDIO, 0.0)

    for message in llm.seen_history[1]:
        assert set(message.keys()) == {"role", "content"}, f"unexpected keys: {message.keys()}"
        assert message["role"] in ("user", "assistant")
        assert isinstance(message["content"], str)


def test_no_internal_metadata_appears_in_model_facing_history(monkeypatch):
    llm = RecordingLLM(["a1", "a2"])
    s, _ = make_session(monkeypatch, ["q1", "q2"], llm)
    s.handle_utterance(AUDIO, 0.0)
    s.handle_utterance(AUDIO, 0.0)

    flat = str(llm.seen_history[1]).lower()
    for forbidden in ("gen=", "g1", "turn_id", "timestamp", "seq", "trace",
                      "fenced", "latency", "provider", "rime", "task"):
        assert forbidden not in flat, f"{forbidden!r} leaked into model-facing history"


def test_prior_turns_drops_anything_that_is_not_a_clean_pair():
    dirty = [
        {"role": "user", "content": "keep me", "gen": "G1", "t": 123.4},
        {"role": "system", "content": "drop me"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "keep me too"},
    ]
    assert _prior_turns(dirty) == [
        {"role": "user", "content": "keep me"},
        {"role": "assistant", "content": "keep me too"},
    ]


def test_prior_turns_handles_empty_and_none():
    assert _prior_turns(None) == []
    assert _prior_turns([]) == []


# --- history object behaviour ----------------------------------------------------------

def test_context_for_appends_the_current_message_without_storing_it():
    h = ConversationHistory()
    ctx = h.context_for("live question")
    assert ctx == [{"role": "user", "content": "live question"}]
    assert h.messages() == [], "the in-flight message must not be stored"


def test_messages_returns_a_copy():
    h = ConversationHistory()
    h.commit_turn("q", "a")
    got = h.messages()
    got[0]["content"] = "mutated"
    assert h.messages()[0]["content"] == "q"


def test_history_is_bounded():
    h = ConversationHistory(max_turns=2)
    for i in range(5):
        h.commit_turn(f"q{i}", f"a{i}")
    assert h.turn_count() == 2
    assert h.messages()[0] == {"role": "user", "content": "q3"}


def test_max_turns_must_be_positive():
    with pytest.raises(ValueError):
        ConversationHistory(max_turns=0)
