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

from aether.audio.vad import DEFAULT_SPEECH_FLOOR, MicVAD
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


# These tests are about the NOISE GATE, not about endpointing, so the endpoint window is pinned
# rather than inherited. `DEFAULT_ENDPOINT_MS` was raised from 500 to 1000 after a real call, and
# that has a genuine side effect on this sweep: the longer the window, the louder a room has to be
# before it never yields 50 consecutive unvoiced frames, never leaves `_speech_active`, and so is
# never measured at all -- widening the known limitation documented below. Pinning keeps these
# tests measuring the gate, and the interaction gets its own test rather than silently changing
# what these assert.
GATE_OFFSET_FRAMES = 25


def run_case(room_sigma: float, speech_amp: float) -> dict:
    """Feed room tone then speech, and report what the gate decided and why.

    Returns the decision plus the two levels the gate actually measured, so a failing assertion
    shows the evidence behind the decision rather than just the verdict.
    """
    vad = MicVAD(Trace(), offset_frames=GATE_OFFSET_FRAMES)
    for f in room_frames(room_sigma):
        vad.process_frame(f)
    for i in range(SPEECH_FRAMES):
        vad.process_frame(voiced_frame(i * FRAME, amp=speech_amp))
    for _ in range(vad.offset_frames + 15):  # clear webrtcvad's ~6-frame hangover
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


def test_a_longer_endpoint_widens_the_unmeasured_room_limitation():
    """MEASURED consequence of a longer endpoint window. 1000 ms was tried and reverted; this
    test is why anyone raising it again must look at the noise gate as well.

    `_ambient_rms` is only updated on unvoiced frames while no utterance is active. Ending an
    utterance needs `offset_frames` CONSECUTIVE unvoiced frames, so the longer that window, the
    quieter a room has to be before the detector ever gets out of "speech active" long enough to
    measure it. At 500 ms a room at sigma 0.02 was measured and the relative gate rejected the
    speech; at 1000 ms the same room is never measured and the gate falls back to the absolute
    floor.

    The absolute floor -- 2500 on telephony, derived from three real calls -- is what does the work
    on the phone path, and ambient WAS measured on every one of those calls (4.1-24.8 RMS), so the
    relative gate is not load-bearing there. That is why raising the window looked safe. It was
    reverted for a different reason: six-second buffers of mostly silence made Whisper guess.
    """
    def ambient_at(endpoint_frames: int) -> float | None:
        vad = MicVAD(Trace(), offset_frames=endpoint_frames)
        for f in room_frames(0.020):
            vad.process_frame(f)
        for i in range(SPEECH_FRAMES):
            vad.process_frame(voiced_frame(i * FRAME, amp=0.06))
        for _ in range(vad.offset_frames + 40):
            vad.process_frame(SILENCE)
        ended = vad.trace.last(EventType.SPEECH_ENDED)
        return ended.fields["ambient_rms"] if ended else None

    assert ambient_at(25) is not None, "at 500 ms this room is measured"
    assert ambient_at(50) is None, "at 1000 ms the same room never is"


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
    for _ in range(vad.offset_frames + 15):    # + webrtcvad's ~6-frame hangover
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
        for _ in range(vad.offset_frames + 15):
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
    for _ in range(vad.offset_frames + 15):    # + webrtcvad's ~6-frame hangover
        vad.process_frame(SILENCE)

    ended = vad.trace.last(EventType.SPEECH_ENDED)
    assert ended.fields["rejected"] != "below_noise_floor"


# ============================ the fence-time credibility gate ============================
#
# Regression for the failure a user hit live: "it is catching external noise and taking it as an
# interruption". Measured on real runs beforehand: 6 of 7 utterances the noise gate rejected had
# ALREADY ducked, stopped or fenced audio. The gate ran at the offset; the fence was applied at
# 300 ms of voiced audio, roughly 800 ms earlier. It decided correctly and arrived far too late.

def _progress_probe(vad):
    """Record every voiced-progress callback, i.e. every chance to fence."""
    seen = []
    vad.on_voiced_progress = lambda ms: seen.append(ms)
    return seen


def test_background_noise_never_reaches_the_fence_callback():
    """The fix: quiet noise may duck, but must never drive a fence."""
    vad = MicVAD(Trace())
    fences = _progress_probe(vad)

    for f in room_frames(0.0005, n=ROOM_FRAMES):        # near-silent room
        vad.process_frame(f)
    for i in range(SPEECH_FRAMES):                       # long, but far below the speech floor
        vad.process_frame(voiced_frame(i * FRAME, amp=0.0008))
    for _ in range(vad.offset_frames + 15):    # + webrtcvad's ~6-frame hangover
        vad.process_frame(SILENCE)

    assert fences == [], (
        "noise below the speech floor must never reach on_voiced_progress, because that callback "
        "is what promotes a duck into a fence and kills the in-flight turn"
    )


def test_real_speech_still_reaches_the_fence_callback():
    """The control. If this breaks, barge-in is dead and the product with it."""
    vad = MicVAD(Trace())
    fences = _progress_probe(vad)

    for f in room_frames(0.0005, n=ROOM_FRAMES):
        vad.process_frame(f)
    for i in range(SPEECH_FRAMES):
        vad.process_frame(voiced_frame(i * FRAME, amp=0.35))
    for _ in range(vad.offset_frames + 15):    # + webrtcvad's ~6-frame hangover
        vad.process_frame(SILENCE)

    assert fences, "close-range speech must still be able to interrupt the agent"
    assert len(fences) >= SPEECH_FRAMES - vad.onset_frames, (
        "the gate must stay open for the whole utterance, not just its first frames"
    )
    # Note: the value passed is wall-clock elapsed since the first voiced frame, so an offline
    # harness that feeds 800 ms of audio in ~2 ms sees single-digit milliseconds here. Whether it
    # crosses MEANINGFUL_SPEECH_MS is a real-time property, covered by
    # scripts/measure_audio_kill.py, which paces frames. What this file pins is *whether the
    # callback is reached at all* -- which is precisely what the credibility gate decides.


def test_ducking_is_still_unconditional():
    """Duck is not stop (MEMORY.md locked decision 6).

    The credibility gate deliberately guards only the fence. Ducking on a door slam is cheap and
    self-correcting; refusing to duck would make the agent talk over a real interruption's opening
    syllable while it waited for proof.
    """
    vad = MicVAD(Trace())
    onsets = []
    vad.on_onset = lambda t: onsets.append(t)

    for f in room_frames(0.0005, n=ROOM_FRAMES):
        vad.process_frame(f)
    for i in range(SPEECH_FRAMES):
        vad.process_frame(voiced_frame(i * FRAME, amp=0.0008))   # same noise as above
    for _ in range(vad.offset_frames + 15):    # + webrtcvad's ~6-frame hangover
        vad.process_frame(SILENCE)

    assert onsets, "onset (and therefore the duck) still fires on anything voiced"


def test_the_speech_floor_is_never_derived_from_a_silent_microphone():
    """A mic reporting near-zero ambient describes itself, not the room.

    Without the absolute floor the bar would be 0.5 x 3 = 1.5, which admits anything.
    """
    vad = MicVAD(Trace())
    vad._ambient_rms = 0.5                    # the level observed on real runs
    assert vad.speech_floor() == vad.speech_floor_abs
    assert vad.speech_floor() > 0.5 * vad.noise_snr_margin, "the raw bar of ~1.5 is not believed"


def test_a_loud_room_still_raises_the_floor_above_the_absolute_minimum():
    """The absolute floor is a minimum, not a replacement -- a loud room still dominates."""
    vad = MicVAD(Trace())
    vad._ambient_rms = 2000.0
    assert vad.speech_floor() == 2000.0 * vad.noise_snr_margin


def test_the_floor_is_configurable_per_microphone():
    """The regression that caused this rewrite: a hardcoded floor cannot suit every mic.

    A floor of 60, calibrated from one session, rejected genuine speech measured at 37-59 on the
    same machine in a later session. The value must be settable, not baked in.
    """
    assert MicVAD(Trace(), speech_floor=5.0).speech_floor_abs == 5.0
    assert MicVAD(Trace(), speech_floor=99.0).speech_floor_abs == 99.0


def test_the_floor_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("AETHER_SPEECH_FLOOR", "7.5")
    assert MicVAD(Trace()).speech_floor_abs == 7.5


# ---- the configured demo floor ----
#
# The demo microphone is calibrated to 35 in .env. `.env` is gitignored, so these tests set the
# variable explicitly rather than depending on a file that does not exist on a fresh clone -- what
# is pinned is that a configured value is *honoured all the way to a rejection decision*, not that
# any particular machine happens to be set up.

DEMO_FLOOR = 35.0


def test_a_configured_floor_reaches_the_rejection_decision(monkeypatch):
    """Configuration that never reaches the decision is decoration. This is the end-to-end path."""
    monkeypatch.setenv("AETHER_SPEECH_FLOOR", str(DEMO_FLOOR))
    vad = MicVAD(Trace())
    vad._ambient_rms = 0.5                    # quiet room: the absolute floor is what binds

    assert vad.speech_floor_abs == DEMO_FLOOR
    assert vad.speech_floor() == DEMO_FLOOR, "the relative bar of 1.5 must not win"
    # Long enough to clear min_speech_ms either way, so only the level is under test.
    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=DEMO_FLOOR + 1) is None
    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=DEMO_FLOOR - 1) == "below_noise_floor"


def test_the_configured_floor_admits_the_speech_that_was_wrongly_rejected(monkeypatch):
    """Regression for the live failure this value was chosen to fix.

    Peaks measured live at 37.3-59.7 were rejected against a floor of 60. Scored here by those
    same figures -- which were MEANS, so the real peaks are higher and the margin is wider still.
    """
    monkeypatch.setenv("AETHER_SPEECH_FLOOR", str(DEMO_FLOOR))
    vad = MicVAD(Trace())
    for observed, ambient in [(37.3, 1.9), (52.6, 0.8), (52.7, 4.9), (55.3, 9.3), (59.7, 12.0)]:
        vad._ambient_rms = ambient
        assert vad._rejection_reason(voiced_ms=800.0, peak_rms=observed) is None, (
            f"peak {observed} with ambient {ambient} must be accepted at floor {DEMO_FLOOR}"
        )


def test_a_loud_room_still_overrides_the_configured_floor(monkeypatch):
    """Configuring an absolute floor must not disable the relative test in a noisy room."""
    monkeypatch.setenv("AETHER_SPEECH_FLOOR", str(DEMO_FLOOR))
    vad = MicVAD(Trace())
    vad._ambient_rms = 40.0                   # relative bar 120, well above the configured 35

    assert vad.speech_floor() == 120.0
    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=60.0) == "below_noise_floor", (
        "a level fine in a quiet room is not fine against a loud one"
    )


def test_the_configured_floor_does_not_touch_the_duration_gate(monkeypatch):
    """Short commands must still interrupt: this change is to level, never to duration."""
    monkeypatch.setenv("AETHER_SPEECH_FLOOR", str(DEMO_FLOOR))
    vad = MicVAD(Trace())
    assert vad.min_speech_ms == 250.0, "duration threshold is untouched"
    # Loud but brief is still too short, exactly as before.
    assert vad._rejection_reason(voiced_ms=100.0, peak_rms=5000.0) == "too_short"
    # And a short real command that clears min_speech_ms still passes.
    assert vad._rejection_reason(voiced_ms=300.0, peak_rms=DEMO_FLOOR + 50) is None


def test_a_bad_environment_value_falls_back_loudly(monkeypatch, capsys):
    monkeypatch.setenv("AETHER_SPEECH_FLOOR", "loud-ish")
    vad = MicVAD(Trace())
    assert vad.speech_floor_abs == DEFAULT_SPEECH_FLOOR
    assert "AETHER_SPEECH_FLOOR" in capsys.readouterr().out, "a bad value must not fail silently"


# ---- peak vs mean: the length bias that rejected real speech ----

def test_the_gate_uses_the_peak_not_the_mean():
    """Root cause of the live rejection: a MEAN is dragged down by long utterances.

    Real speech carries vowel peaks far above its own average, and every extra word adds quiet
    voiced frames (inter-word gaps, trailing consonants) that lower the mean. Measured live:
    utterances of 880-1640 ms were rejected at mean 52-59 while shorter, comparable speech at the
    same distance was accepted. Gating on the peak removes the length dependence entirely.
    """
    vad = MicVAD(Trace(), speech_floor=50.0)
    # A long run whose MEAN is under the floor but which contains clear speech peaks.
    for _ in range(30):
        vad.process_frame(voiced_frame(0, amp=0.001))      # quiet inter-word frames
    vad._voiced_rms_peak = 400.0                            # one loud vowel
    vad._voiced_rms_sum, vad._voiced_rms_n = 30 * 10.0, 30  # mean 10, far below the floor

    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=vad._voiced_rms_peak) is None, (
        "a peak well above the floor must pass even when the mean is below it"
    )
    assert vad._is_credible_speech() is True


def test_flat_low_level_noise_has_no_peak_and_is_still_rejected():
    """The other half: noise is flat, so it has no peak to save it."""
    vad = MicVAD(Trace(), speech_floor=50.0)
    vad._voiced_rms_peak = 12.0
    vad._voiced_rms_sum, vad._voiced_rms_n = 10 * 11.0, 10

    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=vad._voiced_rms_peak) == "below_noise_floor"
    assert vad._is_credible_speech() is False


def test_the_trace_records_the_peak_and_the_floor_it_was_judged_against():
    """A decision the trace cannot explain is not evidence."""
    vad = MicVAD(Trace())
    for f in room_frames(0.0005, n=ROOM_FRAMES):
        vad.process_frame(f)
    for i in range(SPEECH_FRAMES):
        vad.process_frame(voiced_frame(i * FRAME, amp=0.35))
    for _ in range(vad.offset_frames + 15):    # + webrtcvad's ~6-frame hangover
        vad.process_frame(SILENCE)

    ended = vad.trace.last(EventType.SPEECH_ENDED)
    assert ended.fields["speech_peak_rms"] >= ended.fields["speech_rms"], "peak >= mean, always"
    # The floor is recorded as it stood AT THE DECISION. Reading `speech_floor()` afterwards gives
    # a different number, because ambient keeps tracking through the trailing silence -- so the
    # assertion is that the recorded floor explains the recorded verdict, not that it still matches.
    assert ended.fields["speech_floor"] >= vad.speech_floor_abs
    assert ended.fields["rejected"] is None
    assert ended.fields["speech_peak_rms"] >= ended.fields["speech_floor"], (
        "an accepted utterance must have cleared the floor it was actually judged against"
    )
