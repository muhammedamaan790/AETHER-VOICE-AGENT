"""Per-turn latency breakdown: the arithmetic, and the fields that reach the trace.

Two layers:
  - TurnTiming in isolation, so the deltas are provable without a session.
  - The pipeline, so a real turn actually carries the breakdown on ResponseSpoken.

No timing assertions on wall-clock durations here -- only on the arithmetic and on ordering.
Instrumentation must never change what is spoken, so that is asserted too.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.rime_ws import SpeakResult
from aether.events import EventType
from aether.spike import Day1Spike
from aether.timing import TurnTiming
from aether.trace import Trace, now_ms

AUDIO = np.zeros(16000, np.int16)


# --- the arithmetic ---------------------------------------------------------------------

def test_component_stages_are_the_expected_differences():
    t = TurnTiming(
        speech_ended=1000.0, transcript=1300.0,
        llm_start=1310.0, llm_end=2200.0,
        first_audio=3700.0, spoken=3750.0,
    )
    assert t.stt_ms == 300.0          # 1300 - 1000
    assert t.llm_ms == 890.0          # 2200 - 1310
    assert t.tts_ms == 1500.0         # 3700 - 2200


def test_turn_latency_is_the_silence_the_user_sits_through():
    t = TurnTiming(speech_ended=1000.0, first_audio=3700.0)
    assert t.turn_latency_ms == 2700.0, "stopped talking at 1000, heard nothing until 3700"


def test_response_latency_matches_the_prd_definition():
    """PRD.md section 6: response_latency_ms = ResponseSpoken.t - TranscriptFinal.t."""
    t = TurnTiming(transcript=1300.0, spoken=3750.0)
    assert t.response_latency_ms == 2450.0


def test_components_account_for_the_whole_turn():
    """stt + llm + tts should reconstruct turn_latency, minus the gap between stt and llm start."""
    t = TurnTiming(
        speech_ended=1000.0, transcript=1300.0,
        llm_start=1300.0, llm_end=2200.0,
        first_audio=3700.0,
    )
    assert t.stt_ms + t.llm_ms + t.tts_ms == t.turn_latency_ms


@pytest.mark.parametrize("missing", ["speech_ended", "transcript", "llm_start", "llm_end", "first_audio"])
def test_a_missing_mark_yields_none_not_a_wrong_number(missing):
    marks = dict(speech_ended=1000.0, transcript=1300.0, llm_start=1310.0,
                 llm_end=2200.0, first_audio=3700.0, spoken=3750.0)
    marks[missing] = None
    t = TurnTiming(**marks)
    assert None in (t.stt_ms, t.llm_ms, t.tts_ms, t.turn_latency_ms), (
        "a delta that depends on a missing mark must be None, never invented"
    )


def test_empty_timing_is_all_none():
    t = TurnTiming()
    assert t.fields() == {k: None for k in t.fields()}


def test_out_of_order_marks_are_reported_not_hidden():
    """A negative delta means the marks were recorded wrongly. Clamping would mask the bug."""
    t = TurnTiming(speech_ended=2000.0, first_audio=1000.0)
    assert t.turn_latency_ms == -1000.0


def test_delta_rounds_to_one_decimal():
    assert TurnTiming.delta(1000.567, 1000.0) == 0.6


def test_summary_is_readable_and_marks_unknowns():
    t = TurnTiming(speech_ended=1000.0, transcript=1300.0, llm_start=1300.0,
                   llm_end=2200.0, first_audio=3700.0)
    assert t.summary() == "stt=300 llm=900 tts=1500 -> turn=2700 ms"
    assert TurnTiming().summary() == "stt=? llm=? tts=? -> turn=? ms"


def test_fields_are_flat_and_complete():
    keys = set(TurnTiming().fields())
    assert keys == {
        "t_speech_ended", "t_transcript", "t_llm_start", "t_llm_end", "t_first_audio", "t_spoken",
        "stt_ms", "llm_ms", "tts_ms", "turn_latency_ms", "response_latency_ms",
    }
    assert all(not isinstance(v, dict) for v in TurnTiming().fields().values()), "flat, greppable"


# --- the pipeline carries the breakdown ---------------------------------------------------

class FakeLLM:
    name = "fake-llm"

    def respond(self, user_text, history=None):
        return "an answer"


class FakeRime:
    name = "rime"
    transport = "fake"

    def __init__(self):
        self.calls: list[str] = []
        self.config = type("cfg", (), {"model": "mistv2", "voice": "astra"})()
        self.last_latency_ms = 12.0

    def speak(self, text, *, gate, gen, turn_id=None, is_valid=None):
        # Report first_audio_ms measured against this call's own start, exactly as the real
        # clients do. A hardcoded value would invent a timestamp in the future and make the
        # ordering assertion below meaningless.
        t0 = now_ms()
        self.calls.append(text)
        pcm = np.zeros(gate.samplerate // 10, dtype=np.int16)
        accepted = gate.enqueue(pcm, turn_id=turn_id, gen=gen)
        return SpeakResult(accepted=bool(accepted), completed=True,
                           samples=len(pcm), first_audio_ms=now_ms() - t0)


class _FakeGate:
    samplerate = 48000

    def __init__(self):
        self.active_gen = None
        self.is_ducked = False
        self.is_playing = True

    def set_active_generation(self, gen, *, turn_id=None):
        self.active_gen = gen

    def fence_generation(self, gen, *, reason="fenced"):
        self.active_gen = None

    def enqueue(self, pcm, *, turn_id=None, gen=None):
        return gen is None or gen == self.active_gen

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


@pytest.fixture
def session(monkeypatch):
    trace = Trace()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: _FakeGate())
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _FakeMic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: _FakeSTT(trace))
    monkeypatch.setattr("aether.spike.build_llm", lambda: FakeLLM())
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: FakeRime())
    s = Day1Spike(trace)
    s.rime = FakeRime()
    return s, trace


def test_response_spoken_carries_the_full_breakdown(session):
    s, trace = session
    trace.emit(EventType.SPEECH_ENDED, duration_ms=900.0)   # the VAD mark

    s.handle_utterance(AUDIO, 0.0)

    spoken = trace.last(EventType.RESPONSE_SPOKEN)
    assert spoken is not None
    for field in ("t_speech_ended", "t_transcript", "t_llm_start", "t_llm_end",
                  "t_first_audio", "t_spoken", "stt_ms", "llm_ms", "tts_ms",
                  "turn_latency_ms", "response_latency_ms"):
        assert field in spoken.fields, f"{field} missing from ResponseSpoken"
        assert spoken.fields[field] is not None, f"{field} was not measured"


def test_marks_are_in_chronological_order(session):
    s, trace = session
    trace.emit(EventType.SPEECH_ENDED, duration_ms=900.0)

    s.handle_utterance(AUDIO, 0.0)
    f = trace.last(EventType.RESPONSE_SPOKEN).fields

    marks = [f["t_speech_ended"], f["t_transcript"], f["t_llm_start"],
             f["t_llm_end"], f["t_first_audio"], f["t_spoken"]]
    assert marks == sorted(marks), f"marks recorded out of order: {marks}"
    assert all(f[d] >= 0 for d in ("stt_ms", "llm_ms", "tts_ms", "turn_latency_ms"))


def test_existing_response_spoken_fields_are_preserved(session):
    """Instrumentation must not displace what was already recorded."""
    s, trace = session
    trace.emit(EventType.SPEECH_ENDED, duration_ms=900.0)

    s.handle_utterance(AUDIO, 0.0)
    f = trace.last(EventType.RESPONSE_SPOKEN).fields

    assert f["provider"] == "rime"
    assert f["transport"] == "fake"
    assert f["text"] == "an answer"
    assert f["completed"] is True
    assert "audio_ms" in f and "tts_latency_ms" in f


def test_timing_survives_a_missing_speech_ended_mark(session):
    """No SpeechEnded (e.g. a typed/injected turn): the turn still completes, deltas go None."""
    s, trace = session

    s.handle_utterance(AUDIO, 0.0)   # no SPEECH_ENDED emitted first

    spoken = trace.last(EventType.RESPONSE_SPOKEN)
    assert spoken is not None, "a missing mark must not break the turn"
    assert spoken.fields["t_speech_ended"] is None
    assert spoken.fields["turn_latency_ms"] is None
    assert spoken.fields["llm_ms"] is not None, "independent stages are still measured"


def test_instrumentation_does_not_change_what_is_spoken(session):
    s, trace = session
    trace.emit(EventType.SPEECH_ENDED, duration_ms=900.0)

    s.handle_utterance(AUDIO, 0.0)

    assert s.rime.calls == ["an answer"]
    assert len(trace.all(EventType.RESPONSE_SPOKEN)) == 1
    assert trace.all(EventType.RESULT_DISCARDED) == []
    assert s.history.turn_count() == 1


def test_no_new_event_type_was_introduced():
    """The breakdown rides on ResponseSpoken; the canonical vocabulary is unchanged."""
    assert len(list(EventType)) == 18
