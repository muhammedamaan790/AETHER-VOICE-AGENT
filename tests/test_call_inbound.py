"""Inbound call audio: the caller heard the greeting, and AETHER could not hear them.

THE REGRESSION THESE PREVENT. A real call reached the worker. The caller heard the greeting, so
SIP dispatch, registration, `connect()`, the published track, the outbound pump, Rime and the
AudioGate were all working. Nothing the caller said ever produced a response.

The caller's track was attached on BOTH discovery paths -- queued from `track_subscribed` during
the ~9 s pipeline build, then found again by the already-subscribed sweep afterwards. Two
`rtc.AudioStream` readers then pushed every frame into ONE `InboundBridge` sharing one `_carry`
buffer: the audio arrived doubled and interleaved, the 320-sample rebuffering was scrambled across
two producers, and every counter looked healthy because `samples_received` had doubled.

Two further inbound faults are covered here, both of which would have hidden or worsened that one:
a single unconvertible frame ending the pump for the whole call, and diagnostics quoting a sample
rate they never received.

None of this asserts anything about real telephony audio. These are structural guarantees about
the plumbing; the call path itself remains unvalidated (RIME_EVIDENCE Part 6).
"""

from __future__ import annotations

import asyncio
import os
import wave

import numpy as np
import pytest

from aether.bridge import AudioChunk, InboundBridge

try:
    import livekit.rtc  # noqa: F401

    HAS_LIVEKIT = True
except Exception:
    HAS_LIVEKIT = False

needs_livekit = pytest.mark.skipif(not HAS_LIVEKIT, reason="livekit not installed here")


class _Mic:
    """The detector's shape, with no PortAudio behind it."""

    samplerate = 16000
    frame_samples = 320
    listening = True

    def set_listening(self, value):
        self.listening = bool(value)

    def set_context(self, **k): ...

    def speech_floor(self):
        return 35.0

    def process_frame(self, frame): ...


class _Track:
    """Enough of `rtc.Track` for the dedup: an sid."""

    def __init__(self, sid: str):
        self.sid = sid


def _bare_bridge(capture_path=None):
    """A `CallBridge` with only the fields the method under test touches.

    Built with `__new__` rather than `__init__` because the real constructor opens an
    `rtc.AudioSource`, which needs a LiveKit runtime that a unit test has no business starting.
    """
    from aether.telephony.agent import CallBridge

    bridge = CallBridge.__new__(CallBridge)
    bridge.attached = set()
    bridge._tasks = []
    bridge._capture = []
    bridge._capture_path = capture_path
    bridge.inbound = None
    bridge.frames_failed = 0
    return bridge


@pytest.fixture
def no_event_loop(monkeypatch):
    """Collect the pump coroutines instead of scheduling them, and close them cleanly."""
    created = []

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    return created


# ============================ the double pump ============================

@needs_livekit
def test_one_track_offered_twice_starts_exactly_one_pump(no_event_loop):
    """The defect that left the caller inaudible while the greeting played perfectly."""
    bridge = _bare_bridge()
    track = _Track("TR_caller")

    assert bridge.start_inbound(track) is True, "the first offer starts a pump"
    assert bridge.start_inbound(track) is False, "the second must not start another"
    assert len(bridge.attached) == 1
    assert len(no_event_loop) == 1, "exactly one pump coroutine was created"


@needs_livekit
def test_two_different_tracks_still_each_get_a_pump(no_event_loop):
    """Per track, not one-pump-per-call: a second participant must still be heard."""
    bridge = _bare_bridge()

    assert bridge.start_inbound(_Track("TR_a")) is True
    assert bridge.start_inbound(_Track("TR_b")) is True
    assert len(no_event_loop) == 2


@needs_livekit
def test_a_track_without_an_sid_is_still_deduplicated(no_event_loop):
    """Identity is the fallback, so an SDK change cannot quietly reintroduce the double pump."""
    bridge = _bare_bridge()
    track = object()

    assert bridge.start_inbound(track) is True
    assert bridge.start_inbound(track) is False
    assert len(no_event_loop) == 1


@needs_livekit
def test_both_discovery_paths_are_kept():
    """Losing the track entirely is the bug the two paths were written to fix.

    The dedup must not be "fixed" by deleting one of them -- the event can land during the build,
    or the track can already be subscribed before the handler is attached, and either may be the
    one that fires.
    """
    import inspect

    from aether.telephony import agent

    src = inspect.getsource(agent.hotel_call)
    assert "pending_tracks" in src, "the queued path must survive"
    assert "remote_participants" in src, "and so must the already-subscribed sweep"


@needs_livekit
def test_only_one_place_may_start_an_inbound_pump():
    """Structural guard.

    The natural fix for a lost track is to add another attach path, and the last time that
    happened it produced the double pump. Pinning the count means a third path has to go through
    `start_inbound`, which is where the dedup lives.
    """
    import io
    import tokenize

    from aether.telephony import agent

    src = open(agent.__file__, encoding="utf-8").read()
    code = " ".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    assert code.count("pump_inbound") == 2, (
        "pump_inbound should be defined once and started from exactly one place"
    )


# ============================ a bad frame costs 20 ms, not the call ============================

class _Stream:
    """Three frames, the middle one poison."""

    def __init__(self, frames):
        self.frames = frames
        self.closed = False

    def __aiter__(self):
        async def gen():
            for frame in self.frames:
                yield type("E", (), {"frame": frame})()

        return gen()

    async def aclose(self):
        self.closed = True


@needs_livekit
def test_an_unconvertible_frame_does_not_end_the_pump(monkeypatch):
    """One malformed frame used to leave AETHER deaf for the remainder of the call.

    The room, the outbound pump and the turn loop all kept running, so the call looked alive and
    the only evidence was a single line in the log.
    """
    import aether.telephony.agent as mod

    pushed = []
    bridge = _bare_bridge()
    bridge._closing = asyncio.Event()
    bridge.inbound = type("I", (), {"push": lambda _s, chunk: pushed.append(chunk)})()

    def convert(frame):
        if frame == "poison":
            raise ValueError("buffer is not a whole number of int16 samples")
        return frame

    stream = _Stream(["good", "poison", "good"])
    monkeypatch.setattr(mod, "to_chunk", convert)
    monkeypatch.setattr(mod.rtc, "AudioStream", lambda _track: stream)

    asyncio.run(bridge.pump_inbound(object()))

    assert bridge.frames_failed == 1, "the bad frame is counted"
    assert pushed == ["good", "good"], "and the frames after it still reach the VAD"
    assert stream.closed


@needs_livekit
def test_a_systematically_bad_stream_is_counted_not_logged_to_death(monkeypatch):
    """Fifty failures a second would bury the rest of the log. The count is what matters."""
    import aether.telephony.agent as mod

    bridge = _bare_bridge()
    bridge._closing = asyncio.Event()
    bridge.inbound = type("I", (), {"push": lambda _s, _c: None})()

    stream = _Stream(["poison"] * 40)
    monkeypatch.setattr(mod, "to_chunk", lambda _f: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setattr(mod.rtc, "AudioStream", lambda _track: stream)

    asyncio.run(bridge.pump_inbound(object()))

    assert bridge.frames_failed == 40, "every failure is counted"


# ============================ diagnostics honesty ============================

def test_diagnostics_report_the_rate_actually_observed():
    """`source_rate` is what was expected; `observed_rate` is what arrived.

    Quoting 48 kHz for audio that arrived at 8 kHz understates the call six-fold and reads as
    "almost no audio arrived" -- the wrong diagnosis, argued from a number nobody measured.
    """
    bridge = InboundBridge(_Mic(), source_rate=48000)
    for _ in range(5):
        bridge.push(AudioChunk(data=np.zeros(160, dtype=np.int16), sample_rate=8000))

    stats = bridge.diagnostics()
    assert stats["observed_rate"] == 8000
    assert stats["source_rate"] == 48000, "what was expected is still reported, for comparison"
    # 5 blocks x 160 samples at 8 kHz = 100 ms. At the assumed 48 kHz it would read as 16.7 ms.
    assert stats["audio_ms"] == pytest.approx(100.0, abs=0.5)


def test_diagnostics_fall_back_to_the_expected_rate_before_any_audio():
    stats = InboundBridge(_Mic(), source_rate=48000).diagnostics()
    assert stats["observed_rate"] is None
    assert stats["audio_ms"] == 0.0


def test_the_report_surfaces_a_double_pump():
    """`pumps=2` for one caller is the signature of the interleaved-audio defect."""
    from aether.telephony.diagnostics import diagnose, format_report
    from aether.trace import Trace

    inbound = InboundBridge(_Mic(), source_rate=48000)
    report = diagnose(Trace(), inbound=inbound)
    report["inbound_pumps"] = 2
    report["inbound_frames_failed"] = 7

    text = format_report(report)
    assert "pumps=2" in text
    assert "frames_failed=7" in text


def test_the_report_omits_transport_facts_when_there_are_none():
    """The local path has no pumps, and an empty line would be noise."""
    from aether.telephony.diagnostics import diagnose, format_report
    from aether.trace import Trace

    assert "transport:" not in format_report(diagnose(Trace()))


# ============================ the capture is opt-in ============================

@needs_livekit
def test_the_capture_is_off_unless_asked_for(monkeypatch):
    """It records a real caller's voice. That is always a deliberate act, never a default."""
    monkeypatch.delenv("AETHER_CALL_CAPTURE", raising=False)
    bridge = _bare_bridge(capture_path=os.environ.get("AETHER_CALL_CAPTURE") or None)

    assert bridge._capture_path is None
    assert bridge.write_capture() is None


@needs_livekit
def test_no_frame_tap_is_installed_when_capture_is_off(no_event_loop):
    """Capture must cost nothing on an ordinary call."""
    bridge = _bare_bridge(capture_path=None)
    bridge.inbound = InboundBridge(_Mic(), source_rate=48000)

    bridge.start_inbound(_Track("TR_caller"))
    assert bridge.inbound.on_frame is None


@needs_livekit
def test_the_capture_holds_the_samples_the_vad_was_given(tmp_path):
    """What AETHER heard, not what the transport sent: post-resample, post-rebuffer."""
    out = tmp_path / "call.wav"
    bridge = _bare_bridge(capture_path=str(out))
    bridge.spike = type("S", (), {"mic": _Mic()})()

    inbound = InboundBridge(_Mic(), source_rate=48000)
    inbound.on_frame = bridge._capture.append
    rng = np.random.default_rng(11)
    for _ in range(6):
        inbound.push(AudioChunk(data=rng.normal(0, 3000, 960).astype(np.int16),
                                sample_rate=48000))

    assert bridge.write_capture() == str(out)
    with wave.open(str(out), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        written = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)

    assert len(written) == inbound.frames_delivered * 320
    assert np.array_equal(written, np.concatenate(bridge._capture))


@needs_livekit
def test_a_failed_capture_does_not_disturb_the_teardown():
    """A diagnostic must never be able to damage the thing it is diagnosing."""
    bridge = _bare_bridge(capture_path="/no/such/directory/call.wav")
    bridge._capture = [np.zeros(320, dtype=np.int16)]
    bridge.spike = type("S", (), {"mic": _Mic()})()

    assert bridge.write_capture() is None      # must not raise


def test_the_frame_tap_sees_exactly_what_the_vad_sees():
    """The tap is on the bridge, so it cannot drift from what `process_frame` is handed."""
    seen_by_tap, seen_by_vad = [], []

    mic = _Mic()
    mic.process_frame = seen_by_vad.append
    bridge = InboundBridge(mic, source_rate=48000)
    bridge.on_frame = seen_by_tap.append

    rng = np.random.default_rng(5)
    for _ in range(4):
        bridge.push(AudioChunk(data=rng.normal(0, 2000, 960).astype(np.int16),
                               sample_rate=48000))

    assert len(seen_by_tap) == len(seen_by_vad) > 0
    for tapped, delivered in zip(seen_by_tap, seen_by_vad):
        assert np.array_equal(tapped, delivered)


def test_the_tap_never_sees_audio_the_listening_gate_dropped():
    """STOP LISTENING means not heard, and a capture that recorded it anyway would be a lie."""
    mic = _Mic()
    bridge = InboundBridge(mic, source_rate=48000)
    tapped = []
    bridge.on_frame = tapped.append

    mic.set_listening(False)
    for _ in range(4):
        bridge.push(AudioChunk(data=np.full(960, 5000, dtype=np.int16), sample_rate=48000))

    assert tapped == []


# ============================ the console opens itself ============================

@needs_livekit
def test_the_console_opens_a_browser_on_startup(monkeypatch):
    """The page must be up BEFORE the phone rings, and a printed link is a step to forget."""
    import aether.telephony.agent as mod

    opened = []
    monkeypatch.setenv("AETHER_WEB_HTTP_PORT", "8830")
    monkeypatch.setenv("AETHER_WEB_WS_PORT", "8831")
    monkeypatch.delenv("AETHER_WEB_OPEN", raising=False)
    monkeypatch.setattr(mod.webbrowser, "open", lambda url: opened.append(url))
    # Fire the delayed open immediately instead of waiting on a real timer.
    monkeypatch.setattr(mod.threading, "Timer", lambda _delay, fn: type(
        "T", (), {"start": staticmethod(fn)})())

    console = mod.start_console()
    try:
        assert opened == ["http://127.0.0.1:8830/index.html?ws=8831"]
    finally:
        console.stop()


@needs_livekit
def test_opening_a_browser_can_be_turned_off(monkeypatch):
    """A headless worker has no browser, and the attempt would be noise."""
    import aether.telephony.agent as mod

    opened = []
    monkeypatch.setenv("AETHER_WEB_HTTP_PORT", "8832")
    monkeypatch.setenv("AETHER_WEB_WS_PORT", "8833")
    monkeypatch.setenv("AETHER_WEB_OPEN", "0")
    monkeypatch.setattr(mod.webbrowser, "open", lambda url: opened.append(url))

    console = mod.start_console()
    try:
        assert opened == []
        assert console is not None, "the console still serves; only the browser is suppressed"
    finally:
        console.stop()


@needs_livekit
def test_a_browser_that_will_not_open_does_not_stop_the_worker(monkeypatch):
    import aether.telephony.agent as mod

    monkeypatch.setenv("AETHER_WEB_HTTP_PORT", "8834")
    monkeypatch.setenv("AETHER_WEB_WS_PORT", "8835")
    monkeypatch.delenv("AETHER_WEB_OPEN", raising=False)
    monkeypatch.setattr(mod.webbrowser, "open",
                        lambda url: (_ for _ in ()).throw(RuntimeError("no display")))
    monkeypatch.setattr(mod.threading, "Timer", lambda _delay, fn: type(
        "T", (), {"start": staticmethod(fn)})())

    console = mod.start_console()
    try:
        assert console is not None
    finally:
        console.stop()
