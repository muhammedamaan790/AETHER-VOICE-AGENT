"""The listening toggle and INTERRUPT: two controls, two meanings, never the same mechanism.

The bug this whole control set exists to prevent is conflation. Push-to-talk once closed the
microphone in order to stop noise fencing turns, and the side effect was that the user had to press
INTERRUPT before AETHER could hear them at all. So every test here asserts BOTH halves: that the
control did what it says, and that it did not do the other thing.

    START / STOP LISTENING   changes whether audio is processed.  Fences nothing.
    INTERRUPT                fences.                              Closes nothing.

The toggle is verified through the real bridge -- `InboundBridge.push` feeding a real `MicVAD` --
rather than through a flag, because "the button changed a boolean" is not the claim being made.
The claim is that audio genuinely stops reaching the VAD and the transport stays up.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.bridge import AudioChunk, InboundBridge, OutboundBridge
from aether.events import EventType
from aether.trace import Trace

AUDIO = np.zeros(16000, np.int16)


# ============================ the engine control ============================

class _Mic:
    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None
        self.listening = True
        self.samplerate = 16000
        self.frame_samples = 320           # 20 ms at 16 kHz, as MicVAD uses
        self.resets = 0

    def set_listening(self, value):
        if self.listening and not value:
            self.resets += 1          # MicVAD resets detector state on the closing edge
        self.listening = bool(value)

    def set_context(self, **k): ...

    def speech_floor(self):
        return 35.0


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
        return "Certainly."


@pytest.fixture
def spike(monkeypatch):
    from aether.audio.player import AudioGate
    from aether.spike import HANDS_FREE, Day1Spike

    trace = Trace()
    rime = _Rime()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: AudioGate(trace))
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _Mic())
    monkeypatch.setattr("aether.spike.WhisperSTT",
                        lambda *a, **k: _STT("what starters do you have", "what desserts do you have"))
    monkeypatch.setattr("aether.spike.build_llm", lambda: _LLM())
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: rime)
    engine = Day1Spike(trace, input_mode=HANDS_FREE)
    engine.rime = rime
    monkeypatch.setattr(engine, "_streaming_enabled", False)
    engine._trace_for_test = trace
    return engine


def fences(trace) -> list:
    return trace.all(EventType.FENCE_REQUESTED)


# --- START ---------------------------------------------------------------------------

def test_start_listening_opens_the_microphone(spike):
    spike.stop_listening()
    assert spike.listening is False
    assert spike.start_listening() is True
    assert spike.listening is True


def test_start_listening_is_idempotent(spike):
    assert spike.start_listening() is True
    assert spike.start_listening() is True, "pressing it twice is not an error"


# --- STOP ----------------------------------------------------------------------------

def test_stop_listening_closes_the_microphone(spike):
    assert spike.stop_listening() is False
    assert spike.listening is False


def test_stop_while_idle_is_a_safe_no_op(spike):
    trace = spike._trace_for_test
    spike.stop_listening()
    assert fences(trace) == []
    assert spike.gens.active is None
    assert trace.all(EventType.GENERATION_CHANGED) == []


def test_stop_listening_while_aether_is_speaking_does_not_fence_it(spike):
    """THE CENTRAL CLAIM. Stopping listening is not a hangup and not an interrupt."""
    trace = spike._trace_for_test
    spike.handle_utterance(AUDIO, 0.0)
    speaking = spike.gens.active.id
    assert spike.gate.is_playing

    spike.stop_listening()

    assert spike.gens.active.id == speaking, "the generation is untouched"
    assert spike.gens.get(speaking).status.value == "active"
    assert fences(trace) == [], "no fence was requested"
    assert spike.gate.is_playing, "the answer keeps playing"
    assert spike.listening is False


def test_the_toggle_never_allocates_or_fences_a_generation(spike):
    trace = spike._trace_for_test
    before = len(trace.all(EventType.GENERATION_CHANGED))
    for _ in range(4):
        spike.toggle_listening()
    assert len(trace.all(EventType.GENERATION_CHANGED)) == before
    assert fences(trace) == []


# --- START after STOP ----------------------------------------------------------------

def test_start_after_stop_restores_listening(spike):
    spike.stop_listening()
    spike.start_listening()
    assert spike.listening is True


def test_start_after_an_interrupted_response_works_immediately(spike):
    """After INTERRUPT the caller must be able to speak at once, with no extra press."""
    spike.handle_utterance(AUDIO, 0.0)
    spike.interrupt(source="button")
    assert spike.listening is True, "INTERRUPT must never close the microphone"
    spike.stop_listening()
    spike.start_listening()
    assert spike.listening is True

    spike.handle_utterance(AUDIO, 0.0)          # the next question is answered normally
    assert "gulab jamun" in spike.rime.spoken[-1]


def test_the_toggle_reports_the_state_it_reached_not_the_one_requested(spike):
    """The UI label is derived from this return value, so it must be the engine's truth."""
    assert spike.set_listening(False) is False
    assert spike.set_listening(True) is True
    assert spike.toggle_listening() is False
    assert spike.toggle_listening() is True


def test_listening_and_standby_are_one_flag_seen_from_two_ends(spike):
    """Two names, one microphone. A second stored state could fall out of step with the first."""
    spike.stop_listening()
    assert spike.standby is True
    spike.set_standby(False)
    assert spike.listening is True


# ============================ through the real bridge ============================

def _blocks(n: int, rate: int = 48000, amplitude: int = 6000) -> list[AudioChunk]:
    """Loud, obviously non-silent transport audio."""
    rng = np.random.default_rng(7)
    return [AudioChunk(data=(rng.normal(0, amplitude, 960)).astype(np.int16), sample_rate=rate)
            for _ in range(n)]


def test_stop_listening_really_stops_audio_reaching_the_vad(spike):
    """Not a flag test. The frames genuinely stop arriving at `MicVAD.process_frame`."""
    bridge = InboundBridge(spike.mic, source_rate=48000)
    seen: list = []
    spike.mic.process_frame = seen.append

    for chunk in _blocks(6):
        bridge.push(chunk)
    delivered = len(seen)
    assert delivered > 0, "audio reaches the VAD while listening"

    spike.stop_listening()
    for chunk in _blocks(6):
        bridge.push(chunk)
    assert len(seen) == delivered, "not one frame reaches the VAD after STOP LISTENING"
    assert bridge.frames_dropped_not_listening == 6

    spike.start_listening()
    for chunk in _blocks(6):
        bridge.push(chunk)
    assert len(seen) > delivered, "and it resumes on START LISTENING"


def test_stopping_listening_leaves_the_outbound_transport_running(spike):
    """The call stays up. Silence is published, not skipped -- a gap would be heard as a dropout."""
    outbound = OutboundBridge(spike.gate, sink_rate=48000)
    spike.stop_listening()
    chunks = [outbound.pull() for _ in range(5)]
    assert len(chunks) == 5
    assert all(c.sample_rate == 48000 and c.samples > 0 for c in chunks)
    assert outbound.blocks_pulled == 5


def test_audio_from_before_a_stop_is_not_stitched_onto_audio_after_it(spike):
    """A half-captured utterance must not survive the mute."""
    bridge = InboundBridge(spike.mic, source_rate=48000)
    bridge.push(AudioChunk(data=np.ones(500, dtype=np.int16), sample_rate=48000))
    assert len(bridge._carry) > 0

    spike.stop_listening()
    bridge.push(AudioChunk(data=np.ones(500, dtype=np.int16), sample_rate=48000))
    assert len(bridge._carry) == 0


# ============================ INTERRUPT ============================

def test_interrupt_fences_and_leaves_listening_alone(spike):
    trace = spike._trace_for_test
    spike.handle_utterance(AUDIO, 0.0)
    speaking = spike.gens.active.id

    fenced = spike.interrupt(source="button")

    assert fenced == speaking
    assert spike.gens.get(speaking).status.value == "fenced"
    assert spike.listening is True, "INTERRUPT must never close the microphone"


def test_interrupt_with_nothing_running_is_a_safe_no_op(spike):
    assert spike.interrupt(source="button") is None
    assert spike.listening is True


def test_interrupt_does_not_permanently_disable_listening(spike):
    for _ in range(3):
        spike.interrupt(source="button")
    assert spike.listening is True


def test_manual_interrupt_and_natural_barge_in_reach_the_same_fence(spike):
    """One fence, two triggers, distinguished only by `reason` -- which is the evidence."""
    trace = spike._trace_for_test
    spike.handle_utterance(AUDIO, 0.0)
    spike.interrupt(source="button")
    spike.barge.fence_now(reason="voiced_duration_confirmed")

    reasons = [reason for _gen, reason in spike.barge.drain_fenced()]
    assert reasons == ["button_interrupt", "voiced_duration_confirmed"]
    assert trace.all(EventType.RESULT_LEAKED) == []


def test_a_stale_result_cannot_speak_after_an_interrupt(spike):
    """The golden invariant, driven through the button rather than through voice."""
    trace = spike._trace_for_test
    spike.handle_utterance(AUDIO, 0.0)
    spoken_before = len(spike.rime.spoken)

    fenced = spike.interrupt(source="button")
    # Anything still holding that generation is now refused by the gate.
    assert spike.gate.enqueue(np.ones(160, dtype=np.int16), gen=fenced) is False
    assert len(spike.rime.spoken) == spoken_before
    assert trace.all(EventType.RESULT_LEAKED) == []


def test_interrupt_then_an_immediate_new_request_is_answered(spike):
    spike.handle_utterance(AUDIO, 0.0)
    first = spike.gens.active.id
    spike.interrupt(source="button")
    spike.handle_utterance(AUDIO, 0.0)

    assert spike.gens.active.id != first
    assert spike.gens.get(first).status.value == "fenced"
    assert "gulab jamun" in spike.rime.spoken[-1]
    assert spike._trace_for_test.all(EventType.RESULT_LEAKED) == []


# ============================ the browser end ============================

def test_the_bridge_sends_a_desired_state_not_a_flip():
    """Two fast clicks must converge, not cancel out."""
    from aether.web.server import WebBridge

    seen: list = []
    bridge = WebBridge(on_listening=seen.append)
    bridge._handle_inbound('{"action":"listening","on":false}')
    bridge._handle_inbound('{"action":"listening","on":false}')
    assert seen == [False, False], "idempotent: the second click confirms, it does not undo"


def test_the_listening_action_never_reaches_the_interrupt_callback():
    from aether.web.server import WebBridge

    interrupts, listens = [], []
    bridge = WebBridge(on_interrupt=lambda: interrupts.append(1), on_listening=listens.append)
    bridge._handle_inbound('{"action":"listening","on":false}')
    assert (interrupts, listens) == ([], [False])
    bridge._handle_inbound('{"action":"interrupt"}')
    assert (interrupts, listens) == ([1], [False]), "interrupt must not touch listening"


def test_a_malformed_listening_message_never_silently_mutes_the_caller():
    from aether.web.server import WebBridge

    seen: list = []
    bridge = WebBridge(on_listening=seen.append)
    for payload in ['{"action":"listening"}', '{"action":"listening","on":"yes"}',
                    '{"action":"listening","on":1}']:
        bridge._handle_inbound(payload)
    assert seen == [True, True, True], "anything that is not an explicit false means start"


def test_a_failing_listening_control_does_not_break_the_socket_loop():
    from aether.web.server import WebBridge

    def explodes(_on):
        raise RuntimeError("mic gone")

    WebBridge(on_listening=explodes)._handle_inbound('{"action":"listening","on":true}')


def test_the_snapshot_says_whether_anything_is_interruptible():
    from aether.web.server import WebBridge

    for phase, expected in (("listening", False), ("thinking", True), ("speaking", True),
                            ("interrupted", False)):
        bridge = WebBridge(phase_source=lambda p=phase: p)
        assert bridge.current_state()["interruptible"] is expected
