"""Sentence streaming: the accumulator, and its interaction with fencing and history.

Two layers:

  * `SentenceAccumulator` alone -- pure text, no generations, no audio.
  * The streaming turn through the pipeline, with the real `GenerationRegistry`, real `AudioGate`
    and real `BargeInCoordinator`, so fencing is genuinely exercised rather than simulated.

The measured caveat that shaped the design is recorded in `test_gemini_latency.py`: Gemini does
not stream thinking as text, so a one-sentence reply arrives as a single chunk and streaming buys
nothing for it. These tests pin the mechanism, not a latency claim.
"""

# Transcripts here are placeholders -- this file tests the streaming/LLM path, not routing.
# They were single tokens ("q", "a question") until 2026-09-10, when AETHER learned to
# answer a one-word unrecognisable fragment with "could you say that again?" instead of
# handing it to the model. That is the intended behaviour, so the placeholders became
# sentences a caller could actually say. Every assertion below is unchanged.

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.player import AudioGate
from aether.audio.rime_ws import SpeakResult
from aether.events import EventType, GenerationStatus
from aether.llm import RetryingLLM, StubLLM, supports_streaming
from aether.sentences import SentenceAccumulator, stream_sentences
from aether.spike import MEANINGFUL_SPEECH_MS, Day1Spike
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)


# ============================ 1. the accumulator ============================

def test_tokens_accumulate_until_a_boundary():
    acc = SentenceAccumulator()
    assert acc.feed("The capital ") == []
    assert acc.feed("of France ") == []
    assert acc.buffered == "The capital of France "


def test_a_sentence_is_emitted_at_its_boundary():
    acc = SentenceAccumulator()
    acc.feed("The capital of France is Paris")
    assert acc.feed(". ") == ["The capital of France is Paris."]
    assert acc.buffered == ""


@pytest.mark.parametrize("terminator", [".", "!", "?", ";", ":"])
def test_every_strong_boundary_emits(terminator):
    acc = SentenceAccumulator()
    assert acc.feed(f"This is a full clause{terminator} ") == [f"This is a full clause{terminator}"]


def test_multiple_sentences_in_one_chunk():
    acc = SentenceAccumulator()
    out = acc.feed("Paris is the capital. It sits on the Seine. Lyon is south. ")
    assert out == ["Paris is the capital.", "It sits on the Seine.", "Lyon is south."]


def test_a_boundary_split_across_chunks():
    acc = SentenceAccumulator()
    assert acc.feed("Jupiter is the largest planet") == []
    assert acc.feed(".") == [], "a terminator at the very end may still be mid-number"
    assert acc.feed(" It is a gas giant. ") == [
        "Jupiter is the largest planet.", "It is a gas giant."
    ]


def test_decimal_numbers_are_not_sentence_boundaries():
    acc = SentenceAccumulator()
    assert acc.feed("The reading was 3.5 degrees warmer than before. ") == [
        "The reading was 3.5 degrees warmer than before."
    ]


def test_abbreviations_are_not_sentence_boundaries():
    acc = SentenceAccumulator()
    assert acc.feed("Dr. Smith examined the samples today. ") == [
        "Dr. Smith examined the samples today."
    ]


def test_a_fragment_shorter_than_the_minimum_is_held_back():
    acc = SentenceAccumulator(min_chars=12)
    assert acc.feed("No. ") == [], "too short to be a sentence on its own"
    assert acc.feed("It was not correct. ") == ["No. It was not correct."]


def test_flush_returns_the_tail_so_nothing_is_dropped():
    acc = SentenceAccumulator()
    acc.feed("A complete sentence here. And a trailing thought")
    assert acc.flush() == "And a trailing thought"
    assert acc.buffered == ""


def test_reset_discards_the_buffer():
    acc = SentenceAccumulator()
    acc.feed("half a thought that never")
    acc.reset()
    assert acc.buffered == "" and acc.flush() == ""


def test_stream_sentences_end_to_end():
    chunks = iter(["Paris is ", "the capital. ", "It is lovely", " in spring."])
    assert list(stream_sentences(chunks)) == [
        "Paris is the capital.", "It is lovely in spring."
    ]


def test_closing_punctuation_stays_with_its_sentence():
    acc = SentenceAccumulator()
    assert acc.feed('He said "this is the answer." Then he left. ') == [
        'He said "this is the answer."', "Then he left."
    ]


# ============================ 2. provider capability ============================

def test_streaming_is_an_optional_capability():
    assert supports_streaming(StubLLM()) is False


def test_the_blocking_respond_api_is_unchanged():
    """Phase 2 must not disturb the settled path."""
    assert StubLLM().respond("hello").startswith("You said: hello")
    assert RetryingLLM(StubLLM()).respond("hello").startswith("You said: hello")


# ============================ 3. streaming through the pipeline ============================

class StreamingLLM:
    """Yields scripted sentences; can pause between them so a fence can land mid-stream."""

    name = "streaming-fake"

    def __init__(self, sentences, on_sentence=None, fail_after=None):
        self._sentences = list(sentences)
        self.on_sentence = on_sentence
        self.fail_after = fail_after
        self.yielded = 0

    def respond(self, user_text, history=None):
        return " ".join(self._sentences)

    def respond_stream(self, user_text, history=None):
        for i, sentence in enumerate(self._sentences):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("stream broke")
            if self.on_sentence is not None:
                self.on_sentence(i)
            self.yielded += 1
            yield sentence


class SentenceRime:
    """Queues one chunk of PCM per sentence, honouring is_valid like the real client."""

    name = "rime"
    transport = "fake-ws"

    def __init__(self):
        self.calls: list[str] = []
        self.config = type("cfg", (), {"model": "mistv2", "voice": "astra"})()
        self.last_latency_ms = 1.0

    def speak(self, text, *, gate, gen, turn_id=None, is_valid=None):
        self.calls.append(text)
        if is_valid is not None and not is_valid():
            return SpeakResult(accepted=False, completed=False, reason="fenced_midstream")
        pcm = np.full(480, 1000, dtype=np.int16)
        ok = gate.enqueue(pcm, turn_id=turn_id, gen=gen)
        return SpeakResult(accepted=bool(ok), completed=bool(ok),
                           samples=len(pcm) if ok else 0,
                           first_audio_ms=1.0 if ok else None,
                           reason="" if ok else "gate_refused")


class ScriptedSTT:
    def __init__(self, trace, texts):
        self.trace, self._texts = trace, list(texts)

    def transcribe(self, audio, *, turn_id=None, gen=None):
        text = self._texts.pop(0) if self._texts else "what else can you tell me"
        self.trace.emit(EventType.TRANSCRIPT_FINAL, turn_id=turn_id, gen=gen, text=text)
        return text


class SilentMic:
    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None
        self.device = None

    def set_context(self, **k): ...


def make_session(monkeypatch, transcripts, llm, rime=None):
    monkeypatch.setenv("AETHER_LLM_STREAMING", "1")
    trace = Trace()
    gate = AudioGate(trace)
    the_rime = rime or SentenceRime()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: gate)
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: SilentMic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: ScriptedSTT(trace, transcripts))
    monkeypatch.setattr("aether.spike.build_llm", lambda: llm)
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: the_rime)
    s = Day1Spike(trace)
    s.rime = the_rime
    return s, trace


def interrupt(session):
    session.mic.on_onset(0.0)
    session.mic.on_voiced_progress(MEANINGFUL_SPEECH_MS)


def emitted(gate, blocks=40):
    total = 0
    for _ in range(blocks):
        buf = np.zeros((gate.blocksize, 1), dtype=np.int16)
        gate._callback(buf, gate.blocksize, None, None)
        total += int(np.count_nonzero(buf[:, 0]))
    return total


# --- completion ---------------------------------------------------------------------------

def test_each_sentence_is_spoken_as_it_arrives(monkeypatch):
    llm = StreamingLLM(["Paris is the capital.", "It sits on the Seine."])
    s, trace = make_session(monkeypatch, ["what is your star rating"], llm)

    s.handle_utterance(AUDIO, 0.0)

    assert s.rime.calls == ["Paris is the capital.", "It sits on the Seine."], (
        "synthesis must start per sentence, not once at the end"
    )
    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 1, "one ResponseSpoken per turn, not per sentence"
    assert spoken[0].fields["text"] == "Paris is the capital. It sits on the Seine."


def test_history_holds_the_complete_reply_after_a_successful_stream(monkeypatch):
    llm = StreamingLLM(["Paris is the capital.", "It sits on the Seine."])
    s, _ = make_session(monkeypatch, ["what is your star rating"], llm)

    s.handle_utterance(AUDIO, 0.0)

    assert s.history.messages() == [
        {"role": "user", "content": "what is your star rating"},
        {"role": "assistant", "content": "Paris is the capital. It sits on the Seine."},
    ]


# --- fencing ------------------------------------------------------------------------------

def test_fencing_before_the_first_sentence_speaks_nothing(monkeypatch):
    llm = StreamingLLM(["Paris is the capital.", "It sits on the Seine."])
    s, trace = make_session(monkeypatch, ["what is your star rating"], llm)
    llm.on_sentence = lambda i: interrupt(s) if i == 0 else None

    s.handle_utterance(AUDIO, 0.0)

    assert s.rime.calls == [], "not one sentence may be synthesised"
    assert trace.all(EventType.RESPONSE_SPOKEN) == []
    assert s.history.messages() == []
    assert emitted(s.gate) == 0


def test_fencing_after_the_first_sentence_stops_the_rest(monkeypatch):
    llm = StreamingLLM(["Paris is the capital.", "It sits on the Seine.", "Lyon is south."])
    s, trace = make_session(monkeypatch, ["what is your star rating"], llm)
    llm.on_sentence = lambda i: interrupt(s) if i == 1 else None

    s.handle_utterance(AUDIO, 0.0)

    assert s.rime.calls == ["Paris is the capital."], "later sentences must not be synthesised"
    assert trace.all(EventType.RESPONSE_SPOKEN) == [], "a cut-off reply was never fully spoken"
    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert discarded and discarded[0].fields["reason"] == "stale_generation_stream"


def test_no_stale_audio_is_audible_after_a_mid_stream_fence(monkeypatch):
    llm = StreamingLLM(["Paris is the capital.", "It sits on the Seine.", "Lyon is south."])
    s, _ = make_session(monkeypatch, ["what is your star rating"], llm)
    llm.on_sentence = lambda i: interrupt(s) if i == 1 else None

    s.handle_utterance(AUDIO, 0.0)

    assert s.gens.get("G1").status is GenerationStatus.FENCED
    assert emitted(s.gate) == 0, "the fence flushed what had been queued"
    assert s.gate.enqueue(np.full(480, 1000, np.int16), gen="G1") is False


def test_a_partial_stream_is_never_committed_to_history(monkeypatch):
    """Level 1: a turn is remembered whole or not at all."""
    llm = StreamingLLM(["Paris is the capital.", "It sits on the Seine."])
    s, _ = make_session(monkeypatch, ["what is your star rating"], llm)
    llm.on_sentence = lambda i: interrupt(s) if i == 1 else None

    s.handle_utterance(AUDIO, 0.0)

    assert s.history.messages() == [], "a half-spoken answer is not a completed turn"


def test_recovery_after_a_streamed_interruption(monkeypatch):
    llm = StreamingLLM(["First answer here.", "Second part follows."])
    s, trace = make_session(monkeypatch, ["what is your star rating", "is there a temple nearby"], llm)
    llm.on_sentence = lambda i: interrupt(s) if i == 1 else None
    s.handle_utterance(AUDIO, 0.0)

    llm._sentences = ["A clean recovered answer."]
    llm.on_sentence = None
    s.handle_utterance(AUDIO, 0.0)

    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 1 and spoken[0].gen == "G2"
    assert s.history.turn_count() == 1


# --- failure ------------------------------------------------------------------------------

def test_a_stream_that_fails_midway_speaks_nothing_further_and_invents_nothing(monkeypatch):
    llm = StreamingLLM(["Paris is the capital.", "It sits on the Seine."], fail_after=1)
    s, trace = make_session(monkeypatch, ["what is your star rating"], llm)

    s.handle_utterance(AUDIO, 0.0)   # must not raise

    assert trace.all(EventType.RESPONSE_SPOKEN) == []
    assert s.history.messages() == [], "an incomplete reply is not a turn"
    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert discarded and discarded[0].fields["reason"] == "llm_stream_error"


def test_a_stream_that_yields_nothing_is_discarded_not_invented(monkeypatch):
    llm = StreamingLLM([])
    s, trace = make_session(monkeypatch, ["what is your star rating"], llm)

    s.handle_utterance(AUDIO, 0.0)

    assert s.rime.calls == []
    assert s.history.messages() == []
    discarded = trace.all(EventType.RESULT_DISCARDED)
    assert discarded and discarded[0].fields["reason"] == "empty_llm_response"


def test_the_session_survives_a_failed_stream_and_the_next_turn_works(monkeypatch):
    llm = StreamingLLM(["Broken."], fail_after=0)
    s, trace = make_session(monkeypatch, ["what is your star rating", "is there a temple nearby"], llm)
    s.handle_utterance(AUDIO, 0.0)

    llm._sentences = ["A good answer this time."]
    llm.fail_after = None
    s.handle_utterance(AUDIO, 0.0)

    assert len(trace.all(EventType.RESPONSE_SPOKEN)) == 1
    assert s.history.turn_count() == 1


# --- the blocking path still exists -----------------------------------------------------------

def test_streaming_can_be_disabled_and_the_blocking_path_still_works(monkeypatch):
    llm = StreamingLLM(["One sentence answer."])
    s, trace = make_session(monkeypatch, ["what is your star rating"], llm)
    monkeypatch.setenv("AETHER_LLM_STREAMING", "0")
    s._streaming_enabled = False

    s.handle_utterance(AUDIO, 0.0)

    assert llm.yielded == 0, "the stream must not be used when disabled"
    assert s.rime.calls == ["One sentence answer."], "the whole reply is synthesised at once"
    assert len(trace.all(EventType.RESPONSE_SPOKEN)) == 1


def test_a_provider_without_streaming_uses_the_blocking_path(monkeypatch):
    class BlockingOnly:
        name = "blocking-only"

        def respond(self, user_text, history=None):
            return "A blocking answer."

    s, trace = make_session(monkeypatch, ["what is your star rating"], BlockingOnly())

    s.handle_utterance(AUDIO, 0.0)

    assert s.rime.calls == ["A blocking answer."]
    assert len(trace.all(EventType.RESPONSE_SPOKEN)) == 1
