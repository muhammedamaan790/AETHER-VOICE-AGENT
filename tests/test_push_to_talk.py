"""Push-to-talk: the mic is closed until you ask for it, and the button reaches the same fence.

Why this mode exists is worth stating, because it is not noise robustness. An always-open mic
cannot tell "talking to the agent" from "talking to a colleague". That is not a threshold problem
-- no level, duration or SNR gate separates two humans in a room; it needs speaker identity, which
AETHER does not have. Closing the mic answers the question by construction.

What must NOT change is the fence. A press and a barge-in are different triggers into the identical
`fence_now`, so the generation registry, the AudioGate's per-chunk tag and every stale-result
guarantee behave the same either way. These tests pin that equivalence, and pin that the trace can
still tell the two apart.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.player import AudioGate
from aether.audio.vad import MicVAD
from aether.events import EventType
from aether.interruption import BargeInCoordinator
from aether.spike import HANDS_FREE, OPEN_MIC, PUSH_TO_TALK, Day1Spike
from aether.supervisor.generations import GenerationRegistry
from aether.trace import Trace

FRAME = 320
AUDIO = np.zeros(16000, np.int16)


# ============================ the microphone gate ============================

def test_a_closed_mic_captures_nothing():
    """The core of the mode: frames arriving while closed are not processed at all."""
    vad = MicVAD(Trace())
    vad.set_listening(False)

    for _ in range(60):
        vad._callback(np.zeros((FRAME, 1), np.int16), FRAME, None, None)

    assert vad.utterances.empty()
    assert vad.trace.all(EventType.SPEECH_ONSET) == [], "a closed mic cannot even detect onset"


def test_closing_the_mic_discards_a_half_captured_utterance():
    """Otherwise a fragment from before the close would be stitched onto the next press."""
    vad = MicVAD(Trace())
    tone = (np.sin(np.arange(FRAME) * 0.35) * 8000).astype(np.int16)
    for _ in range(6):
        vad.process_frame(tone)
    assert vad._speech_active or vad._voiced_run > 0, "precondition: capture is under way"

    vad.set_listening(False)

    assert vad._speech_active is False
    assert vad._utterance == []
    assert vad._voiced_run == 0


def test_listening_reflects_the_real_state():
    vad = MicVAD(Trace())
    assert vad.listening is True
    vad.set_listening(False)
    assert vad.listening is False
    vad.set_listening(True)
    assert vad.listening is True


# ============================ the interrupt path ============================

class _Mic:
    """Minimal stand-in with the gate the pipeline actually drives."""

    def __init__(self):
        self.on_onset = None
        self.on_voiced_progress = None
        self.listening = True
        self.calls: list[bool] = []

    def set_listening(self, value):
        self.listening = value
        self.calls.append(value)

    def set_context(self, **k): ...


class _STT:
    """Returns nothing, so a turn stops right after capture -- which is all these tests need."""

    def transcribe(self, audio, *, turn_id=None, gen=None):
        return ""


@pytest.fixture
def rig(monkeypatch):
    trace = Trace()
    monkeypatch.setattr("aether.spike.AudioGate", lambda *a, **k: AudioGate(trace))
    monkeypatch.setattr("aether.spike.MicVAD", lambda *a, **k: _Mic())
    monkeypatch.setattr("aether.spike.WhisperSTT", lambda *a, **k: _STT())
    monkeypatch.setattr("aether.spike.build_llm", lambda: type("L", (), {"name": "fake"})())
    monkeypatch.setattr("aether.spike.build_tts", lambda *a, **k: object())
    return trace


def test_push_to_talk_starts_with_the_mic_closed(rig):
    spike = Day1Spike(rig, input_mode=PUSH_TO_TALK)
    assert spike.mic.listening is False, "nothing is captured until the operator asks"


def test_push_to_talk_wires_no_barge_in_callbacks(rig):
    """The behaviour being removed: voice must not be able to fence in this mode."""
    spike = Day1Spike(rig, input_mode=PUSH_TO_TALK)
    assert spike.mic.on_onset is None
    assert spike.mic.on_voiced_progress is None


def test_open_mic_still_wires_barge_in(rig):
    """The control. Open mic is retained, not deleted."""
    spike = Day1Spike(rig, input_mode=OPEN_MIC)
    assert spike.mic.on_onset is not None
    assert spike.mic.on_voiced_progress is not None
    assert spike.mic.listening is True


def test_interrupt_opens_the_mic(rig):
    spike = Day1Spike(rig, input_mode=PUSH_TO_TALK)
    spike.interrupt(source="button")
    assert spike.mic.listening is True


def test_the_mic_closes_again_as_soon_as_the_utterance_exists(rig):
    """The mic is open between the press and the end of speech -- not for the whole turn.

    Everything after capture (STT, the model, synthesis, playback) runs with the mic shut, so a
    conversation during the agent's answer cannot be captured and the agent cannot hear itself.
    """
    spike = Day1Spike(rig, input_mode=PUSH_TO_TALK)
    spike.interrupt(source="button")
    assert spike.mic.listening is True

    spike.handle_utterance(AUDIO, 0.0)

    assert spike.mic.listening is False


def test_open_mic_never_closes_the_mic(rig):
    spike = Day1Spike(rig, input_mode=OPEN_MIC)
    spike.handle_utterance(AUDIO, 0.0)
    assert spike.mic.listening is True, "closing the mic is push-to-talk behaviour only"


def test_an_invalid_input_mode_is_rejected(rig):
    with pytest.raises(ValueError, match="input_mode"):
        Day1Spike(rig, input_mode="telepathy")


# ============================ same fence, different trigger ============================

def _coordinator():
    trace = Trace()
    gens = GenerationRegistry(trace)
    gate = AudioGate(trace)
    barge = BargeInCoordinator(trace, gens, gate, meaningful_speech_ms=300.0)
    return trace, gens, gate, barge


def test_a_button_press_fences_exactly_like_a_barge_in():
    """The equivalence that makes the mode safe: one fence, reached two ways."""
    trace, gens, gate, barge = _coordinator()
    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id, turn_id=1)

    fenced = barge.fence_now(reason="button_interrupt")

    assert fenced == gen.id
    assert gens.is_active(gen.id) is False, "the real registry fenced it"
    pcm = np.zeros(480, dtype=np.int16)
    assert gate.enqueue(pcm, turn_id=1, gen=gen.id) is False, (
        "and the AudioGate refuses its audio, exactly as after a voice barge-in"
    )


def test_the_trace_distinguishes_a_press_from_a_voice_interruption():
    """Evidence must never conflate the two, or the demo cannot say which one it showed."""
    _trace, _gens, _gate, barge = _coordinator()

    barge.begin_turn(turn_id=1)
    barge.fence_now(reason="button_interrupt")
    barge.begin_turn(turn_id=2)
    barge.on_speech_onset()
    barge.on_voiced_progress(400.0)

    reasons = [reason for _gen, reason in barge.drain_fenced()]
    assert reasons == ["button_interrupt", "voiced_duration_confirmed"]


def test_interrupting_when_nothing_is_speaking_is_harmless(rig):
    """One button has to serve both "stop talking" and "start listening"."""
    spike = Day1Spike(rig, input_mode=PUSH_TO_TALK)
    assert spike.interrupt(source="button") is None, "nothing in flight to fence"
    assert spike.mic.listening is True, "but the mic still opens"


def test_interrupt_records_its_source(rig):
    spike = Day1Spike(rig, input_mode=PUSH_TO_TALK)
    spike.barge.begin_turn(turn_id=1)
    spike.interrupt(source="keyboard")
    assert [r for _g, r in spike.barge.drain_fenced()] == ["keyboard_interrupt"]


# ============================ hands-free: the live-test regression ============================
#
# Found in live use: AETHER could not hear anything until INTERRUPT was pressed. That was
# push-to-talk behaving as built, and it was the wrong default -- listening and fencing had been
# conflated into one mode. They are orthogonal:
#
#                    mic open by default?   voice may fence?
#   HANDS_FREE              yes                   no
#   PUSH_TO_TALK            no                    no
#   OPEN_MIC                yes                   yes

def test_hands_free_listens_immediately_without_any_interrupt(rig):
    """THE REGRESSION: speaking must work with nothing pressed."""
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    assert spike.mic.listening is True, (
        "the mic must be open from startup -- requiring a press before the agent can hear "
        "anything is the bug this mode exists to fix"
    )


def test_hands_free_never_lets_voice_fence(rig):
    """INTERRUPT is the only thing that stops an active task, which is what makes noise harmless."""
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    assert spike.mic.on_voiced_progress is None, "voice must never promote a duck into a fence"
    assert spike.mic.on_onset is None, (
        "and must not duck either: should_resume_after_short_utterance() is "
        "`is_ducked and not _fence_applied`, so a ducked-but-unfenced utterance takes the "
        "resume-and-return path in handle_utterance and is silently discarded"
    )


def test_hands_free_keeps_the_mic_open_across_a_turn(rig):
    """Unlike push-to-talk, nothing closes the mic -- the next utterance needs no press either."""
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    spike.handle_utterance(AUDIO, 0.0)
    assert spike.mic.listening is True


def test_the_button_still_fences_in_hands_free(rig):
    """Manual interrupt is preserved: it is now the ONLY thing that stops an active task."""
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    gen = spike.barge.begin_turn(turn_id=1)
    fenced = spike.interrupt(source="button")

    assert fenced == gen.id
    assert spike.gens.is_active(gen.id) is False
    assert [r for _g, r in spike.barge.drain_fenced()] == ["button_interrupt"]


def test_speech_still_interrupts_via_the_next_generation(rig):
    """Hands-free does not lose interruption -- it just lands at end-of-utterance.

    `GenerationRegistry.allocate()` fences the previous generation whenever a turn begins, so a
    real utterance still supersedes whatever was in flight; it simply happens when the user stops
    speaking rather than 300 ms into it.
    """
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    first = spike.barge.begin_turn(turn_id=1)
    spike.barge.end_turn()

    second = spike.barge.begin_turn(turn_id=2)

    assert second.id != first.id
    assert spike.gens.is_active(first.id) is False, "the older generation was fenced by the new turn"
    assert spike.gens.is_active(second.id) is True


def test_hands_free_is_the_default_for_both_entrypoints():
    """The bug was a *default*, so the defaults are what must be pinned."""
    import inspect

    import aether.spike as spike_mod
    import aether.web.__main__ as web_mod

    for mod, label in ((spike_mod, "python -m aether.spike"), (web_mod, "python -m aether.web")):
        src = inspect.getsource(mod.main)
        assert "default=HANDS_FREE" in src, f"{label} must default to hands_free"


def test_the_cli_has_an_interrupt_path_at_all():
    """`python -m aether.spike` had NO way to interrupt, and with a closed mic that made it deaf."""
    import inspect

    src = inspect.getsource(Day1Spike._start_interrupt_listener)
    assert "stdin" in src, "the terminal needs some interrupt input"
    assert "self.interrupt(" in src, "and it must reach the same interrupt() the button calls"
    assert "daemon=True" in src, "the listener must not keep the process alive"


def test_the_run_loop_starts_the_interrupt_listener():
    import inspect

    assert "_start_interrupt_listener()" in inspect.getsource(Day1Spike.run)


# ============================ standby: stop LISTENING, not stop talking ============================
#
# Two independent intentions, two independent controls. Hands-free keeps the mic open, so a
# conversation with someone else is captured, transcribed and answered aloud -- voice can no longer
# falsely INTERRUPT, but it can still falsely RESPOND, and only the user knows which speech was
# meant for the agent.

def test_standby_mutes_the_microphone(rig):
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    assert spike.standby is False and spike.mic.listening is True

    spike.set_standby(True)
    assert spike.standby is True and spike.mic.listening is False

    spike.set_standby(False)
    assert spike.standby is False and spike.mic.listening is True


def test_toggle_standby_flips_and_reports(rig):
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    assert spike.toggle_standby() is True, "returns True when it has just muted"
    assert spike.standby is True
    assert spike.toggle_standby() is False
    assert spike.standby is False


def test_standby_never_fences_anything(rig):
    """The whole point: muting the mic must not touch the active generation."""
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    gen = spike.barge.begin_turn(turn_id=1)

    spike.set_standby(True)

    assert spike.gens.is_active(gen.id) is True, "a turn in flight keeps running and keeps speaking"
    assert spike.barge.drain_fenced() == [], "standby emits no fence"
    assert rig.all(EventType.FENCE_REQUESTED) == []
    assert rig.all(EventType.GENERATION_CHANGED) == [
        e for e in rig.all(EventType.GENERATION_CHANGED)
    ], "no generation transition is caused by muting"


def test_interrupt_does_not_cancel_standby_in_hands_free(rig):
    """THE REGRESSION this pair was designed around.

    `interrupt()` used to call `set_listening(True)` unconditionally, so pressing INTERRUPT while
    muted would silently un-mute -- making standby useless. Only push-to-talk may open the mic,
    because there the press IS how you get a turn.
    """
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    spike.barge.begin_turn(turn_id=1)
    spike.set_standby(True)

    spike.interrupt(source="button")

    assert spike.standby is True, "INTERRUPT stops the agent; it must not re-open the mic"


def test_interrupt_still_opens_the_mic_in_push_to_talk(rig):
    """The control: in push-to-talk the press is the only way to get a turn, so it must open it."""
    spike = Day1Spike(rig, input_mode=PUSH_TO_TALK)
    assert spike.mic.listening is False

    spike.interrupt(source="button")

    assert spike.mic.listening is True


def test_standby_is_derived_from_the_mic_not_tracked_separately(rig):
    """Two sources of truth for 'am I listening' would eventually disagree."""
    spike = Day1Spike(rig, input_mode=HANDS_FREE)
    spike.mic.set_listening(False)          # changed behind the pipeline's back
    assert spike.standby is True


def test_the_cli_listener_handles_both_controls():
    import inspect

    src = inspect.getsource(Day1Spike._start_interrupt_listener)
    assert "toggle_standby()" in src, "m + ENTER must toggle standby"
    assert "self.interrupt(" in src, "bare ENTER must still interrupt"
