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


# ============================ identity: the words a caller actually hears ============================
#
# Reported from a live session: AETHER introduced itself as "I am a text-based assistant." The
# SYSTEM_PROMPT already said "a voice assistant... spoken aloud" at the time, so a positive role
# statement was not enough. `tests/test_llm_providers.py` pins the prompt's denials; this pins the
# GREETING, which is the very first thing a caller hears and is not model-generated at all.

BANNED_SELF_DESCRIPTIONS = (
    "text-based", "text based", "chatbot", "chat bot", "language model",
    "as an ai", "i am an ai", "virtual assistant", "software", "program",
    "type", "typing", "message", "text me",
)


def test_the_greeting_never_describes_a_text_interaction():
    from aether.telephony.agent import GREETING

    spoken = GREETING.lower()
    for banned in BANNED_SELF_DESCRIPTIONS:
        assert banned not in spoken, f"the greeting must never say {banned!r}"


def test_the_greeting_identifies_aether_as_the_hotel_manager():
    """A caller must know who picked up, in the first sentence."""
    from aether.telephony.agent import GREETING

    spoken = GREETING.lower()
    assert "aether" in spoken
    assert "manager" in spoken, "the role, not a generic assistant"


def test_the_greeting_is_short_enough_to_interrupt_comfortably():
    """It is spoken down a phone line, and a caller in a hurry will talk over it."""
    from aether.telephony.agent import GREETING

    assert len(GREETING.split()) <= 20, "a long greeting is a bad phone greeting"
    assert GREETING.strip().endswith("?"), "it should hand the turn back to the caller"


def test_the_greeting_is_speakable():
    """No digits or symbols: it goes straight to Rime."""
    import re

    from aether.telephony.agent import GREETING

    assert not re.search(r"[\d*_`#|<>{}\[\]]", GREETING), f"not speakable: {GREETING!r}"


# ============================ call diagnostics ============================
#
# "AETHER never replied" was the entire report from the first real call, and it is six different
# faults wearing the same coat. These tests pin that each one is named distinctly, because the
# value of the report is precisely that it does not say "something went wrong".

class _DiagMic:
    def __init__(self, listening=True):
        self.on_onset = None
        self.on_voiced_progress = None
        self.listening = listening
        self.samplerate = 16000
        self.frame_samples = 320

    def set_listening(self, value):
        self.listening = bool(value)

    def set_context(self, **k): ...

    def speech_floor(self):
        return 35.0

    def process_frame(self, frame): ...


def _diag_bridges(listening=True, blocks=4):
    """An inbound bridge fed real audio, and an outbound bridge that has published silence."""
    import numpy as np

    from aether.audio.player import AudioGate
    from aether.bridge import AudioChunk, InboundBridge, OutboundBridge
    from aether.trace import Trace

    inbound = InboundBridge(_DiagMic(listening), source_rate=48000)
    rng = np.random.default_rng(3)
    for _ in range(blocks):
        inbound.push(AudioChunk(data=rng.normal(0, 4000, 960).astype(np.int16), sample_rate=48000))
    outbound = OutboundBridge(AudioGate(Trace()), sink_rate=48000)
    outbound.pull()
    return inbound, outbound


def test_no_inbound_audio_is_named_as_such():
    from aether.bridge import InboundBridge
    from aether.telephony.diagnostics import diagnose
    from aether.trace import Trace

    report = diagnose(Trace(), inbound=InboundBridge(_DiagMic()))
    assert report["stage"] == "inbound_audio"
    assert report["inbound"]["samples_received"] == 0


def test_audio_that_arrived_while_not_listening_is_not_reported_as_silence():
    """The distinction that matters most: "nobody spoke" and "we were not listening" are different."""
    from aether.telephony.diagnostics import diagnose
    from aether.trace import Trace

    inbound, _out = _diag_bridges(listening=False)
    report = diagnose(Trace(), inbound=inbound)
    assert report["stage"] == "listening_gate"
    assert inbound.samples_received > 0
    assert inbound.frames_delivered == 0
    assert "listening was off" in report["detail"]


def test_vad_rejection_reports_the_levels_it_rejected():
    """The numbers needed to re-derive a threshold from real call audio, and nothing more."""
    from aether.telephony.diagnostics import diagnose
    from aether.trace import Trace

    inbound, _out = _diag_bridges()
    report = diagnose(Trace(), inbound=inbound)
    assert report["stage"] == "vad"
    assert "peak rms" in report["detail"] and "speech_floor" in report["detail"]
    assert inbound.frames_delivered > 0


def test_stt_failure_is_distinguished_from_vad_failure():
    from aether.events import EventType
    from aether.telephony.diagnostics import diagnose
    from aether.trace import Trace

    trace = Trace()
    trace.emit(EventType.SPEECH_ONSET)
    trace.emit(EventType.TRANSCRIPT_FINAL, text="")
    report = diagnose(trace)
    assert report["stage"] == "stt"
    assert report["empty_transcripts"] == 1


def test_a_rime_failure_is_named_with_its_reason():
    from aether.events import EventType
    from aether.telephony.diagnostics import diagnose
    from aether.trace import Trace

    trace = Trace()
    trace.emit(EventType.SPEECH_ONSET)
    trace.emit(EventType.TRANSCRIPT_FINAL, text="what starters do you have")
    trace.emit(EventType.RESULT_DISCARDED, stage="tts", reason="tts_error")
    report = diagnose(trace)
    assert report["stage"] == "tts"
    assert "tts_error" in report["detail"]


def test_a_reply_that_never_existed_is_not_blamed_on_rime():
    from aether.events import EventType
    from aether.telephony.diagnostics import diagnose
    from aether.trace import Trace

    trace = Trace()
    trace.emit(EventType.SPEECH_ONSET)
    trace.emit(EventType.TRANSCRIPT_FINAL, text="what starters do you have")
    trace.emit(EventType.RESULT_DISCARDED, stage="llm", reason="empty_llm_response")
    assert diagnose(trace)["stage"] == "reply"


def test_a_spoken_turn_that_published_no_audio_is_an_outbound_fault():
    """The caller heard silence even though everything upstream succeeded."""
    from aether.events import EventType
    from aether.telephony.diagnostics import diagnose
    from aether.trace import Trace

    inbound, outbound = _diag_bridges()
    trace = Trace()
    trace.emit(EventType.SPEECH_ONSET)
    trace.emit(EventType.TRANSCRIPT_FINAL, text="what starters do you have")
    trace.emit(EventType.RESPONSE_SPOKEN, text="For starters we have...")
    report = diagnose(trace, inbound=inbound, outbound=outbound)
    assert report["stage"] == "outbound_audio"
    assert outbound.blocks_pulled > 0 and outbound.blocks_with_audio == 0


def test_a_healthy_call_reports_no_failed_stage():
    import numpy as np

    from aether.events import EventType
    from aether.telephony.diagnostics import diagnose, format_report
    from aether.trace import Trace

    inbound, outbound = _diag_bridges()
    outbound.gate.set_active_generation("G1")
    outbound.gate.enqueue(np.full(outbound.gate.blocksize, 900, dtype=np.int16), gen="G1")
    outbound.pull()

    trace = Trace()
    trace.emit(EventType.SPEECH_ONSET)
    trace.emit(EventType.TRANSCRIPT_FINAL, text="what starters do you have")
    trace.emit(EventType.RESPONSE_SPOKEN, text="For starters we have...")

    report = diagnose(trace, inbound=inbound, outbound=outbound)
    assert report["stage"] is None
    assert report["leaks"] == 0
    assert "verdict  : OK" in format_report(report)


def test_the_report_states_measurements_and_never_a_threshold_to_use():
    """It measures. Deriving a new speech floor is a deliberate act against real call audio."""
    from aether.telephony.diagnostics import diagnose, format_report
    from aether.trace import Trace

    inbound, outbound = _diag_bridges()
    text = format_report(diagnose(Trace(), inbound=inbound, outbound=outbound)).lower()
    for advice in ("recommend", "should be", "try setting", "increase", "decrease"):
        assert advice not in text, "the report reports; it does not tune"


def test_diagnostics_does_not_import_livekit():
    """Like the rest of the non-agent telephony modules, so it runs under test."""
    import inspect
    import io
    import tokenize

    import aether.telephony.diagnostics as mod

    # CODE only. The module docstring says "no LiveKit import" in order to explain the boundary,
    # and a raw-source grep would fail on that explanation rather than on an import.
    src = inspect.getsource(mod)
    code = " ".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    ).lower()
    assert "livekit" not in code
