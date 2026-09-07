"""Adverse-audio sweep: does the VAD gate actually behave the way its constants claim?

`tests/test_reliability_and_noise.py` already pins the point cases -- a blip is rejected, a distant
voice is rejected, a loud voice in a loud room is accepted. Those prove the gate *can* do the right
thing at three chosen levels. They cannot tell you where the boundary is, whether it moves, or
whether it is even monotonic, and one of them carries a `pytest.skip` for when the synthetic levels
fail to separate -- so a regression there could hide rather than fail.

This file sweeps instead of sampling. Two external reviews (a Rasa/Rime demo and a LiveKit
telephony playbook) independently treated adverse-audio evaluation as a first-class artifact, and
AETHER had none: `min_speech_ms = 250.0` and `noise_snr_margin = 3.0` in `aether/audio/vad.py` were
hand-set and never validated across a range.

What a sweep buys that point cases do not:

* **Monotonicity.** Louder speech must never be *more* likely to be rejected than quieter speech.
  An inversion means the gate is not measuring what it thinks it is, and no single point case can
  detect one.
* **A located boundary.** The tests below assert where the crossover sits relative to the
  configured constant, so retuning `noise_snr_margin` without updating the reasoning fails loudly.
* **No skips.** Every case asserts; nothing opts out when the synthetic levels are inconvenient.

Offline and deterministic: frames are fed straight to `MicVAD.process_frame`, the same entry point
the live callback uses, with seeded noise. No microphone, no PortAudio input, no wall-clock timing.

It measures the *gate*, not the room. Synthetic harmonics mixed with Gaussian noise are not speech
in a real warehouse, and this says nothing about echo, reverb, or the agent's own output bleeding
into the mic (MEMORY.md §7 -- there is no AEC). It answers one question: given a measured ambient
level and a speech level, does the gate decide consistently and in the direction it claims?
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.vad import MicVAD
from aether.events import EventType
from aether.trace import Trace

SR = 16000
FRAME = 320          # 20 ms, matching the live configuration
SILENCE = np.zeros(FRAME, dtype=np.int16)

# Long enough to clear `min_speech_ms` comfortably, so SNR is the only variable under test.
SPEECH_FRAMES = 40   # 800 ms
ROOM_FRAMES = 30     # enough for the ambient tracker to settle


def voiced_frame(offset: int, amp: float, f0: float = 140.0) -> np.ndarray:
    """A voiced frame webrtcvad reliably flags, at a controllable amplitude.

    Same generator as tests/test_reliability_and_noise.py so the two files describe the same
    synthetic speech and a number from one is comparable with the other.
    """
    t = (np.arange(FRAME) + offset) / SR
    sig = np.zeros(FRAME)
    for k, a in [(1, 1.0), (2, 0.6), (3, 0.45), (4, 0.3), (5, 0.2), (8, 0.12), (12, 0.08)]:
        sig += a * np.sin(2 * np.pi * f0 * k * t)
    sig += 0.05 * np.random.default_rng(offset).normal(0, 1, FRAME)
    sig = sig / np.abs(sig).max() * amp
    return (sig * 32767).astype(np.int16)


def room_frames(sigma: float, n: int = ROOM_FRAMES, seed: int = 7) -> list[np.ndarray]:
    """Steady Gaussian room tone. Seeded, so a failure reproduces exactly."""
    rng = np.random.default_rng(seed)
    return list((rng.normal(0, sigma, (n, FRAME)) * 32767).astype(np.int16))


def run_case(room_sigma: float, speech_amp: float) -> dict:
    """Feed room tone then speech, and report what the gate decided and why.

    Returns the decision plus the two levels the gate actually measured, so a failing assertion
    shows the evidence behind the decision rather than just the verdict.
    """
    vad = MicVAD(Trace())
    for f in room_frames(room_sigma):
        vad.process_frame(f)
    for i in range(SPEECH_FRAMES):
        vad.process_frame(voiced_frame(i * FRAME, amp=speech_amp))
    for _ in range(40):                      # clear webrtcvad's ~6-frame hangover
        vad.process_frame(SILENCE)

    ended = vad.trace.last(EventType.SPEECH_ENDED)
    return {
        "accepted": not vad.utterances.empty(),
        "rejected": ended.fields["rejected"] if ended else "no_utterance",
        "speech_rms": ended.fields["speech_rms"] if ended else None,
        "ambient_rms": ended.fields["ambient_rms"] if ended else None,
        "voiced_ms": ended.fields["voiced_ms"] if ended else None,
    }


# ============================ SNR sweep ============================

# Speech amplitudes from "barely above the room" to "clearly close-range", in a fixed room.
AMPLITUDES = [0.010, 0.015, 0.020, 0.030, 0.045, 0.070, 0.100, 0.200, 0.350, 0.500]
ROOM_SIGMA = 0.02


@pytest.mark.parametrize("amp", AMPLITUDES)
def test_every_snr_case_reaches_a_decision(amp):
    """No case may be indeterminate: the gate either accepts or names its reason."""
    outcome = run_case(ROOM_SIGMA, amp)
    assert outcome["accepted"] or outcome["rejected"] in {
        "too_short", "below_noise_floor", "no_utterance",
    }, f"unclassified outcome at amp={amp}: {outcome}"


def test_acceptance_is_monotonic_in_speech_level():
    """The property no single point case can prove: louder is never *less* acceptable.

    An inversion would mean the gate is keying on something other than level, which is exactly the
    failure a hand-tuned constant hides until it meets a real room.
    """
    results = [(amp, run_case(ROOM_SIGMA, amp)) for amp in AMPLITUDES]
    accepted = [r["accepted"] for _amp, r in results]

    first_accept = next((i for i, a in enumerate(accepted) if a), None)
    assert first_accept is not None, (
        f"nothing was accepted at any level; the gate is closed. {[(a, r['rejected']) for a, r in results]}"
    )
    assert all(accepted[first_accept:]), (
        "acceptance flipped back off as speech got louder -- "
        f"{[(amp, r['accepted'], r['rejected']) for amp, r in results]}"
    )


def test_the_crossover_matches_the_configured_margin():
    """Where the gate actually switches must agree with `noise_snr_margin`, not just look sane.

    This is the test that fails if someone retunes the constant without revisiting the reasoning:
    it compares the measured speech/ambient ratio at the boundary against the configured margin.
    """
    margin = MicVAD(Trace()).noise_snr_margin
    results = [(amp, run_case(ROOM_SIGMA, amp)) for amp in AMPLITUDES]

    accepted = [(amp, r) for amp, r in results if r["accepted"]]
    floor_rejected = [(amp, r) for amp, r in results if r["rejected"] == "below_noise_floor"]
    assert accepted, "expected some level to be loud enough"
    assert floor_rejected, "expected some level to be quiet enough to fall below the floor"

    # Every below-floor rejection must genuinely be under the margin, and every acceptance over it.
    for amp, r in floor_rejected:
        ratio = r["speech_rms"] / r["ambient_rms"]
        assert ratio < margin, (
            f"amp={amp} was rejected as below_noise_floor but measured ratio {ratio:.2f} "
            f">= margin {margin}"
        )
    for amp, r in accepted:
        ratio = r["speech_rms"] / r["ambient_rms"]
        assert ratio >= margin, (
            f"amp={amp} was accepted but measured ratio {ratio:.2f} < margin {margin}"
        )


def test_the_bar_rises_with_the_measured_room():
    """The gate is relative: identical speech passes in a quiet room and fails in a louder one.

    Levels are measured, not assumed -- at sigma 0.002 the room lands around 68 RMS and at 0.02
    around 679, so the same 0.06 speech sits far above the margin in one and far below in the
    other. Both rooms here are quiet enough that ambient is actually tracked; see the
    known-limitation test below for what happens when it is not.
    """
    quiet = run_case(0.002, 0.06)
    moderate = run_case(0.020, 0.06)

    assert quiet["accepted"], "close-range speech in a quiet room must be accepted"
    assert not moderate["accepted"] and moderate["rejected"] == "below_noise_floor", (
        "the identical speech level must fail against a louder measured room -- "
        "otherwise the test is absolute, not relative"
    )
    assert quiet["ambient_rms"] < moderate["ambient_rms"], "the tracker followed the room"


def test_known_limitation_a_loud_room_is_never_measured_so_the_floor_never_engages():
    """FOUND BY THIS SWEEP, and it is a real hole rather than a quirk of the synthetic audio.

    `_ambient_rms` is only updated on frames webrtcvad reports as *not* voiced. Once room noise is
    loud enough that the detector calls it speech, that branch stops running, ambient stays None,
    and `_rejection_reason` takes its documented "never measured -> accept" path. The noise-floor
    gate therefore disables itself in precisely the loud room it exists for.

    **Three fixes were attempted and all three were reverted.** Recorded so the next person does
    not spend the afternoon again:

    1. *Minimum statistics over a trailing RMS window.* An utterance always ends with the silence
       run that triggers the offset, so the minimum reliably landed on it and reported a floor of
       ~0 — permissive in a different, less honest way.
    2. *Snapshot the floor at onset instead of at the offset.* In a loud room webrtcvad flags the
       room itself as voiced from the very first frame, so onset fired at frame 1 with two frames
       of history and never fired again. No usable snapshot point exists.
    3. *Minimum over the window, excluding digital-silence frames.* This did engage the floor in a
       loud room, and made the gate **non-monotonic**: at room sigma 0.05, speech at amp 0.02 was
       accepted while amp 0.35 was rejected. Louder speech becoming *less* acceptable is a worse
       failure than the permissive default, and `test_acceptance_is_monotonic_in_speech_level`
       catches it.

    The reason all three fail is measurable and fundamental: **from room sigma 0.02 upward,
    webrtcvad calls 100% of room-noise frames voiced** (50/50 at 0.02, 0.05 and 0.10). Once that
    happens, `speech_rms` — the mean over voiced frames — is contaminated by room noise, and any
    floor derived from the same frames is contaminated by speech. No RMS-based estimator can
    separate two signals the detector underneath it has already merged.

    A real fix therefore needs a *different signal*, not a better statistic: acoustic echo
    cancellation, spectral features, or a separate noise estimator — and real-room evidence rather
    than synthetic Gaussian tone. Pinned so the behaviour is visible instead of surprising, and so
    a future fix has a failing-by-design test to flip.

    Practical consequence for the demo: in a loud hall the gate contributes nothing, and only
    `min_speech_ms` still filters. Headphones and a close mic remain required (MEMORY.md §7).
    """
    loud = run_case(0.05, 0.06)

    assert loud["ambient_rms"] is None, (
        "the room was never measured -- if this now reports a level, the tracker was fixed "
        "and this test should be replaced by a real floor assertion"
    )
    assert loud["accepted"], "so the utterance passes on the documented permissive default"


# ============================ duration sweep ============================

DURATION_FRAMES = [2, 6, 10, 12, 13, 20, 40]      # 40 ms .. 800 ms


@pytest.mark.parametrize("n_frames", DURATION_FRAMES)
def test_duration_gate_matches_min_speech_ms(n_frames):
    """`min_speech_ms` must be exactly the boundary, in a quiet room where SNR cannot interfere."""
    vad = MicVAD(Trace())
    for _ in range(ROOM_FRAMES):
        vad.process_frame(SILENCE)
    for i in range(n_frames):
        vad.process_frame(voiced_frame(i * FRAME, amp=0.35))
    for _ in range(40):
        vad.process_frame(SILENCE)

    ended = vad.trace.last(EventType.SPEECH_ENDED)
    if ended is None:
        pytest.fail(f"{n_frames} frames produced no SpeechEnded at all")

    voiced_ms = ended.fields["voiced_ms"]
    accepted = not vad.utterances.empty()
    if voiced_ms < vad.min_speech_ms:
        assert not accepted and ended.fields["rejected"] == "too_short", (
            f"{voiced_ms} ms is under min_speech_ms={vad.min_speech_ms} and must be rejected"
        )
    else:
        assert accepted, f"{voiced_ms} ms clears min_speech_ms={vad.min_speech_ms} and must pass"


def test_duration_acceptance_is_monotonic():
    """Longer speech is never less acceptable than shorter speech."""
    accepted = []
    for n in DURATION_FRAMES:
        vad = MicVAD(Trace())
        for _ in range(ROOM_FRAMES):
            vad.process_frame(SILENCE)
        for i in range(n):
            vad.process_frame(voiced_frame(i * FRAME, amp=0.35))
        for _ in range(40):
            vad.process_frame(SILENCE)
        accepted.append(not vad.utterances.empty())

    first = next((i for i, a in enumerate(accepted) if a), None)
    assert first is not None, "no duration was accepted"
    assert all(accepted[first:]), f"acceptance flipped off as speech got longer: {accepted}"


# ============================ the unmeasured-room case ============================

def test_an_unmeasured_room_never_rejects_on_the_floor():
    """Before ambient exists, the floor test must not fire -- dropping real speech is worse.

    Pins the documented conservative default (`vad.py`: "when ambient has never been measured,
    accept"), which a sweep would otherwise never reach because every case above measures a room
    first.
    """
    vad = MicVAD(Trace())
    assert vad._ambient_rms is None, "precondition: the room has not been measured"
    for i in range(SPEECH_FRAMES):
        vad.process_frame(voiced_frame(i * FRAME, amp=0.35))
    for _ in range(40):
        vad.process_frame(SILENCE)

    ended = vad.trace.last(EventType.SPEECH_ENDED)
    assert ended.fields["rejected"] != "below_noise_floor"
