"""Interruption and recovery.

The safety property under test, stated once:

    For every generation G, once G is fenced, no audio belonging to G may subsequently
    become audible.

"Audible" is decided by the real `AudioGate`, so the gate is used unmodified here rather than
faked -- a fake gate would only prove that the fake refuses things. Only the microphone, STT, LLM
and the Rime socket are stubbed, because those are I/O; the generation registry, the fencing and
the gate are the real implementations.

Interruption is driven by calling the VAD's realtime callbacks (`on_onset`, `on_voiced_progress`)
exactly as the input thread does. No sleeps and no thresholds are used to create the race: each
test places the fence at an exact point by construction.
"""

# Transcripts here are placeholders -- this file tests the streaming/LLM path, not routing.
# They were single tokens ("q", "a question") until 2026-09-10, when AETHER learned to
# answer a one-word unrecognisable fragment with "could you say that again?" instead of
# handing it to the model. That is the intended behaviour, so the placeholders became
# sentences a caller could actually say. Every assertion below is unchanged.

from __future__ import annotations

import threading

import numpy as np
import pytest

from aether.audio.player import AudioGate
from aether.audio.rime_ws import SpeakResult
from aether.events import EventType, GenerationStatus
from aether.spike import MEANINGFUL_SPEECH_MS, Day1Spike
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)


# --- stubs: I/O only ---------------------------------------------------------------------

class ScriptedSTT:
    def __init__(self, trace, texts):
        self.trace = trace
        self._texts = list(texts)

    def transcribe(self, audio, *, turn_id=None, gen=None):
        text = self._texts.pop(0) if self._texts else "what else can you tell me"
        self.trace.emit(EventType.TRANSCRIPT_FINAL, turn_id=turn_id, gen=gen, text=text)
        return text


class ScriptedLLM:
    name = "fake-llm"

    def __init__(self, answers):
        self._answers = list(answers)
        self.calls = 0

    def respond(self, user_text, history=None):
        self.calls += 1
        return self._answers.pop(0) if self._answers else "another answer"


class BlockingLLM:
    """Blocks inside respond() so a fence can land while the model is 'thinking'."""

    name = "blocking-llm"

    def __init__(self, answer="STALE ANSWER"):
        self.answer = answer
        self.started = threading.Event()
        self.release = threading.Event()

    def respond(self, user_text, history=None):
        self.started.set()
        assert self.release.wait(timeout=5.0), "test never released the LLM"
        return self.answer


class StreamingRime:
    """Feeds PCM into the real gate chunk by chunk, honouring is_valid() like the WS client."""

    name = "rime"
    transport = "fake-ws"

    def __init__(self, chunks=4, samples=480):
        self.calls: list[str] = []
        self.chunks = chunks
        self.samples = samples
        self.cleared = False
        self.config = type("cfg", (), {"model": "mistv2", "voice": "astra"})()
        self.last_latency_ms = 1.0
        self.on_chunk = None          # hook to inject a fence mid-stream

    def speak(self, text, *, gate, gen, turn_id=None, is_valid=None):
        self.calls.append(text)
        still_valid = is_valid or (lambda: True)
        streamed = 0
        for i in range(self.chunks):
            if self.on_chunk is not None:
                self.on_chunk(i)
            if not still_valid():
                self.cleared = True          # the real client sends {"operation":"clear"}
                return SpeakResult(accepted=streamed > 0, completed=False,
                                   samples=streamed, first_audio_ms=1.0,
                                   reason="fenced_midstream")
            pcm = np.full(self.samples, gen_marker(gen), dtype=np.int16)
            if not gate.enqueue(pcm, turn_id=turn_id, gen=gen):
                self.cleared = True
                return SpeakResult(accepted=streamed > 0, completed=False,
                                   samples=streamed, reason="gate_refused")
            streamed += len(pcm)
        return SpeakResult(accepted=True, completed=True, samples=streamed, first_audio_ms=1.0)


class SilentMic:
    """Stands in for MicVAD; the tests call the callbacks the input thread would call."""

    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None
        self.device = None

    def set_context(self, **kwargs): ...


def make_session(monkeypatch, transcripts, llm, rime=None, gate=None):
    """Real GenerationRegistry, real AudioGate, real fencing. Stubbed I/O only."""
    trace = Trace()
    the_gate = gate if gate is not None else AudioGate(trace)   # real gate, stream never started
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: the_gate)
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: SilentMic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: ScriptedSTT(trace, transcripts))
    monkeypatch.setattr("aether.spike.build_llm", lambda: llm)
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: rime or StreamingRime())
    s = Day1Spike(trace)
    s.rime = rime or StreamingRime()
    return s, trace


def pump(gate: AudioGate, blocks: int = 3) -> np.ndarray:
    """Run the real output callback, as the PortAudio thread does continuously.

    Not a sleep and not a delay: the callback is where duck/stop are applied and where chunks are
    accepted or refused. Without running it, a requested stop stays pending forever and would
    later flush audio that production had already dealt with milliseconds earlier.
    """
    out = []
    for _ in range(blocks):
        buf = np.zeros((gate.blocksize, 1), dtype=np.int16)
        gate._callback(buf, gate.blocksize, None, None)
        out.append(buf[:, 0].copy())
    return np.concatenate(out)


def interrupt(session):
    """Exactly what the input thread does when the user starts talking and keeps talking.

    The audio thread is then given a moment to actually apply the stop, because that is what
    happens in production -- the callback is running the whole time.
    """
    session.mic.on_onset(0.0)
    session.mic.on_voiced_progress(MEANINGFUL_SPEECH_MS)
    pump(session.gate)


def gen_marker(gen: str | None) -> int:
    """A distinct PCM value per generation, so emitted audio can be attributed to its origin."""
    return 1000 * int(str(gen)[1:]) if gen and str(gen)[1:].isdigit() else 1


def emitted_by_generation(gate: AudioGate, blocks: int = 40) -> dict[str, int]:
    """Drain the gate ONCE and attribute every emitted sample to its generation.

    Draining is destructive, so a test that cares about two generations must use this rather than
    calling `audible_samples` twice -- the first call would consume the second generation's audio.
    """
    audio = pump(gate, blocks)
    counts: dict[str, int] = {}
    for value in np.unique(audio):
        if value != 0:
            counts[f"G{int(value) // 1000}"] = int(np.count_nonzero(audio == value))
    return counts


def audible_samples(gate: AudioGate, gen: str, blocks: int = 40) -> int:
    """Samples of `gen` the gate would actually PUT ON THE SPEAKER.

    This drives the real PortAudio callback rather than inspecting the queue. That distinction
    matters: `fence_generation` revokes the active generation synchronously but flushes inside the
    callback, so a queue snapshot can still show fenced chunks that the callback would never emit.
    What reaches the speaker is the only thing that counts as audible.
    """
    marker = gen_marker(gen)
    return int(np.count_nonzero(pump(gate, blocks) == marker))


# --- 1. interrupt while the AI is speaking -------------------------------------------------

def test_interrupt_while_speaking_fences_and_stops_the_old_response(monkeypatch):
    rime = StreamingRime(chunks=8)
    s, trace = make_session(monkeypatch, ["first question", "second question"],
                            ScriptedLLM(["a long first answer"]), rime=rime)

    # Interrupt after the second chunk has been handed to the gate.
    rime.on_chunk = lambda i: interrupt(s) if i == 2 else None
    s.handle_utterance(AUDIO, 0.0)

    g1 = "G1"
    assert s.gens.get(g1).status is GenerationStatus.FENCED
    assert rime.cleared, "Rime streaming must be told to stop"
    assert audible_samples(s.gate, g1) == 0, "no G1 audio may be emitted after the fence"
    assert trace.all(EventType.RESPONSE_SPOKEN) == [], "a cut-off answer was never fully spoken"
    assert [e.gen for e in trace.all(EventType.RESULT_DISCARDED)] == [g1]


def test_the_old_response_never_resumes(monkeypatch):
    """After the fence, late chunks for the old generation must still be refused."""
    rime = StreamingRime(chunks=4)
    s, _ = make_session(monkeypatch, ["what is your star rating"], ScriptedLLM(["answer one"]), rime=rime)
    rime.on_chunk = lambda i: interrupt(s) if i == 1 else None
    s.handle_utterance(AUDIO, 0.0)

    late = np.full(480, gen_marker("G1"), dtype=np.int16)
    assert s.gate.enqueue(late, gen="G1") is False, "a fenced generation may never be re-queued"
    assert audible_samples(s.gate, "G1") == 0


# --- 2. interrupt while the LLM is thinking -------------------------------------------------

def test_interrupt_while_llm_is_thinking_fences_the_generation(monkeypatch):
    """The regression that motivated this work: nothing is playing yet, so the barge-in used to
    be ignored entirely and the abandoned answer was still spoken."""
    llm = BlockingLLM("STALE ANSWER")
    rime = StreamingRime()
    s, trace = make_session(monkeypatch, ["first question"], llm, rime=rime)

    worker = threading.Thread(target=s.handle_utterance, args=(AUDIO, 0.0))
    worker.start()
    assert llm.started.wait(timeout=5.0)
    assert s.gate.is_playing is False, "precondition: no audio yet, the model is still thinking"

    interrupt(s)                       # user speaks while the model thinks
    assert s.gens.get("G1").status is GenerationStatus.FENCED, "the fence must not need audio"

    llm.release.set()
    worker.join(timeout=5.0)

    assert rime.calls == [], "a stale answer must never reach Rime"
    assert trace.all(EventType.RESPONSE_SPOKEN) == []
    discarded = [e for e in trace.all(EventType.RESULT_DISCARDED) if e.gen == "G1"]
    assert discarded and discarded[0].fields["stage"] == "llm"


def test_stale_llm_answer_never_reaches_the_gate(monkeypatch):
    llm = BlockingLLM("STALE ANSWER")
    s, _ = make_session(monkeypatch, ["what is your star rating"], llm)

    worker = threading.Thread(target=s.handle_utterance, args=(AUDIO, 0.0))
    worker.start()
    llm.started.wait(timeout=5.0)
    interrupt(s)
    llm.release.set()
    worker.join(timeout=5.0)

    assert audible_samples(s.gate, "G1") == 0
    assert len(s.gate._queue) == 0


# --- 6. race conditions, placed exactly rather than by timing --------------------------------

def test_fence_immediately_before_the_llm_returns(monkeypatch):
    class FenceAtReturnLLM:
        name = "race-llm"

        def respond(self, user_text, history=None):
            interrupt(s)               # fence lands on the last instruction before returning
            return "answer that raced the fence"

    s, trace = make_session(monkeypatch, ["what is your star rating"], FenceAtReturnLLM())
    s.handle_utterance(AUDIO, 0.0)

    assert s.rime.calls == []
    assert trace.all(EventType.RESPONSE_SPOKEN) == []
    assert [e.fields["stage"] for e in trace.all(EventType.RESULT_DISCARDED)] == ["llm"]


def test_fence_after_the_llm_returns_but_before_tts_starts(monkeypatch):
    rime = StreamingRime()
    s, trace = make_session(monkeypatch, ["what is your star rating"], ScriptedLLM(["an answer"]), rime=rime)
    rime.on_chunk = lambda i: interrupt(s) if i == 0 else None   # before the first chunk

    s.handle_utterance(AUDIO, 0.0)

    assert audible_samples(s.gate, "G1") == 0, "not one sample may reach the speaker"
    assert trace.all(EventType.RESPONSE_SPOKEN) == []
    assert rime.cleared


def test_fence_during_rime_streaming_stops_mid_utterance(monkeypatch):
    rime = StreamingRime(chunks=10)
    s, trace = make_session(monkeypatch, ["what is your star rating"], ScriptedLLM(["a long answer"]), rime=rime)
    rime.on_chunk = lambda i: interrupt(s) if i == 3 else None

    s.handle_utterance(AUDIO, 0.0)

    assert rime.cleared, "streaming must stop, not drain all ten chunks"
    assert audible_samples(s.gate, "G1") == 0, "and what was already queued must be flushed"
    assert trace.all(EventType.RESPONSE_SPOKEN) == []


def test_stale_rime_chunks_arriving_after_a_fence_are_refused(monkeypatch):
    """The gate is the final authority even if the client keeps pushing."""
    s, _ = make_session(monkeypatch, ["what is your star rating"], ScriptedLLM(["a"]))
    s.gate.set_active_generation("G1")
    s.gens.allocate(turn_id=1)

    s.gens.mark_fenced("G1")
    s.gate.fence_generation("G1")

    for _ in range(5):
        assert s.gate.enqueue(np.full(480, gen_marker("G1"), np.int16), gen="G1") is False
    assert audible_samples(s.gate, "G1") == 0


# --- 3. recovery ------------------------------------------------------------------------------

def test_recovery_immediately_after_an_interruption(monkeypatch):
    rime = StreamingRime(chunks=6)
    s, trace = make_session(monkeypatch, ["first question", "second question"],
                            ScriptedLLM(["first answer", "second answer"]), rime=rime)

    rime.on_chunk = lambda i: interrupt(s) if i == 2 else None
    s.handle_utterance(AUDIO, 0.0)              # interrupted
    rime.on_chunk = None
    s.handle_utterance(AUDIO, 0.0)              # the interrupting utterance becomes the new turn

    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 1 and spoken[0].gen == "G2", "only the recovered turn speaks"
    assert rime.calls == ["first answer", "second answer"]

    emitted = emitted_by_generation(s.gate)          # one destructive drain, attributed
    assert emitted.get("G1", 0) == 0, "no stale audio leaked into the new response"
    assert emitted.get("G2", 0) > 0, "the new response is genuinely audible"


def test_multiple_interruptions_in_sequence(monkeypatch):
    rime = StreamingRime(chunks=6)
    s, trace = make_session(monkeypatch, ["what is your star rating", "is there a temple nearby", "what else can you tell me"],
                            ScriptedLLM(["a1", "a2", "a3"]), rime=rime)

    rime.on_chunk = lambda i: interrupt(s) if i == 1 else None
    s.handle_utterance(AUDIO, 0.0)              # G1 interrupted
    s.handle_utterance(AUDIO, 0.0)              # G2 interrupted
    rime.on_chunk = None
    s.handle_utterance(AUDIO, 0.0)              # G3 completes

    emitted = emitted_by_generation(s.gate)
    for stale in ("G1", "G2"):
        assert s.gens.get(stale).status is GenerationStatus.FENCED
        assert emitted.get(stale, 0) == 0, f"{stale} was fenced and must be inaudible"
    assert emitted.get("G3", 0) > 0, "the recovered turn must actually be heard"
    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 1 and spoken[0].gen == "G3"
    assert len(trace.all(EventType.FENCE_REQUESTED)) == 2, "every interruption is observable"


def test_generation_ids_stay_monotonic_across_interruptions(monkeypatch):
    rime = StreamingRime(chunks=4)
    s, trace = make_session(monkeypatch, ["what is your star rating", "is there a temple nearby", "what else can you tell me"],
                            ScriptedLLM(["a1", "a2", "a3"]), rime=rime)
    rime.on_chunk = lambda i: interrupt(s) if i == 1 else None
    s.handle_utterance(AUDIO, 0.0)
    s.handle_utterance(AUDIO, 0.0)
    rime.on_chunk = None
    s.handle_utterance(AUDIO, 0.0)

    changes = [e.fields["to_gen"] for e in trace.all(EventType.GENERATION_CHANGED)]
    assert changes == ["G1", "G2", "G3"]
    assert [int(g[1:]) for g in changes] == sorted(int(g[1:]) for g in changes)


# --- 4. conversation history ------------------------------------------------------------------

def test_an_interrupted_response_does_not_enter_history(monkeypatch):
    rime = StreamingRime(chunks=6)
    s, _ = make_session(monkeypatch, ["what is your star rating"], ScriptedLLM(["an answer the user never finished hearing"]),
                        rime=rime)
    rime.on_chunk = lambda i: interrupt(s) if i == 2 else None

    s.handle_utterance(AUDIO, 0.0)

    assert s.history.messages() == [], "a partially heard answer is not a completed turn"


def test_history_after_interruption_then_recovery_holds_only_the_completed_turn(monkeypatch):
    rime = StreamingRime(chunks=6)
    s, _ = make_session(monkeypatch, ["interrupted question", "real question"],
                        ScriptedLLM(["cut off answer", "complete answer"]), rime=rime)
    rime.on_chunk = lambda i: interrupt(s) if i == 2 else None
    s.handle_utterance(AUDIO, 0.0)
    rime.on_chunk = None
    s.handle_utterance(AUDIO, 0.0)

    assert s.history.messages() == [
        {"role": "user", "content": "real question"},
        {"role": "assistant", "content": "complete answer"},
    ]


def test_a_thinking_interruption_leaves_history_untouched(monkeypatch):
    llm = BlockingLLM("never heard")
    s, _ = make_session(monkeypatch, ["what is your star rating"], llm)
    worker = threading.Thread(target=s.handle_utterance, args=(AUDIO, 0.0))
    worker.start()
    llm.started.wait(timeout=5.0)
    interrupt(s)
    llm.release.set()
    worker.join(timeout=5.0)

    assert s.history.messages() == []


# --- 7. the safety property, stated directly ---------------------------------------------------

@pytest.mark.parametrize("fence_at_chunk", [0, 1, 2, 3])
def test_no_audio_of_a_fenced_generation_is_ever_audible(monkeypatch, fence_at_chunk):
    """The property itself, swept across every point in the stream at which a fence can land."""
    rime = StreamingRime(chunks=6)
    s, trace = make_session(monkeypatch, ["what is your star rating"], ScriptedLLM(["an answer"]), rime=rime)
    rime.on_chunk = lambda i: interrupt(s) if i == fence_at_chunk else None

    s.handle_utterance(AUDIO, 0.0)

    assert s.gens.get("G1").status is GenerationStatus.FENCED
    assert audible_samples(s.gate, "G1") == 0
    assert s.gate.enqueue(np.full(480, gen_marker("G1"), np.int16), gen="G1") is False
    assert trace.all(EventType.RESPONSE_SPOKEN) == []
    assert trace.all(EventType.RESULT_LEAKED) == []


def test_a_generation_that_is_never_fenced_is_fully_audible(monkeypatch):
    """The guard must not be so eager that a normal turn stops being heard."""
    rime = StreamingRime(chunks=5, samples=480)
    s, trace = make_session(monkeypatch, ["what is your star rating"], ScriptedLLM(["a clean answer"]), rime=rime)

    s.handle_utterance(AUDIO, 0.0)

    assert audible_samples(s.gate, "G1") == 5 * 480
    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 1 and spoken[0].fields["provider"] == "rime"
    assert s.history.turn_count() == 1


# --- in-flight bookkeeping --------------------------------------------------------------------

def test_turn_in_flight_is_cleared_on_every_exit_path(monkeypatch):
    """A stuck flag would arm barge-in forever and fence generations that were never in flight."""
    s, _ = make_session(monkeypatch, ["what is your star rating", ""], ScriptedLLM(["a1"]))

    s.handle_utterance(AUDIO, 0.0)                     # normal completion
    assert s.barge.turn_in_flight is False

    s.handle_utterance(AUDIO, 0.0)                     # empty transcript -> early return
    assert s.barge.turn_in_flight is False


def test_barge_in_is_not_armed_when_the_agent_is_idle(monkeypatch):
    """Speaking to an idle agent starts a turn; it must not fence a generation."""
    s, trace = make_session(monkeypatch, ["what is your star rating"], ScriptedLLM(["a1"]))

    assert s.gate.is_playing is False and s.barge.turn_in_flight is False
    interrupt(s)

    assert s.barge.armed is False, "there is nothing to interrupt"
    assert trace.all(EventType.FENCE_REQUESTED) == []
