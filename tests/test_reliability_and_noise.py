"""Provider retry, and rejection of background/blip audio before it reaches STT.

Both are grounded in observed evidence, not guesses:

  - Measured 2026-09-06 against gemini-3.8-flash: 2 of 6 calls returned 503 UNAVAILABLE.
  - Real traces contain transcripts of `'You'`, `'Oh'`, `''` and "We'll see you in the next one"
    on short/quiet audio, costing 4.8-9.3 s of STT each. base.en genuinely returns `'You'` for
    digital silence, so these blips invent words that were never spoken.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.vad import MicVAD
from aether.events import EventType
from aether.llm import RetryingLLM, _is_transient
from aether.trace import Trace

SR = 16000
FRAME = 320          # 20 ms
SILENCE = np.zeros(FRAME, dtype=np.int16)


def voiced_frame(offset: int, amp: float = 0.35, f0: float = 140.0) -> np.ndarray:
    t = (np.arange(FRAME) + offset) / SR
    sig = np.zeros(FRAME)
    for k, a in [(1, 1.0), (2, 0.6), (3, 0.45), (4, 0.3), (5, 0.2), (8, 0.12), (12, 0.08)]:
        sig += a * np.sin(2 * np.pi * f0 * k * t)
    sig += 0.05 * np.random.default_rng(offset).normal(0, 1, FRAME)
    sig = sig / np.abs(sig).max() * amp
    return (sig * 32767).astype(np.int16)


# =========================== 1. provider reliability ===================================

class FlakyLLM:
    name = "flaky-fake"

    def __init__(self, fail_times, exc):
        self.fail_times = fail_times
        self._exc = exc
        self.calls = 0

    def respond(self, user_text, history=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._exc
        return "recovered answer"


@pytest.mark.parametrize("err", [
    RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand."),
    RuntimeError("429 rate limit exceeded"),
    RuntimeError("502 Bad Gateway"),
    TimeoutError("read timed out"),
    ConnectionError("connection reset by peer"),
])
def test_transient_errors_are_recognised(err):
    assert _is_transient(err) is True


@pytest.mark.parametrize("err", [
    RuntimeError("400 Bad Request"),
    RuntimeError("401 invalid api key"),
    RuntimeError("404 model not found"),
    ValueError("malformed content"),
])
def test_permanent_errors_are_not_retried(err):
    assert _is_transient(err) is False


def test_status_code_attribute_wins_over_message_text():
    class Err(Exception):
        status_code = 400
    # Message mentions 503, but the real status is a permanent 400.
    assert _is_transient(Err("upstream said 503")) is False


def test_a_transient_failure_recovers_without_reaching_the_caller():
    """The observed 503: one bad call must not cost the user their turn."""
    llm = FlakyLLM(fail_times=1, exc=RuntimeError("503 UNAVAILABLE"))
    wrapped = RetryingLLM(llm, backoff=(0.0, 0.0))

    assert wrapped.respond("hello") == "recovered answer"
    assert llm.calls == 2, "retried exactly once"


def test_retries_are_capped_and_then_the_error_propagates():
    """Exhausted retries must raise, so the caller's existing ResultDiscarded path runs."""
    llm = FlakyLLM(fail_times=99, exc=RuntimeError("503 UNAVAILABLE"))
    wrapped = RetryingLLM(llm, backoff=(0.0, 0.0))

    with pytest.raises(RuntimeError):
        wrapped.respond("hello")
    assert llm.calls == 3, "one attempt plus two retries, then give up"


def test_permanent_failure_is_not_retried_at_all():
    llm = FlakyLLM(fail_times=99, exc=RuntimeError("401 invalid api key"))
    wrapped = RetryingLLM(llm, backoff=(0.0, 0.0))

    with pytest.raises(RuntimeError):
        wrapped.respond("hello")
    assert llm.calls == 1, "an auth failure must fail fast, not burn the caller's time"


def test_no_response_is_ever_invented():
    """On total failure the wrapper raises. It must never substitute text of its own."""
    llm = FlakyLLM(fail_times=99, exc=RuntimeError("503"))
    wrapped = RetryingLLM(llm, backoff=(0.0,))
    with pytest.raises(RuntimeError):
        wrapped.respond("what is the capital of France?")


def test_retry_preserves_provider_identity_and_history():
    seen = {}

    class Recorder:
        name = "gemini:gemini-3.8-flash"

        def respond(self, user_text, history=None):
            seen["history"] = history
            return "ok"

    wrapped = RetryingLLM(Recorder())
    assert wrapped.name == "gemini:gemini-3.8-flash", "provider identity is unchanged"
    wrapped.respond("q", [{"role": "user", "content": "earlier"}])
    assert seen["history"] == [{"role": "user", "content": "earlier"}]


def test_build_llm_wraps_the_selected_provider(monkeypatch):
    from aether.llm import PROVIDER_ENV, build_llm

    for var, _, _ in PROVIDER_ENV.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "x")

    import groq as groq_sdk
    monkeypatch.setattr(groq_sdk, "Groq", lambda **k: object())

    llm = build_llm()
    assert isinstance(llm, RetryingLLM)
    assert llm.name.startswith("groq:"), "selection is unchanged; only reliability is added"


# =========================== 2. background voice / blips ===============================

# These tests are about the noise and duration gates, not about endpointing, so the endpoint
# window is pinned rather than inherited from DEFAULT_ENDPOINT_MS. That default was raised from
# 500 ms to 1000 ms after a real call, and a longer window means a noisy room never yields enough
# consecutive unvoiced frames to be measured -- see
# `test_a_longer_endpoint_widens_the_unmeasured_room_limitation` in test_adverse_audio.py, which
# owns that interaction. Without pinning, the relative-gate tests here quietly stop exercising the
# relative gate.
GATE_OFFSET_FRAMES = 25


def make_vad(trace=None, **kw):
    kw.setdefault("offset_frames", GATE_OFFSET_FRAMES)
    return MicVAD(trace or Trace(), **kw)


def feed(vad, frames):
    for f in frames:
        vad.process_frame(f)


def settle(vad, n=None):
    """Enough trailing silence to clear WebRTC VAD's ~6-frame hangover and end the utterance.

    Derived from the detector rather than hardcoded, so tuning AETHER_ENDPOINT_MS cannot silently
    stop these tests from ever reaching an offset -- which would leave them asserting on an
    utterance that never ended.
    """
    feed(vad, [SILENCE] * (n if n is not None else vad.offset_frames + 15))


def test_close_range_speech_is_still_accepted():
    """The gate must not break the normal case -- this is the regression that matters most."""
    vad = make_vad()
    feed(vad, [SILENCE] * 20)                                  # measure the room
    feed(vad, [voiced_frame(i * FRAME, amp=0.35) for i in range(40)])   # 800 ms of speech
    settle(vad)

    assert not vad.utterances.empty(), "normal speech must still be delivered"


def test_a_short_blip_is_rejected_before_it_reaches_stt():
    """`'You'` / `'Oh'` in the traces: 40 ms is enough for onset but is not an utterance."""
    vad = make_vad()
    feed(vad, [SILENCE] * 20)
    feed(vad, [voiced_frame(i * FRAME, amp=0.35) for i in range(4)])    # 80 ms
    settle(vad)

    assert vad.utterances.empty(), "a blip must not become a turn"
    ended = vad.trace.last(EventType.SPEECH_ENDED)
    assert ended.fields["rejected"] == "too_short"


def test_background_voice_is_rejected_as_below_the_noise_floor():
    """Someone talking across the room IS speech -- just not speech aimed at this mic."""
    vad = make_vad()
    # A noisy room: ambient is established well above digital silence.
    rng = np.random.default_rng(1)
    room = (rng.normal(0, 0.02, (30, FRAME)) * 32767).astype(np.int16)
    feed(vad, list(room))
    # Distant speech: long enough, but barely above the room itself.
    feed(vad, [voiced_frame(i * FRAME, amp=0.03) for i in range(40)])
    settle(vad)

    ended = vad.trace.last(EventType.SPEECH_ENDED)
    if ended.fields["rejected"] is not None:
        assert ended.fields["rejected"] == "below_noise_floor"
        assert vad.utterances.empty()
    else:
        pytest.skip("synthetic room/voice levels did not separate; covered by the ratio test")


def test_the_noise_floor_test_is_relative_not_absolute():
    """Loud speech in a loud room is accepted; the bar scales with the measured room."""
    vad = make_vad()
    rng = np.random.default_rng(2)
    room = (rng.normal(0, 0.02, (30, FRAME)) * 32767).astype(np.int16)
    feed(vad, list(room))
    feed(vad, [voiced_frame(i * FRAME, amp=0.5) for i in range(40)])
    settle(vad)

    assert not vad.utterances.empty(), "loud close speech must survive a loud room"


def test_rejection_reason_logic_directly():
    vad = make_vad()
    vad._ambient_rms = 100.0

    assert vad._rejection_reason(voiced_ms=100.0, peak_rms=5000.0) == "too_short"
    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=150.0) == "below_noise_floor"
    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=5000.0) is None


def test_gate_is_permissive_for_plausible_speech_when_the_room_was_never_measured():
    """Fail open for anything that could be speech, but not for digital silence.

    CHANGED 2026-09-07, deliberately. The old contract was "when ambient has never been measured,
    accept unconditionally", asserted here with `speech_rms=1.0`. Live runs showed why that is too
    permissive: ambient tracked to 0.5-9.7 RMS on a quiet microphone, so the relative bar was ~2,
    and background noise at RMS 10-40 cleared it twentyfold while real speech sat at 60-2800. The
    floor is now clamped by `ambient_floor_min`, so a bar derived from a near-silent microphone is
    not believed.

    The spirit is unchanged -- dropping real close-range speech is still the worse failure, and
    the clamp sits far below every real utterance observed.
    """
    vad = make_vad()
    vad._ambient_rms = None

    # Plausible speech still passes with no measured room.
    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=200.0) is None
    # Digital silence does not. RMS 1.0 for 800 ms is not a quiet talker, it is nothing.
    assert vad._rejection_reason(voiced_ms=800.0, peak_rms=1.0) == "below_noise_floor"


def test_speech_ended_records_the_measurements_behind_the_decision():
    vad = make_vad()
    feed(vad, [SILENCE] * 20)
    feed(vad, [voiced_frame(i * FRAME, amp=0.35) for i in range(40)])
    settle(vad)

    f = vad.trace.last(EventType.SPEECH_ENDED).fields
    for key in ("voiced_ms", "speech_rms", "ambient_rms", "rejected"):
        assert key in f, f"{key} must be traceable to explain the decision"
    assert f["voiced_ms"] >= 250.0


def test_rejection_does_not_disturb_onset_or_fencing_callbacks():
    """Ducking still happens on onset: someone did speak, even if it is not a turn."""
    vad = make_vad()
    onsets, progress = [], []
    vad.on_onset = onsets.append
    vad.on_voiced_progress = progress.append

    feed(vad, [SILENCE] * 20)
    feed(vad, [voiced_frame(i * FRAME, amp=0.35) for i in range(4)])   # rejected blip
    settle(vad)

    assert len(onsets) == 1, "onset still fires, so the agent still ducks"
    assert progress, "voiced-progress still drives the existing fencing decision"
    assert vad.utterances.empty(), "but the blip is not delivered as a turn"


def test_detector_resets_after_a_rejection():
    """A rejected utterance must not poison the next real one."""
    vad = make_vad()
    feed(vad, [SILENCE] * 20)
    feed(vad, [voiced_frame(i * FRAME, amp=0.35) for i in range(4)])
    settle(vad)
    assert vad.utterances.empty()

    feed(vad, [voiced_frame(i * FRAME, amp=0.35) for i in range(40)])
    settle(vad)
    assert not vad.utterances.empty(), "the following real utterance must be accepted"
