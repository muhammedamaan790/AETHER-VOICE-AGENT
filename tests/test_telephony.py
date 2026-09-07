"""The telephony boundary: frame conversion, configuration, and call lifecycle.

Split deliberately by what needs the SDK:

* **Frame conversion and config** run everywhere. `aether/telephony/frames.py` imports no LiveKit,
  so the part most likely to be subtly wrong about dtype, layout or channel count is covered in
  the main environment, before a phone is anywhere near it.
* **Agent lifecycle** needs `livekit`, and skips cleanly when it is absent rather than failing.

Nothing here has made a call. These tests say the plumbing is shaped correctly; they say nothing
about how AETHER behaves on real telephony audio.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.bridge import AudioChunk
from aether.telephony import (
    AGENT_NAME,
    TRANSPORT_CHANNELS,
    TRANSPORT_SAMPLE_RATE,
    LiveKitConfig,
)
from aether.telephony.frames import to_chunk, to_frame_args

class FakeAudioFrame:
    """Stands in for `rtc.AudioFrame`: same four attributes, no SDK needed.

    Matches livekit 1.1.17's public surface -- `data`, `sample_rate`, `num_channels`,
    `samples_per_channel` -- which is exactly what `to_chunk` duck-types against.
    """

    def __init__(self, pcm: np.ndarray, sample_rate: int, num_channels: int = 1):
        self.data = np.asarray(pcm, dtype=np.int16).tobytes()
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = len(pcm) // num_channels


# ============================ inbound conversion ============================

def test_a_mono_frame_converts_unchanged():
    pcm = np.array([0, 1000, -1000, 32767, -32768], dtype=np.int16)
    chunk = to_chunk(FakeAudioFrame(pcm, 48000))

    assert chunk.sample_rate == 48000
    assert chunk.data.dtype == np.int16
    np.testing.assert_array_equal(chunk.data, pcm)


def test_a_stereo_frame_is_downmixed_not_misread():
    """THE LAYOUT TRAP: LiveKit carries INTERLEAVED audio.

    Reading `[L0, R0, L1, R1, ...]` as mono yields alternating samples at double the apparent
    rate -- audible as a chipmunk buzz, and easy to ship without noticing because it still
    "produces audio".
    """
    left = np.array([100, 200, 300], dtype=np.int16)
    right = np.array([300, 400, 500], dtype=np.int16)
    interleaved = np.empty(6, dtype=np.int16)
    interleaved[0::2], interleaved[1::2] = left, right

    chunk = to_chunk(FakeAudioFrame(interleaved, 48000, num_channels=2))

    assert chunk.samples == 3, "a stereo frame must halve, not pass through at double length"
    np.testing.assert_array_equal(chunk.data, np.array([200, 300, 400], dtype=np.int16))


def test_downmixing_cannot_overflow():
    """Averaged in int32: two loud channels must not wrap through int16 halfway."""
    interleaved = np.array([32767, 32767, -32768, -32768], dtype=np.int16)
    chunk = to_chunk(FakeAudioFrame(interleaved, 48000, num_channels=2))
    assert chunk.data.tolist() == [32767, -32768]


def test_an_odd_stereo_frame_does_not_crash():
    """A truncated frame is a transport reality, not a reason to drop the call."""
    chunk = to_chunk(FakeAudioFrame(np.array([1, 2, 3], dtype=np.int16), 48000, num_channels=2))
    assert chunk.samples == 1


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 48000])
def test_the_frames_own_rate_is_carried_not_assumed(rate):
    """Telephony is often 8 kHz. The rate must come from the frame, never from a constant."""
    assert to_chunk(FakeAudioFrame(np.zeros(160, np.int16), rate)).sample_rate == rate


def test_an_empty_frame_is_harmless():
    assert to_chunk(FakeAudioFrame(np.zeros(0, np.int16), 48000)).samples == 0


# ============================ outbound conversion ============================

def test_frame_args_match_the_sdk_constructor():
    """`rtc.AudioFrame(data, sample_rate, num_channels, samples_per_channel)` -- in that order."""
    pcm = np.array([1, 2, 3, 4], dtype=np.int16)
    data, rate, channels, spc = to_frame_args(AudioChunk(pcm, 48000))

    assert data == pcm.tobytes()
    assert (rate, channels, spc) == (48000, 1, 4)


def test_samples_per_channel_is_per_channel_not_total():
    """LiveKit rejects a mismatch, and on mono the error is invisible until the first stereo sink."""
    pcm = np.array([1, 2, 3, 4], dtype=np.int16)
    _data, _rate, channels, spc = to_frame_args(AudioChunk(pcm, 48000), num_channels=2)
    assert (channels, spc) == (2, 4), "4 frames per channel, 8 samples total"


def test_a_round_trip_preserves_mono_audio():
    pcm = (np.sin(np.arange(480) * 0.1) * 8000).astype(np.int16)
    data, rate, channels, spc = to_frame_args(AudioChunk(pcm, 48000))
    back = to_chunk(FakeAudioFrame(np.frombuffer(data, dtype=np.int16), rate, channels))

    np.testing.assert_array_equal(back.data, pcm)
    assert back.sample_rate == 48000 and spc == len(pcm)


# ============================ configuration ============================

def test_config_reads_the_new_project_from_the_environment(monkeypatch):
    """Swapping projects must be three variables and no code change."""
    monkeypatch.setenv("LIVEKIT_URL", "wss://new-project.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIxxxx")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secretvalue")

    config = LiveKitConfig.from_env()
    assert config.configured is True
    assert config.project_host == "new-project.livekit.cloud"


def test_missing_credentials_are_reported_by_name_only(monkeypatch):
    for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(var, raising=False)

    config = LiveKitConfig.from_env()
    assert config.configured is False
    assert set(config.missing()) == {"LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"}
    assert config.project_host == "(unset)"


def test_the_secret_is_never_rendered(monkeypatch):
    """A traceback during a demo must not put a credential on screen."""
    monkeypatch.setenv("LIVEKIT_URL", "wss://p.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "do-not-print-me-1234567890")

    config = LiveKitConfig.from_env()
    assert "do-not-print-me-1234567890" not in repr(config)
    assert "do-not-print-me-1234567890" not in f"{config}"
    assert config.api_secret == "do-not-print-me-1234567890", "still usable, just not renderable"


def test_the_agent_registers_under_the_expected_name():
    """The dispatch rule will name this exactly; a typo here is a silent no-dispatch."""
    assert AGENT_NAME == "aether-hotel"


def test_transport_constants_are_mono():
    assert TRANSPORT_CHANNELS == 1
    assert TRANSPORT_SAMPLE_RATE == 48000


# ============================ agent lifecycle (needs the SDK) ============================

# A module-level `importorskip` would skip this WHOLE file, including the conversion and config
# tests above that deliberately need no SDK. Mark only the lifecycle tests instead.
try:
    import livekit  # noqa: F401
    HAS_LIVEKIT = True
except ImportError:
    HAS_LIVEKIT = False

needs_livekit = pytest.mark.skipif(not HAS_LIVEKIT, reason="livekit not installed here")


@needs_livekit


def test_the_agent_module_imports_and_names_its_agent():
    from aether.telephony import agent

    assert agent.AGENT_NAME == "aether-hotel"
    assert agent.server is not None


@needs_livekit
def test_the_agent_does_not_use_agent_session():
    """AgentSession would replace AETHER's STT, LLM, TTS and turn detection wholesale.

    Using it would mean the judged interruption path was LiveKit's, leaving GenerationRegistry,
    the AudioGate's per-chunk tagging and the four-layer fence sitting unused beside it.
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
    for forbidden in ("AgentSession", "AgentBuilder", "VoicePipelineAgent"):
        assert forbidden not in code, f"{forbidden} must not appear on the judged path"
    assert "rtc" in code, "it uses livekit.rtc directly"


@needs_livekit
def test_the_agent_reimplements_no_audio_or_fencing_logic():
    """It is plumbing. The brain is imported, never copied."""
    import io
    import tokenize

    from aether.telephony import agent

    src = open(agent.__file__, encoding="utf-8").read()
    code = " ".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    # `set_active_generation` is deliberately NOT forbidden: the greeting must tell the gate which
    # generation may play, exactly as `Day1Spike._blocking_turn` does before it speaks. Calling the
    # API is the point; reimplementing what it protects is not.
    for forbidden in ("webrtcvad", "mark_fenced", "GenerationRegistry", "process_frame",
                      "fence_generation", "request_duck", "request_stop"):
        assert forbidden not in code, f"the agent must not reimplement {forbidden}"
    assert "barge.begin_turn" in code or "begin_turn" in code, (
        "it drives the real coordinator rather than inventing a generation"
    )


# ============================ regressions from the first real call ============================
#
# The first call reached the worker, produced no audio, and was killed after ~60 s with
# "entrypoint did not exit in time". Four separate defects, each independently sufficient:
#
#   1. the entrypoint waited on an Event only its own teardown could set  -> permanent hang
#   2. the pipeline was built on the event loop (measured 9051 ms)        -> LiveKit stalled
#   3. `track_subscribed` was registered AFTER connect, i.e. after those  -> inbound never started
#      9 s, so the event fired into a gap and was lost
#   4. nothing ever spoke                                                 -> silence regardless

@needs_livekit
def test_the_closing_event_is_owned_by_the_entrypoint_not_the_bridge():
    """THE DEADLOCK. `CallBridge` must not create the Event it waits on.

    When it did, `aclose()` was the only setter -- and `aclose()` runs in the entrypoint's
    `finally`, after the wait it was supposed to release. Passing the Event in makes the cycle
    impossible to reintroduce without changing this signature.
    """
    import asyncio
    import inspect

    from aether.telephony.agent import CallBridge

    params = inspect.signature(CallBridge.__init__).parameters
    assert "closing" in params, "the entrypoint must own the closing Event and pass it in"

    owned = asyncio.Event()
    src = inspect.getsource(CallBridge.__init__)
    assert "asyncio.Event()" not in src, "the bridge must not create its own closing Event"
    assert owned is not None


@needs_livekit
def test_the_entrypoint_releases_on_disconnect():
    """A hangup must end the call. Without a disconnect handler nothing ever set the Event."""
    import inspect

    from aether.telephony import agent

    src = inspect.getsource(agent.hotel_call)
    assert 'room.on("disconnected")' in src, "a room disconnect must release the wait"
    assert 'room.on("participant_disconnected")' in src, "so must the caller hanging up"
    assert src.count("closing.set()") >= 2, "both disconnect paths must set it"


@needs_livekit
def test_handlers_are_registered_before_connect():
    """THE RACE. LiveKit subscribes the caller's track within ms of connect.

    Registering the handler afterwards -- especially after a 9 s blocking build -- means the event
    fires into a gap and is lost permanently, which is exactly why call one had no inbound audio.
    """
    import inspect

    from aether.telephony import agent

    src = inspect.getsource(agent.hotel_call)
    subscribed = src.index('room.on("track_subscribed")')
    connect = src.index("await ctx.connect()")
    assert subscribed < connect, "track_subscribed must be registered BEFORE connect()"

    disconnected = src.index('room.on("disconnected")')
    assert disconnected < connect, "disconnect must be registered BEFORE connect() too"


@needs_livekit
def test_a_track_arriving_before_the_pipeline_is_not_lost():
    """The pipeline takes ~9 s to build; a track arriving in that window must be queued."""
    import inspect

    from aether.telephony import agent

    src = inspect.getsource(agent.hotel_call)
    assert "pending_tracks" in src, "tracks arriving early must be held, not dropped"
    assert "already-subscribed" in src, "and tracks subscribed before we looked must be swept up"
    assert "remote_participants" in src, "by scanning participants after connect"


@needs_livekit
def test_the_pipeline_is_built_off_the_event_loop():
    """MEASURED 9051 ms. Inline, it stalls LiveKit's heartbeats for the whole of call setup."""
    import inspect

    from aether.telephony import agent

    src = inspect.getsource(agent.hotel_call)
    assert "asyncio.to_thread(build_pipeline" in src, (
        "constructing the pipeline blocks for ~9 s and must not run on the loop"
    )


@needs_livekit
def test_the_greeting_runs_off_the_event_loop_too():
    """Synthesis blocks. The loop must stay free to carry the audio the greeting produces."""
    import inspect

    from aether.telephony import agent

    src = inspect.getsource(agent.hotel_call)
    assert "asyncio.to_thread(speak_greeting" in src


def test_the_greeting_uses_the_ordinary_rime_path_and_is_interruptible():
    """It must be a real generation, so a caller talking over it fences it like any other answer."""
    import inspect

    from aether.telephony.agent import speak_greeting

    src = inspect.getsource(speak_greeting)
    assert "begin_turn" in src, "the greeting needs a real generation"
    assert "set_active_generation" in src, "so the gate knows it may play"
    assert "is_valid" in src, "and it must be fenceable mid-sentence"
    assert "end_turn" in src, "and must not leave a turn in flight"


def test_a_failing_greeting_does_not_end_the_call():
    """The caller can still speak. A greeting is not worth dropping a call for."""
    from aether.telephony.agent import speak_greeting

    class Boom:
        def __init__(self):
            self.barge = self
            self.gate = self
            self.rime = self
            self.ended = False

        def begin_turn(self, *, turn_id):
            return type("G", (), {"id": "G1"})()

        def set_active_generation(self, *a, **k): ...

        def speak(self, *a, **k):
            raise RuntimeError("rime is down")

        def is_valid(self, _gen):
            return True

        def end_turn(self):
            self.ended = True

    spike = Boom()
    assert speak_greeting(spike) is False, "it reports failure rather than raising"
    assert spike.ended is True, "and still closes the turn it opened"
