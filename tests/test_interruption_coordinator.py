"""The interruption/recovery coordinator in isolation.

`tests/test_interruption_recovery.py` proves the behaviour end-to-end through the pipeline. This
file pins the coordinator's own contract -- the parts that are easy to break silently:

  * it owns no generation counter and no fencing rule, it delegates both
  * `Phase` is derived from existing state, never a competing source of truth
  * the realtime callbacks stay realtime-safe (no trace IO on the audio thread)

The real `GenerationRegistry` and `AudioGate` are used, because delegation is the thing under test
-- a fake would only prove the fake was called.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.player import AudioGate
from aether.events import EventType, GenerationStatus
from aether.interruption import BargeInCoordinator, Phase
from aether.supervisor.generations import GenerationRegistry
from aether.trace import Trace

MEANINGFUL = 300.0


def pump(gate: AudioGate, blocks: int = 3) -> None:
    """Run the real output callback, as the PortAudio thread does continuously.

    Duck and stop are *requested* on one thread and *applied* in the callback, so without running
    it `is_ducked` never becomes true and a requested stop stays pending forever.
    """
    for _ in range(blocks):
        buf = np.zeros((gate.blocksize, 1), dtype=np.int16)
        gate._callback(buf, gate.blocksize, None, None)


@pytest.fixture
def rig():
    trace = Trace()
    gens = GenerationRegistry(trace)
    gate = AudioGate(trace)            # real gate; its stream is never started
    barge = BargeInCoordinator(trace, gens, gate, meaningful_speech_ms=MEANINGFUL)
    return barge, gens, gate, trace


def queue_audio(gate: AudioGate, gen: str, samples: int = 480) -> bool:
    return gate.enqueue(np.full(samples, 1000, dtype=np.int16), gen=gen)


# --- delegation: no second source of truth ------------------------------------------------

def test_begin_turn_delegates_to_the_existing_registry(rig):
    """No second generation counter: ids come from GenerationRegistry alone."""
    barge, gens, _, _ = rig

    g1 = barge.begin_turn(turn_id=1)
    barge.end_turn()
    g2 = barge.begin_turn(turn_id=2)

    assert (g1.id, g2.id) == ("G1", "G2")
    assert gens.active is g2
    assert not hasattr(barge, "_counter") and not hasattr(barge, "_next_gen")


def test_is_valid_is_the_registry_answer_not_a_local_copy(rig):
    barge, gens, _, _ = rig
    gen = barge.begin_turn(turn_id=1)

    assert barge.is_valid(gen.id) is True
    gens.mark_fenced(gen.id)                       # fenced behind the coordinator's back
    assert barge.is_valid(gen.id) is False, "the registry must remain the single answer"


def test_fence_now_marks_the_registry_and_revokes_the_gate(rig):
    barge, gens, gate, _ = rig
    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id)
    assert queue_audio(gate, gen.id) is True

    fenced = barge.fence_now(reason="test")

    assert fenced == gen.id
    assert gens.get(gen.id).status is GenerationStatus.FENCED
    assert queue_audio(gate, gen.id) is False, "the gate must refuse the fenced generation"


def test_the_coordinator_does_not_speak_the_rime_protocol():
    """Rime clearing stays in the streaming client, driven by the is_valid callback."""
    import inspect

    import aether.interruption as mod

    src = inspect.getsource(mod)
    # Strip the module docstring and comments: they explain the contract by quoting it.
    body = src.split('"""', 2)[-1]
    code = " ".join(line.split("#", 1)[0] for line in body.splitlines())
    assert '"operation"' not in code, "the Rime wire protocol must stay in the streaming client"
    assert "websocket" not in code.lower() and "ws.send" not in code


# --- realtime safety ------------------------------------------------------------------------

def test_the_audio_thread_callbacks_emit_no_trace_events(rig):
    """Trace writes do file IO; they must never happen on the PortAudio input thread."""
    barge, _, gate, trace = rig
    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id)
    queue_audio(gate, gen.id)
    before = len(trace.events)

    barge.on_speech_onset()
    barge.on_voiced_progress(MEANINGFUL)

    assert len(trace.events) == before, "no event may be emitted from the audio thread"
    assert barge.drain_fenced() == [gen.id], "it is deferred to the main thread instead"


def test_drain_fenced_is_emptied_by_reading(rig):
    barge, _, _, _ = rig
    barge.begin_turn(turn_id=1)
    barge.fence_now(reason="test")

    assert barge.drain_fenced() == ["G1"]
    assert barge.drain_fenced() == [], "a fence is reported once, not on every turn"


def test_rapid_consecutive_interruptions_are_each_recorded(rig):
    barge, _, _, _ = rig
    barge.begin_turn(turn_id=1); barge.fence_now(reason="one"); barge.end_turn()
    barge.begin_turn(turn_id=2); barge.fence_now(reason="two"); barge.end_turn()

    assert barge.drain_fenced() == ["G1", "G2"], "neither may overwrite the other"


# --- arming rules ----------------------------------------------------------------------------

def test_speaking_to_an_idle_agent_arms_nothing(rig):
    barge, _, _, _ = rig

    barge.on_speech_onset()
    barge.on_voiced_progress(MEANINGFUL * 3)

    assert barge.armed is False
    assert barge.drain_fenced() == [], "there is nothing to interrupt"


def test_a_turn_in_flight_arms_even_with_no_audio(rig):
    """The regression this subdomain exists for: interrupting while the LLM thinks."""
    barge, gens, gate, _ = rig
    gen = barge.begin_turn(turn_id=1)
    assert gate.is_playing is False

    barge.on_speech_onset()
    assert barge.armed is True, "arming must not require audio"
    barge.on_voiced_progress(MEANINGFUL)

    assert gens.get(gen.id).status is GenerationStatus.FENCED


def test_a_short_utterance_ducks_but_does_not_fence(rig):
    barge, gens, gate, _ = rig
    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id)
    queue_audio(gate, gen.id)

    barge.on_speech_onset()
    pump(gate)                                    # the audio thread applies the duck
    barge.on_voiced_progress(MEANINGFUL - 1)

    assert barge.fence_applied is False
    assert gens.get(gen.id).status is GenerationStatus.ACTIVE
    assert barge.should_resume_after_short_utterance() is True


def test_the_fence_is_applied_once_per_barge_in(rig):
    barge, _, gate, _ = rig
    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id)
    queue_audio(gate, gen.id)

    barge.on_speech_onset()
    for ms in (MEANINGFUL, MEANINGFUL + 100, MEANINGFUL + 900):
        barge.on_voiced_progress(ms)

    assert barge.drain_fenced() == [gen.id], "continued speech must not fence repeatedly"


def test_end_turn_disarms_so_a_finished_turn_is_not_fenced_later(rig):
    barge, gens, _, _ = rig
    gen = barge.begin_turn(turn_id=1)
    barge.end_turn()

    barge.on_speech_onset()
    barge.on_voiced_progress(MEANINGFUL)

    assert barge.drain_fenced() == []
    assert gens.get(gen.id).status is GenerationStatus.ACTIVE


# --- Phase is derived, not authoritative -------------------------------------------------------

def test_phase_listening_when_idle(rig):
    barge, _, _, _ = rig
    assert barge.phase is Phase.LISTENING


def test_phase_thinking_when_a_turn_is_in_flight_with_no_audio(rig):
    barge, _, _, _ = rig
    barge.begin_turn(turn_id=1)
    assert barge.phase is Phase.THINKING


def test_phase_speaking_once_audio_is_queued(rig):
    barge, _, gate, _ = rig
    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id)
    queue_audio(gate, gen.id)
    assert barge.phase is Phase.SPEAKING


def test_phase_interrupted_then_recovering_then_listening(rig):
    barge, _, gate, _ = rig
    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id)
    queue_audio(gate, gen.id)

    barge.on_speech_onset()
    barge.on_voiced_progress(MEANINGFUL)
    assert barge.phase is Phase.RECOVERING, "a pending fence dominates: recovery is owed"

    barge.drain_fenced()
    assert barge.phase is Phase.INTERRUPTED, "still tearing the fenced turn down"

    barge.end_turn()
    assert barge.phase is Phase.LISTENING


def test_phase_never_contradicts_the_registry(rig):
    """Phase is a view. If it ever disagreed with the registry, the registry is right."""
    barge, gens, _, _ = rig
    gen = barge.begin_turn(turn_id=1)
    barge.fence_now(reason="test")
    barge.drain_fenced()

    assert barge.phase is Phase.INTERRUPTED
    assert gens.is_active(gen.id) is False, "the authoritative answer, and it agrees"


def test_recovery_produces_a_usable_new_generation(rig):
    barge, gens, gate, _ = rig
    old = barge.begin_turn(turn_id=1)
    gate.set_active_generation(old.id)
    barge.fence_now(reason="interrupted")
    barge.end_turn()
    barge.drain_fenced()                          # the pipeline reports fences, then starts the turn

    new = barge.begin_turn(turn_id=2)
    gate.set_active_generation(new.id)

    assert new.id == "G2"
    assert barge.is_valid(new.id) is True
    assert queue_audio(gate, new.id) is True, "the new generation must be able to speak"
    assert queue_audio(gate, old.id) is False, "the old one must not"
    # The test just queued G2 audio, so the agent genuinely is speaking again.
    assert barge.phase is Phase.SPEAKING, "recovery ends in a normal speaking turn"
