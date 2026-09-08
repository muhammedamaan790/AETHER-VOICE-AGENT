"""The LiveKit↔AETHER audio bridge, proven without a phone or the LiveKit SDK.

Two directions, one guarantee:

    inbound   transport audio ──▶ MicVAD ──▶ utterance queue      (STT path reached)
    outbound  AudioGate ──▶ transport audio                       (Rime output carried)
    and       a fence mid-response leaks nothing in either direction

The bridge deliberately does not import LiveKit, so these run in the main environment where the SDK
is not installed. That is the isolation boundary working: if these pass, the only thing left to get
wrong on a real call is the thin `rtc.AudioFrame` ↔ `AudioChunk` conversion at the call site.

Frame sizes here are chosen to be hostile on purpose — 480 and 960 samples at 48 kHz, which is what
LiveKit actually delivers and which divides into AETHER's 320-sample frames unevenly.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.audio.player import AudioGate
from aether.audio.vad import MicVAD
from aether.bridge import AudioChunk, InboundBridge, OutboundBridge, wav_to_chunks
from aether.events import EventType
from aether.interruption import BargeInCoordinator
from aether.supervisor.generations import GenerationRegistry
from aether.trace import Trace

SR_TRANSPORT = 48000          # what LiveKit hands us
SR_VAD = 16000                # what MicVAD wants


def speech(seconds: float, sample_rate: int = SR_TRANSPORT, amp: float = 0.35) -> np.ndarray:
    """Voiced audio webrtcvad reliably flags, at an arbitrary transport rate."""
    n = int(sample_rate * seconds)
    t = np.arange(n) / sample_rate
    sig = np.zeros(n)
    for k, a in [(1, 1.0), (2, 0.6), (3, 0.45), (4, 0.3), (5, 0.2), (8, 0.12)]:
        sig += a * np.sin(2 * np.pi * 140.0 * k * t)
    sig += 0.05 * np.random.default_rng(3).normal(0, 1, n)
    return (sig / np.abs(sig).max() * amp * 32767).astype(np.int16)


def endpoint_silence(mic) -> float:
    """Seconds of trailing silence that will actually end an utterance on THIS detector.

    Derived rather than hardcoded: the endpoint window is configurable (AETHER_ENDPOINT_MS), and a
    fixed 1.0 s stopped working the moment it was raised -- leaving these tests asserting on an
    utterance that had never ended. The extra 15 frames clear webrtcvad's ~6-frame hangover.
    """
    return (mic.offset_frames + 15) * mic.frame_ms / 1000.0


def silence(seconds: float, sample_rate: int = SR_TRANSPORT) -> np.ndarray:
    return np.zeros(int(sample_rate * seconds), dtype=np.int16)


# ============================ inbound: transport → VAD/STT path ============================

@pytest.mark.parametrize("block", [480, 960, 1024, 137])
def test_any_transport_block_size_reaches_the_vad(block):
    """THE TRAP: `process_frame` silently returns on any size but 320. Rebuffering is mandatory.

    Without it a LiveKit call would deliver audio that vanished with no error, no exception and no
    log line -- the worst possible failure mode to debug on a live call.
    """
    mic = MicVAD(Trace())
    bridge = InboundBridge(mic, source_rate=SR_TRANSPORT)
    pcm = speech(1.0)

    for i in range(0, len(pcm), block):
        bridge.push(AudioChunk(pcm[i:i + block], SR_TRANSPORT))

    expected = int(1.0 * SR_VAD / mic.frame_samples)          # 1 s at 16 kHz / 320
    assert abs(bridge.frames_delivered - expected) <= 1, (
        f"block size {block} lost audio: {bridge.frames_delivered} frames, expected ~{expected}"
    )


def test_no_audio_is_lost_across_block_boundaries():
    """The carry buffer: samples left over from one block must open the next, not be dropped."""
    mic = MicVAD(Trace())
    bridge = InboundBridge(mic, source_rate=SR_TRANSPORT)

    total = 0
    for chunk in wav_to_chunks(speech(2.0), SR_TRANSPORT, block_ms=13.0):   # deliberately ragged
        total += bridge.push(chunk)

    expected = int(2.0 * SR_VAD / mic.frame_samples)
    assert abs(total - expected) <= 1, "ragged block sizes must not accumulate loss"


def test_speech_through_the_bridge_produces_an_utterance():
    """End to end inbound: transport audio in, a complete utterance out on the STT queue."""
    mic = MicVAD(Trace())
    bridge = InboundBridge(mic, source_rate=SR_TRANSPORT)

    for chunk in wav_to_chunks(silence(0.6), SR_TRANSPORT):      # let ambient settle
        bridge.push(chunk)
    for chunk in wav_to_chunks(speech(1.2), SR_TRANSPORT):
        bridge.push(chunk)
    for chunk in wav_to_chunks(silence(endpoint_silence(mic)), SR_TRANSPORT):
        bridge.push(chunk)

    assert not mic.utterances.empty(), "the STT path must be reached from transport audio"
    audio, _onset = mic.utterances.get_nowait()
    assert len(audio) > SR_VAD * 0.5, "the utterance must carry the speech, not a fragment"
    assert mic.trace.last(EventType.SPEECH_ENDED).fields["rejected"] is None


def test_a_transport_at_the_vad_rate_needs_no_resample():
    mic = MicVAD(Trace())
    bridge = InboundBridge(mic, source_rate=SR_VAD)
    for chunk in wav_to_chunks(speech(0.5, SR_VAD), SR_VAD):
        bridge.push(chunk)
    assert bridge.frames_delivered > 0


# ============================ inbound: the listening gate ============================

def test_standby_stops_transport_audio_reaching_the_vad():
    """THE SECOND TRAP: `process_frame` bypasses `_enabled`, so the bridge must check it.

    Otherwise Stop Listening would be silently dead on a phone call while appearing to work.
    """
    mic = MicVAD(Trace())
    bridge = InboundBridge(mic, source_rate=SR_TRANSPORT)
    mic.set_listening(False)

    for chunk in wav_to_chunks(speech(1.5), SR_TRANSPORT):
        bridge.push(chunk)

    assert bridge.frames_delivered == 0
    assert bridge.frames_dropped_not_listening > 0, "and it is counted, not silently ignored"
    assert mic.utterances.empty()
    assert mic.trace.all(EventType.SPEECH_ONSET) == []


def test_resuming_listening_starts_clean():
    """Audio from before a mute must never be stitched onto audio from after it."""
    mic = MicVAD(Trace())
    bridge = InboundBridge(mic, source_rate=SR_TRANSPORT)

    bridge.push(AudioChunk(speech(0.05), SR_TRANSPORT))     # partial frame into the carry
    mic.set_listening(False)
    bridge.push(AudioChunk(speech(0.05), SR_TRANSPORT))     # dropped, and clears the carry
    mic.set_listening(True)

    assert len(bridge._carry) == 0


# ============================ outbound: AudioGate → transport ============================

def test_rime_audio_reaches_the_transport():
    """End to end outbound: what the gate accepts is what the transport carries."""
    trace = Trace()
    gate = AudioGate(trace)
    out = OutboundBridge(gate, sink_rate=SR_TRANSPORT)

    gate.set_active_generation("G1", turn_id=1)
    tone = (np.sin(np.arange(gate.samplerate // 2) * 0.05) * 8000).astype(np.int16)
    assert gate.enqueue(tone, turn_id=1, gen="G1") is True

    heard = np.concatenate([out.pull().data for _ in range(20)])
    assert np.abs(heard).max() > 0, "queued Rime audio must appear on the transport"


def test_a_pull_always_returns_a_full_block_even_when_idle():
    """A phone call needs a continuous stream; a gap is heard as a dropout, not as silence."""
    gate = AudioGate(Trace())
    out = OutboundBridge(gate, sink_rate=SR_TRANSPORT)

    chunk = out.pull()
    expected = int(round(gate.blocksize * SR_TRANSPORT / gate.samplerate))
    assert chunk.samples == expected
    assert chunk.sample_rate == SR_TRANSPORT
    assert np.abs(chunk.data).max() == 0, "idle is silence, not absence"


def test_the_transport_rate_is_honoured():
    gate = AudioGate(Trace())
    for rate in (8000, 16000, 48000):
        chunk = OutboundBridge(gate, sink_rate=rate).pull()
        assert chunk.sample_rate == rate
        assert chunk.samples == int(round(gate.blocksize * rate / gate.samplerate))


# ============================ the guarantee: fencing survives the bridge ============================

def _rig():
    trace = Trace()
    gens = GenerationRegistry(trace)
    gate = AudioGate(trace)
    barge = BargeInCoordinator(trace, gens, gate, meaningful_speech_ms=300.0)
    return trace, gens, gate, barge, OutboundBridge(gate, sink_rate=SR_TRANSPORT)


def test_a_fence_mid_response_stops_transport_audio_and_leaks_nothing():
    """The whole reason for bridging instead of adopting LiveKit's own pipeline.

    A generation fenced while its audio is queued must produce silence on the phone line, and the
    trace must show zero leakage. This is the same AudioGate that protects the local speaker.
    """
    trace, gens, gate, barge, out = _rig()

    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id, turn_id=1)
    loud = (np.sin(np.arange(gate.samplerate) * 0.05) * 12000).astype(np.int16)   # 1 s of speech
    assert gate.enqueue(loud, turn_id=1, gen=gen.id) is True

    before = np.concatenate([out.pull().data for _ in range(3)])
    assert np.abs(before).max() > 0, "precondition: the caller is hearing the answer"

    barge.fence_now(reason="customer_barge_in")

    after = np.concatenate([out.pull().data for _ in range(25)])
    assert np.abs(after).max() == 0, "a fenced generation must not reach the phone line"

    assert gens.is_active(gen.id) is False
    assert gate.enqueue(loud, turn_id=1, gen=gen.id) is False, (
        "and no further stale audio may be accepted"
    )
    assert trace.all(EventType.RESULT_LEAKED) == [], "ResultLeaked must be 0"


def test_the_replacement_answer_is_carried_after_a_fence():
    """Fencing must stop the stale answer without killing the transport."""
    trace, gens, gate, barge, out = _rig()

    first = barge.begin_turn(turn_id=1)
    gate.set_active_generation(first.id, turn_id=1)
    gate.enqueue((np.sin(np.arange(gate.samplerate) * 0.05) * 12000).astype(np.int16),
                 turn_id=1, gen=first.id)
    out.pull()
    barge.fence_now(reason="customer_barge_in")
    for _ in range(25):
        out.pull()                                   # drain the flush

    barge.end_turn()
    second = barge.begin_turn(turn_id=2)
    gate.set_active_generation(second.id, turn_id=2)
    assert gate.enqueue((np.sin(np.arange(gate.samplerate // 2) * 0.07) * 9000).astype(np.int16),
                        turn_id=2, gen=second.id) is True

    heard = np.concatenate([out.pull().data for _ in range(20)])
    assert np.abs(heard).max() > 0, "the new answer must reach the caller"
    assert trace.all(EventType.RESULT_LEAKED) == []


def test_a_full_call_shaped_exchange_leaks_nothing():
    """Inbound and outbound together, with a barge-in in the middle."""
    trace, gens, gate, barge, out = _rig()
    mic = MicVAD(trace)
    inbound = InboundBridge(mic, source_rate=SR_TRANSPORT)

    # AETHER is answering.
    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id, turn_id=1)
    gate.enqueue((np.sin(np.arange(gate.samplerate) * 0.05) * 12000).astype(np.int16),
                 turn_id=1, gen=gen.id)
    out.pull()

    # The customer talks over it; the call keeps streaming both ways throughout.
    for chunk in wav_to_chunks(speech(0.8), SR_TRANSPORT):
        inbound.push(chunk)
        out.pull()
    barge.fence_now(reason="customer_barge_in")
    for chunk in wav_to_chunks(silence(endpoint_silence(mic)), SR_TRANSPORT):
        inbound.push(chunk)
        out.pull()

    assert inbound.frames_delivered > 0, "the customer was heard"
    assert not mic.utterances.empty(), "and their question reached the STT path"
    assert trace.all(EventType.RESULT_LEAKED) == [], "ResultLeaked must be 0"
    tail = np.concatenate([out.pull().data for _ in range(10)])
    assert np.abs(tail).max() == 0, "the abandoned answer is gone from the line"


# ============================ the isolation boundary ============================

def test_the_bridge_does_not_import_livekit():
    """The whole point: this is testable where the SDK is not installed.

    Telephony assumptions belong at the call site, converting `rtc.AudioFrame` to `AudioChunk`.
    """
    import io
    import tokenize

    import aether.bridge as mod

    src = open(mod.__file__, encoding="utf-8").read()
    code = " ".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    assert "livekit" not in code.lower(), "the bridge must stay transport-agnostic"
    assert "rtc" not in code.split(), "no SDK types may leak across the boundary"


def test_the_bridge_reimplements_no_audio_logic():
    """It adapts shape and rate. Everything else is called, not copied."""
    import io
    import tokenize

    import aether.bridge as mod

    src = open(mod.__file__, encoding="utf-8").read()
    code = " ".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    for forbidden in ("webrtcvad", "mark_fenced", "fence_generation", "request_duck",
                      "set_active_generation", "GenerationRegistry"):
        assert forbidden not in code, f"the bridge must not reimplement {forbidden}"
    assert "process_frame" in code and "_callback" in code, "it drives the real seams"
