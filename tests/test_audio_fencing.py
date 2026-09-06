"""The golden invariant applied to streaming audio.

RULES.md R1: a stale result must never become spoken output. For audio that means a fenced
generation's samples must never reach the speaker -- not the chunks already queued, and not the
chunks still in flight from the synthesiser when the fence lands.

These tests force an interruption mid-playback and assert from the trace that the old generation's
audio was CUT, not drained.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.player import AudioGate
from aether.events import EventType
from aether.trace import Trace


def tone(gate: AudioGate, seconds: float) -> np.ndarray:
    t = np.arange(int(gate.samplerate * seconds)) / gate.samplerate
    return (np.sin(2 * np.pi * 220.0 * t) * 0.2 * 32767).astype(np.int16)


@pytest.fixture
def gate():
    tr = Trace()
    g = AudioGate(tr)
    g.open()
    yield g, tr
    g.close()


# --- the enqueue-after-fence race (the bug that survives a bare stop) -----------------

@pytest.mark.audio
def test_audio_synthesised_for_a_fenced_generation_is_refused(gate):
    """The streaming bug in miniature: TTS returns AFTER the fence. It must not play."""
    g, tr = gate
    g.set_active_generation("G1")
    g.fence_generation("G1", reason="interruption")

    accepted = g.enqueue(tone(g, 3.0), gen="G1")

    assert accepted is False, "audio for a fenced generation must be refused"
    assert not g.is_playing, "refused audio must not be queued"
    discarded = tr.first(EventType.RESULT_DISCARDED)
    assert discarded is not None
    assert discarded.gen == "G1"
    assert discarded.fields["reason"] == "stale_generation"


@pytest.mark.audio
def test_fence_flushes_audio_already_queued(gate):
    """Clearing the synthesiser is not enough -- already-decoded audio must be dropped too."""
    g, tr = gate
    g.set_active_generation("G1")
    assert g.enqueue(tone(g, 10.0), gen="G1") is True
    assert g.is_playing

    g.fence_generation("G1", reason="interruption")
    g._emitted.wait(1.0)

    assert not g.is_playing, "fence must flush already-queued audio, not let it drain"
    assert tr.first(EventType.AUDIO_STOPPED) is not None


@pytest.mark.audio
def test_in_flight_chunk_for_fenced_generation_is_dropped_by_the_callback(gate):
    """Defence in depth: a chunk that got queued while G1 was active must still not play
    once the active generation has moved on."""
    g, tr = gate
    g.set_active_generation("G1")
    g.enqueue(tone(g, 5.0), gen="G1")

    # Generation moves on without a flush -- the callback alone must refuse the old chunks.
    g.set_active_generation("G2")
    deadline = 1.0
    while deadline > 0 and g.is_playing:
        import time
        time.sleep(0.02)
        deadline -= 0.02

    assert not g.is_playing, "callback must drop chunks whose generation is no longer active"
    drops = [e for e in tr.all(EventType.RESULT_DISCARDED)
             if e.fields.get("reason") == "stale_audio_chunk"]
    assert drops, "dropped chunks must be recorded as ResultDiscarded"
    assert drops[0].gen == "G1"


# --- the headline assertion -----------------------------------------------------------

@pytest.mark.audio
def test_interruption_midplayback_cuts_old_generation_and_never_speaks_it_again(gate):
    """Force an interruption mid-playback; assert the old generation is cut, not drained.

    Asserts, from the trace alone:
      - AudioStopped for the old generation exists
      - the old generation emits ResultDiscarded
      - NO ResponseSpoken for the old generation occurs after the fence
      - the new generation's audio plays only after the old one was stopped
    """
    g, tr = gate

    # G1 speaking: 10 s of audio committed, ResponseSpoken emitted at commit time.
    g.set_active_generation("G1", turn_id=1)
    assert g.enqueue(tone(g, 10.0), turn_id=1, gen="G1") is True
    tr.emit(EventType.RESPONSE_SPOKEN, turn_id=1, gen="G1", provider="rime", text="old answer")

    import time
    time.sleep(0.25)  # let some of G1 actually play
    assert g.is_playing

    # Interruption confirmed -> fence G1.
    tr.emit(EventType.FENCE_REQUESTED, gen="G1", reason="meaningful_interruption")
    g.fence_generation("G1", reason="meaningful_interruption")
    g._emitted.wait(1.0)
    stopped = tr.first(EventType.AUDIO_STOPPED)
    assert stopped is not None, "the fence must stop audio"
    assert not g.is_playing, "old generation's audio must be cut, not left to drain"

    # G1's synthesiser result arrives late -- the classic stale result.
    assert g.enqueue(tone(g, 3.0), turn_id=1, gen="G1") is False

    # G2 takes over and speaks.
    g.set_active_generation("G2", turn_id=2)
    assert g.enqueue(tone(g, 0.5), turn_id=2, gen="G2") is True
    tr.emit(EventType.RESPONSE_SPOKEN, turn_id=2, gen="G2", provider="rime", text="new answer")

    # --- assertions from the trace alone ---
    fence = tr.first(EventType.FENCE_REQUESTED)
    g1_discards = [e for e in tr.all(EventType.RESULT_DISCARDED) if e.gen == "G1"]
    assert g1_discards, "the fenced generation must record ResultDiscarded"

    g1_spoken_after_fence = [
        e for e in tr.all(EventType.RESPONSE_SPOKEN) if e.gen == "G1" and e.seq > fence.seq
    ]
    assert g1_spoken_after_fence == [], "a fenced generation must never speak again"

    assert stopped.seq > fence.seq, "AudioStopped must follow the fence"
    g2_spoken = tr.first(EventType.RESPONSE_SPOKEN)
    g2_spoken = [e for e in tr.all(EventType.RESPONSE_SPOKEN) if e.gen == "G2"][0]
    assert g2_spoken.seq > stopped.seq, "the new generation may only speak after the old was cut"

    assert tr.all(EventType.RESULT_LEAKED) == [], "no leak may occur in safe mode"


@pytest.mark.audio
def test_active_generation_audio_is_accepted_normally(gate):
    """The guard must not break the ordinary path."""
    g, tr = gate
    g.set_active_generation("G1")
    assert g.enqueue(tone(g, 0.2), gen="G1") is True
    assert tr.all(EventType.RESULT_DISCARDED) == []


def test_untagged_audio_still_plays(gate=None):
    """Chunks with gen=None (test tones, harness audio) bypass the check by design."""
    tr = Trace()
    g = AudioGate(tr)
    assert g.enqueue(np.zeros(100, dtype=np.int16), gen=None) is True
