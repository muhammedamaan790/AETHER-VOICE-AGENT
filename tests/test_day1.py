"""Day-1 tests: trace spine, generation IDs, audio gate duck/stop, VAD onset/offset.

Tests marked `audio` open real PortAudio streams and need a working output device.
Run without them:  pytest -m "not audio"
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from aether.events import EventType, GenerationStatus
from aether.trace import Trace, read_trace
from aether.supervisor.generations import GenerationRegistry

SR_IN = 16000
FRAME_N = 320  # 20 ms


def voiced_frame(offset: int, f0: float = 140.0, amp: float = 0.35) -> np.ndarray:
    t = (np.arange(FRAME_N) + offset) / SR_IN
    sig = np.zeros(FRAME_N)
    for k, a in [(1, 1.0), (2, 0.6), (3, 0.45), (4, 0.3), (5, 0.2), (8, 0.12), (12, 0.08)]:
        sig += a * np.sin(2 * np.pi * f0 * k * t)
    sig += 0.05 * np.random.randn(FRAME_N)
    sig = sig / np.abs(sig).max() * amp
    return (sig * 32767).astype(np.int16)


SILENCE = np.zeros(FRAME_N, dtype=np.int16)


# --- trace -------------------------------------------------------------------------

def test_trace_emits_monotonic_seq_and_time():
    tr = Trace()
    a = tr.emit(EventType.SPEECH_ONSET)
    b = tr.emit(EventType.AUDIO_DUCKED)
    c = tr.emit(EventType.AUDIO_STOPPED)
    assert [a.seq, b.seq, c.seq] == [1, 2, 3]
    assert a.t <= b.t <= c.t


def test_trace_writes_readable_jsonl(tmp_path):
    path = tmp_path / "run.jsonl"
    tr = Trace(path)
    tr.emit(EventType.TRANSCRIPT_FINAL, turn_id=1, gen="G1", text="hello there")
    tr.close()

    rows = read_trace(path)
    assert len(rows) == 1
    assert rows[0]["type"] == "TranscriptFinal"
    assert rows[0]["gen"] == "G1"
    assert rows[0]["text"] == "hello there"   # per-event fields are flattened into the envelope
    json.dumps(rows)  # round-trips


def test_trace_t_override_is_used():
    tr = Trace()
    ev = tr.emit(EventType.AUDIO_STOPPED, t=123.5)
    assert ev.t == 123.5


def test_only_canonical_event_names_are_written():
    """Guards RULES.md R3.2 -- the trace can only contain names from the canonical vocabulary."""
    tr = Trace()
    for et in EventType:
        tr.emit(et)
    canonical = {e.value for e in EventType}
    assert {e.type for e in tr.events} <= canonical


# --- generations -------------------------------------------------------------------

def test_generation_ids_are_monotonic_and_previous_is_fenced():
    tr = Trace()
    reg = GenerationRegistry(tr)
    g1 = reg.allocate(turn_id=1)
    g2 = reg.allocate(turn_id=2)

    assert (g1.id, g2.id) == ("G1", "G2")
    assert g1.status is GenerationStatus.FENCED
    assert g2.status is GenerationStatus.ACTIVE
    assert reg.is_active("G2") and not reg.is_active("G1")


def test_generation_change_is_traced():
    tr = Trace()
    reg = GenerationRegistry(tr)
    reg.allocate()
    reg.allocate()
    changes = tr.all(EventType.GENERATION_CHANGED)
    assert len(changes) == 2
    assert changes[1].fields["from_gen"] == "G1"
    assert changes[1].fields["to_gen"] == "G2"
    assert changes[1].fields["to_status"] == "active"


# --- audio gate --------------------------------------------------------------------

@pytest.fixture
def gate():
    from aether.audio.player import AudioGate
    tr = Trace()
    g = AudioGate(tr)
    g.open()
    yield g, tr
    g.close()


@pytest.mark.audio
def test_duck_is_applied_and_traced(gate):
    g, tr = gate
    g.enqueue(np.zeros(g.samplerate, dtype=np.int16))
    assert g.duck(timeout=1.0), "duck was not applied within 1s"
    assert g.is_ducked
    ev = tr.first(EventType.AUDIO_DUCKED)
    assert ev is not None and ev.fields["gain"] == g.duck_gain


@pytest.mark.audio
def test_stop_flushes_queue_and_traces_stream_latency(gate):
    g, tr = gate
    g.enqueue(np.zeros(g.samplerate * 5, dtype=np.int16))
    assert g.is_playing
    assert g.stop(timeout=1.0), "stop was not applied within 1s"
    assert not g.is_playing, "stop must flush queued audio"
    ev = tr.first(EventType.AUDIO_STOPPED)
    assert ev is not None
    assert "stream_output_latency_ms" in ev.fields   # residual is disclosed, not hidden


@pytest.mark.audio
def test_duck_then_resume_restores_full_gain(gate):
    g, tr = gate
    g.enqueue(np.zeros(g.samplerate * 2, dtype=np.int16))
    assert g.duck(timeout=1.0)
    assert g.resume(timeout=1.0)
    assert not g.is_ducked
    assert tr.first(EventType.AUDIO_RESUMED) is not None


@pytest.mark.audio
def test_duck_and_stop_are_distinct_events(gate):
    """RULES.md R6 -- duck and stop must never be collapsed into one action."""
    g, tr = gate
    g.enqueue(np.zeros(g.samplerate * 3, dtype=np.int16))
    g.duck(timeout=1.0)
    g.stop(timeout=1.0)
    assert tr.first(EventType.AUDIO_DUCKED) is not None
    assert tr.first(EventType.AUDIO_STOPPED) is not None
    assert tr.first(EventType.AUDIO_DUCKED).t <= tr.first(EventType.AUDIO_STOPPED).t


def test_gate_rejects_non_int16_pcm():
    from aether.audio.player import AudioGate
    g = AudioGate(Trace())
    with pytest.raises(TypeError):
        g.enqueue(np.zeros(100, dtype=np.float32))


# --- VAD ---------------------------------------------------------------------------

def make_vad(trace, **kw):
    from aether.audio.vad import MicVAD
    return MicVAD(trace, **kw)


def test_vad_emits_onset_after_confirmation_window():
    tr = Trace()
    vad = make_vad(tr)
    for _ in range(5):
        vad.process_frame(SILENCE)
    assert tr.first(EventType.SPEECH_ONSET) is None

    for i in range(5):
        vad.process_frame(voiced_frame(i * FRAME_N))
    onset = tr.first(EventType.SPEECH_ONSET)
    assert onset is not None
    # t is the first voiced frame, declared later -> confirm window is included in the metric
    assert onset.fields["declared_t"] >= onset.t
    assert onset.fields["confirm_ms"] >= 0


def test_vad_emits_speech_ended_and_delivers_utterance():
    tr = Trace()
    vad = make_vad(tr)
    for i in range(15):
        vad.process_frame(voiced_frame(i * FRAME_N))
    # WebRTC VAD has ~6 frames (120 ms) of internal hangover: it keeps reporting speech for a
    # short while after the audio goes quiet. So reaching `offset_frames` CONSECUTIVE unvoiced
    # frames needs offset_frames + hangover of real silence. Measured at aggressiveness 2.
    for _ in range(vad.offset_frames + 10):
        vad.process_frame(SILENCE)

    assert tr.first(EventType.SPEECH_ENDED) is not None
    audio, onset_t = vad.utterances.get_nowait()
    assert len(audio) > 0
    assert onset_t > 0


def test_vad_ignores_silence_entirely():
    tr = Trace()
    vad = make_vad(tr)
    for _ in range(100):
        vad.process_frame(SILENCE)
    assert tr.all(EventType.SPEECH_ONSET) == []


def test_vad_onset_fires_the_realtime_callback():
    """on_onset must be invoked so the gate can duck before anything is understood."""
    tr = Trace()
    vad = make_vad(tr)
    seen = []
    vad.on_onset = seen.append
    for i in range(5):
        vad.process_frame(voiced_frame(i * FRAME_N))
    assert len(seen) == 1


# --- Rime ---------------------------------------------------------------------------

def test_rime_refuses_to_run_unconfigured(monkeypatch):
    """No fallback TTS exists by design (RULES.md R9.4): unconfigured means silence, not substitution."""
    from aether.audio.rime import RimeNotConfigured, RimeTTS
    for k in ("RIME_API_KEY", "RIME_API_URL", "RIME_MODEL", "RIME_VOICE", "RIME_LANGUAGE"):
        monkeypatch.delenv(k, raising=False)
    rime = RimeTTS(Trace())
    assert not rime.configured
    assert set(rime.missing_config()) == {
        "RIME_API_KEY", "RIME_API_URL", "RIME_MODEL", "RIME_VOICE", "RIME_LANGUAGE",
    }
    with pytest.raises(RimeNotConfigured):
        rime.synthesize("hello", target_samplerate=48000)


def test_rime_resampling_preserves_duration():
    from aether.audio.rime import resample_int16
    src = np.zeros(24000, dtype=np.int16)
    out = resample_int16(src, 24000, 48000)
    assert len(out) == 48000
    assert out.dtype == np.int16
