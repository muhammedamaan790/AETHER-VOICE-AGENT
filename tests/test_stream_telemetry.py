"""High-resolution stream telemetry.

Three questions the pipeline could not previously answer, all measured from the same request start:

    llm_request_sent_ms   when the network call was initiated
    llm_ttft_ms           to the first RAW provider chunk
    llm_total_ms          to stream exhaustion

plus `llm_first_sentence_ms`, which is deliberately separate. The accumulator holds text back
until a linguistic boundary, so timing TTFT at the first *sentence* would blame the accumulator for
the provider's latency, or hide it. Keeping both makes the split visible.

`llm_transport_reused` answers the connection question directly: the SDK builds one persistent
httpx client, so a stable identity across turns means no new TCP/TLS handshake per request.

All of it rides on the existing `ResponseSpoken` event -- no new event type.
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
from aether.events import EventType
from aether.spike import MEANINGFUL_SPEECH_MS, Day1Spike
from aether.trace import Trace, now_ms

AUDIO = np.zeros(16000, np.int16)

METRIC_FIELDS = (
    "llm_request_sent_ms", "llm_ttft_ms", "llm_total_ms", "llm_first_sentence_ms",
    "llm_raw_chunks", "llm_transport_reused", "llm_client_init_ms", "llm_requests_made",
)


class TimedStreamingLLM:
    """Reports stream timings the way the real Gemini adapter does."""

    name = "timed-fake"

    def __init__(self, sentences, ttft_ms=120.0, total_ms=900.0, on_sentence=None,
                 transport_reused=True, raw_chunks=None):
        self._sentences = list(sentences)
        self.on_sentence = on_sentence
        self.last_stream_timing = None
        self._ttft, self._total = ttft_ms, total_ms
        self._transport_reused = transport_reused
        self._raw_chunks = raw_chunks if raw_chunks is not None else len(sentences)

    def respond(self, user_text, history=None):
        return " ".join(self._sentences)

    def respond_stream(self, user_text, history=None):
        request_sent = now_ms()
        try:
            for i, sentence in enumerate(self._sentences):
                if self.on_sentence is not None:
                    self.on_sentence(i)
                yield sentence
        finally:
            # Recorded in a finally, exactly as the adapter does, so a consumer that stops early
            # still leaves measurements behind.
            self.last_stream_timing = {
                "request_sent_ms": round(request_sent, 3),
                "ttft_ms": self._ttft,
                "total_ms": self._total,
                "raw_chunks": self._raw_chunks,
                "transport_reused": self._transport_reused,
                "client_init_ms": 4.2,
                "requests_made": 1,
            }


class SentenceRime:
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
                           first_audio_ms=1.0 if ok else None)


class ScriptedSTT:
    def __init__(self, trace, texts):
        self.trace, self._texts = trace, list(texts)

    def transcribe(self, audio, *, turn_id=None, gen=None):
        text = self._texts.pop(0) if self._texts else "what is your star rating"
        self.trace.emit(EventType.TRANSCRIPT_FINAL, turn_id=turn_id, gen=gen, text=text)
        return text


class SilentMic:
    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None
        self.device = None

    def set_context(self, **k): ...


def make_session(monkeypatch, llm, transcripts=("what is your star rating",)):
    monkeypatch.setenv("AETHER_LLM_STREAMING", "1")
    trace = Trace()
    gate = AudioGate(trace)
    rime = SentenceRime()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: gate)
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: SilentMic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: ScriptedSTT(trace, transcripts))
    monkeypatch.setattr("aether.spike.build_llm", lambda: llm)
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: rime)
    s = Day1Spike(trace)
    s.rime = rime
    return s, trace


def interrupt(session):
    session.mic.on_onset(0.0)
    session.mic.on_voiced_progress(MEANINGFUL_SPEECH_MS)


# --- standard stream -----------------------------------------------------------------------

def test_all_three_metrics_reach_the_trace(monkeypatch):
    llm = TimedStreamingLLM(["Paris is the capital.", "It sits on the Seine."],
                            ttft_ms=210.0, total_ms=1400.0)
    s, trace = make_session(monkeypatch, llm)

    s.handle_utterance(AUDIO, 0.0)

    f = trace.last(EventType.RESPONSE_SPOKEN).fields
    for field in METRIC_FIELDS:
        assert field in f, f"{field} missing from ResponseSpoken"
    assert f["llm_ttft_ms"] == 210.0
    assert f["llm_total_ms"] == 1400.0
    assert f["llm_request_sent_ms"] > 0


def test_no_new_event_type_was_introduced():
    assert len(list(EventType)) == 18


def test_ttft_and_first_sentence_are_measured_separately(monkeypatch):
    """The accumulator holds text until a boundary; conflating the two would hide that."""
    llm = TimedStreamingLLM(["A first complete sentence."], ttft_ms=95.0, total_ms=800.0)
    s, trace = make_session(monkeypatch, llm)

    s.handle_utterance(AUDIO, 0.0)

    f = trace.last(EventType.RESPONSE_SPOKEN).fields
    assert f["llm_ttft_ms"] == 95.0, "raw provider TTFT comes from the adapter"
    assert f["llm_first_sentence_ms"] is not None, "sentence assembly is timed by the pipeline"
    assert f["llm_first_sentence_ms"] >= 0


def test_total_is_never_less_than_ttft(monkeypatch):
    llm = TimedStreamingLLM(["One sentence here."], ttft_ms=300.0, total_ms=1200.0)
    s, trace = make_session(monkeypatch, llm)

    s.handle_utterance(AUDIO, 0.0)

    f = trace.last(EventType.RESPONSE_SPOKEN).fields
    assert f["llm_total_ms"] >= f["llm_ttft_ms"]


def test_raw_chunk_count_is_recorded(monkeypatch):
    llm = TimedStreamingLLM(["One.", "Two.", "Three."], raw_chunks=7)
    s, trace = make_session(monkeypatch, llm)

    s.handle_utterance(AUDIO, 0.0)

    assert trace.last(EventType.RESPONSE_SPOKEN).fields["llm_raw_chunks"] == 7


# --- connection reuse ------------------------------------------------------------------------

def test_transport_reuse_is_reported(monkeypatch):
    """The question this instrumentation exists to answer: is a handshake paid per turn?"""
    llm = TimedStreamingLLM(["A sentence here."], transport_reused=True)
    s, trace = make_session(monkeypatch, llm)

    s.handle_utterance(AUDIO, 0.0)

    assert trace.last(EventType.RESPONSE_SPOKEN).fields["llm_transport_reused"] is True


def test_a_recreated_transport_would_be_visible(monkeypatch):
    """If anything ever started rebuilding the client per turn, the trace would say so."""
    llm = TimedStreamingLLM(["A sentence here."], transport_reused=False)
    s, trace = make_session(monkeypatch, llm)

    s.handle_utterance(AUDIO, 0.0)

    assert trace.last(EventType.RESPONSE_SPOKEN).fields["llm_transport_reused"] is False


def test_client_init_cost_is_recorded_once_not_per_turn(monkeypatch):
    llm = TimedStreamingLLM(["A sentence here."])
    s, trace = make_session(monkeypatch, llm, transcripts=("what is your star rating", "is there a temple nearby"))

    s.handle_utterance(AUDIO, 0.0)
    s.handle_utterance(AUDIO, 0.0)

    spoken = trace.all(EventType.RESPONSE_SPOKEN)
    assert len(spoken) == 2
    assert {e.fields["llm_client_init_ms"] for e in spoken} == {4.2}, (
        "construction cost is a session constant, not a per-turn charge"
    )


# --- fenced stream ---------------------------------------------------------------------------

def test_metrics_are_still_recorded_when_the_stream_is_fenced(monkeypatch):
    """A fenced turn's provider timing is evidence too -- it must not be lost."""
    llm = TimedStreamingLLM(["First sentence here.", "Second sentence here."],
                            ttft_ms=180.0, total_ms=650.0)
    s, trace = make_session(monkeypatch, llm)
    llm.on_sentence = lambda i: interrupt(s) if i == 1 else None

    s.handle_utterance(AUDIO, 0.0)

    assert trace.all(EventType.RESPONSE_SPOKEN) == [], "a fenced turn is not spoken"
    assert s._last_stream_metrics["llm_ttft_ms"] == 180.0
    assert s._last_stream_metrics["llm_total_ms"] == 650.0


def test_fencing_before_the_first_sentence_still_reports_provider_latency(monkeypatch):
    """The sentence was assembled, then discarded unspoken. Provider latency is what this metric
    measures, so it stands: blanking it would throw away the evidence the telemetry exists for."""
    llm = TimedStreamingLLM(["Only sentence here."], ttft_ms=140.0)
    s, trace = make_session(monkeypatch, llm)
    llm.on_sentence = lambda i: interrupt(s) if i == 0 else None

    s.handle_utterance(AUDIO, 0.0)

    assert trace.all(EventType.RESPONSE_SPOKEN) == [], "nothing was spoken"
    assert s._last_stream_metrics["llm_ttft_ms"] == 140.0
    assert s._last_stream_metrics["llm_first_sentence_ms"] is not None, (
        "a sentence was produced before the fence, and how long that took is still evidence"
    )


# --- honesty about absent measurements ---------------------------------------------------------

def test_a_provider_that_reports_nothing_yields_none_not_zero(monkeypatch):
    class BareStreamingLLM:
        name = "bare-fake"

        def respond(self, user_text, history=None):
            return "Bare answer here."

        def respond_stream(self, user_text, history=None):
            yield "Bare answer here."

    s, trace = make_session(monkeypatch, BareStreamingLLM())

    s.handle_utterance(AUDIO, 0.0)

    f = trace.last(EventType.RESPONSE_SPOKEN).fields
    assert f["llm_ttft_ms"] is None, "absent must read as absent, never as 0"
    assert f["llm_total_ms"] is None
    assert f["llm_transport_reused"] is None


def test_the_blocking_path_reports_no_stream_metrics(monkeypatch):
    """Metrics belong to the streaming path; the blocking path must not fake them."""
    llm = TimedStreamingLLM(["A sentence here."])
    s, trace = make_session(monkeypatch, llm)
    s._streaming_enabled = False

    s.handle_utterance(AUDIO, 0.0)

    f = trace.last(EventType.RESPONSE_SPOKEN).fields
    assert f.get("llm_ttft_ms") is None


# --- the adapter's own accounting ---------------------------------------------------------------

def test_gemini_adapter_records_ttft_on_the_raw_chunk(monkeypatch):
    """TTFT must come from the first raw chunk, before sentence assembly holds anything back."""
    class Chunk:
        def __init__(self, text):
            self.text = text
            self.usage_metadata = None

    class FakeModels:
        def generate_content_stream(self, **kwargs):
            # A long first fragment with no boundary: sentence assembly emits nothing yet.
            yield Chunk("The capital of France ")
            yield Chunk("is Paris. ")

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    from google import genai
    monkeypatch.setattr(genai, "Client", FakeClient)

    from aether.llm import GeminiLLM
    llm = GeminiLLM("key-not-real", "gemini-3.8-flash")
    sentences = list(llm.respond_stream("what is your star rating"))

    assert sentences == ["The capital of France is Paris."]
    t = llm.last_stream_timing
    assert t["raw_chunks"] == 2, "both raw chunks counted, not just the emitted sentence"
    assert t["ttft_ms"] is not None and t["ttft_ms"] >= 0
    assert t["total_ms"] >= t["ttft_ms"]
    assert t["requests_made"] == 1


def test_gemini_adapter_records_timing_even_if_the_consumer_stops_early(monkeypatch):
    class Chunk:
        def __init__(self, text):
            self.text = text
            self.usage_metadata = None

    class FakeModels:
        def generate_content_stream(self, **kwargs):
            yield Chunk("First complete sentence. ")
            yield Chunk("Second complete sentence. ")

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    from google import genai
    monkeypatch.setattr(genai, "Client", FakeClient)

    from aether.llm import GeminiLLM
    llm = GeminiLLM("key-not-real", "gemini-3.8-flash")

    stream = llm.respond_stream("what is your star rating")
    next(stream)          # take one sentence, then abandon the stream as a fence does
    stream.close()

    assert llm.last_stream_timing is not None, "a fenced stream must still leave measurements"
    assert llm.last_stream_timing["ttft_ms"] is not None
