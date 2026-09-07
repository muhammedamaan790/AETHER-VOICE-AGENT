"""Rime /ws3 streaming TTS.

Protocol under test (verified against docs.rime.ai/docs/websockets and the mistv3
websockets-json reference):

    send      {"text": "..."}          (no eos -- it closes the connection; see the test below)
    interrupt {"operation": "clear"}
    recv      {"type":"chunk","data":<base64>} | {"type":"timestamps",...}
              | {"type":"done"} | {"type":"error","message":...}

A fake socket replays that exact protocol, so these are deterministic and need no network.

Fencing semantics are NOT re-implemented here: the gate remains the final authority and still
refuses a stale generation. The streaming client only stops feeding it early.
"""

from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from aether.audio.rime_ws import RimeHttpSpeaker, RimeStreamingTTS, build_tts
from aether.config import RimeConfig
from aether.trace import Trace

CFG = RimeConfig(
    api_key="test-key-not-real",
    api_url="https://example.invalid/tts",
    model="mistv2",
    voice="astra",
    language="eng",
)


def pcm_chunk(n_samples: int, value: int = 1000) -> str:
    return base64.b64encode(np.full(n_samples, value, dtype="<i2").tobytes()).decode()


class FakeWS:
    """Replays a scripted /ws3 server and records what the client sent."""

    def __init__(self, script):
        self._script = list(script)
        self.sent: list[dict] = []
        self.closed = False

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        if not self._script:
            raise TimeoutError("no more scripted frames")
        return self._script.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False

    # convenience for assertions
    @property
    def operations(self):
        return [m.get("operation") for m in self.sent if "operation" in m]


class CollectingGate:
    samplerate = 48000

    def __init__(self, active_gen="G1", refuse=False):
        self.active_gen = active_gen
        self.refuse = refuse
        self.chunks: list[tuple[str | None, int]] = []   # (gen, n_samples)

    def enqueue(self, pcm, *, turn_id=None, gen=None):
        if self.refuse or (gen is not None and gen != self.active_gen):
            return False
        self.chunks.append((gen, len(pcm)))
        return True

    @property
    def total_samples(self):
        return sum(n for _, n in self.chunks)


def make_client(monkeypatch, script):
    ws = FakeWS(script)
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *a, **k: ws,
    )
    return RimeStreamingTTS(Trace(), config=CFG, samplerate=48000), ws


# --- 1: chunks enqueue immediately ------------------------------------------------------

def test_chunks_are_enqueued_as_they_arrive(monkeypatch):
    """Audio reaches the gate per chunk, not batched at the end."""
    script = [
        json.dumps({"type": "chunk", "data": pcm_chunk(240)}),
        json.dumps({"type": "chunk", "data": pcm_chunk(240)}),
        json.dumps({"type": "chunk", "data": pcm_chunk(240)}),
        json.dumps({"type": "done"}),
    ]
    tts, ws = make_client(monkeypatch, script)
    gate = CollectingGate()

    result = tts.speak("hello", gate=gate, gen="G1", turn_id=1)

    assert len(gate.chunks) == 3, "each chunk must be enqueued separately, not concatenated"
    assert gate.total_samples == 720
    assert result.accepted and result.completed
    assert result.first_audio_ms is not None, "first-audio latency must be recorded"


def test_verified_protocol_is_what_gets_sent(monkeypatch):
    tts, ws = make_client(monkeypatch, [json.dumps({"type": "done"})])
    tts.speak("say this", gate=CollectingGate(), gen="G1")

    assert ws.sent[0] == {"text": "say this"}


def test_eos_is_not_sent_because_it_closes_the_connection(monkeypatch):
    """Measured 2026-09-06: eos makes the server close the socket (1005), forcing a fresh ~920 ms
    handshake on the next turn. The utterance still completes with "done" without it."""
    tts, ws = make_client(monkeypatch, [json.dumps({"type": "done"})])
    tts.speak("say this", gate=CollectingGate(), gen="G1")

    assert "eos" not in ws.operations


def test_connection_is_reused_across_turns(monkeypatch):
    """The handshake (~920 ms) cost nearly twice the synthesis (~520 ms). Pay it once."""
    ws = FakeWS([json.dumps({"type": "chunk", "data": pcm_chunk(100)}),
                 json.dumps({"type": "done"}),
                 json.dumps({"type": "chunk", "data": pcm_chunk(100)}),
                 json.dumps({"type": "done"})])
    connects = {"n": 0}

    def fake_connect(*a, **k):
        connects["n"] += 1
        return ws

    monkeypatch.setattr("websockets.sync.client.connect", fake_connect)
    tts = RimeStreamingTTS(Trace(), config=CFG, samplerate=48000)
    gate = CollectingGate()

    tts.speak("first", gate=gate, gen="G1")
    assert tts.last_reused_connection is False
    tts.speak("second", gate=gate, gen="G1")

    assert connects["n"] == 1, "the second turn must not re-handshake"
    assert tts.last_reused_connection is True
    assert len(gate.chunks) == 2


def test_a_dead_cached_connection_reconnects_instead_of_losing_the_turn(monkeypatch):
    dead = FakeWS([])
    def dead_send(raw):
        raise ConnectionError("socket closed while idle")
    dead.send = dead_send
    good = FakeWS([json.dumps({"type": "chunk", "data": pcm_chunk(100)}),
                   json.dumps({"type": "done"})])
    # `dead` is pre-cached, so the reconnect must hand back `good`.
    seq = [good]
    monkeypatch.setattr("websockets.sync.client.connect", lambda *a, **k: seq.pop(0))

    tts = RimeStreamingTTS(Trace(), config=CFG, samplerate=48000)
    tts._ws = dead                      # pretend a stale connection was cached
    gate = CollectingGate()

    result = tts.speak("hello", gate=gate, gen="G1")

    assert result.accepted is True, "a stale cache must cost a reconnect, not the turn"
    assert len(gate.chunks) == 1


def test_interruption_drops_the_cached_connection(monkeypatch):
    """After `clear` the socket's state is uncertain, so it is not reused."""
    script = [json.dumps({"type": "chunk", "data": pcm_chunk(100)}) for _ in range(6)]
    tts, ws = make_client(monkeypatch, script)
    gate = CollectingGate()

    tts.speak("long", gate=gate, gen="G1", is_valid=lambda: len(gate.chunks) < 2)

    assert "clear" in ws.operations
    assert tts._ws is None, "a cleared connection must not be handed to the next turn"


def test_connection_url_requests_pcm_at_the_gate_sample_rate(monkeypatch):
    tts, _ = make_client(monkeypatch, [json.dumps({"type": "done"})])
    url = tts._url()
    assert url.startswith("wss://users-ws.rime.ai/ws3?")
    assert "audioFormat=pcm" in url, "PCM avoids MP3 decoding entirely"
    assert "samplingRate=48000" in url, "matching the gate avoids resampling"
    assert "speaker=astra" in url and "modelId=mistv2" in url and "lang=eng" in url


# --- 2: generation tagging ---------------------------------------------------------------

def test_every_chunk_is_tagged_with_the_current_generation(monkeypatch):
    script = [
        json.dumps({"type": "chunk", "data": pcm_chunk(100)}),
        json.dumps({"type": "chunk", "data": pcm_chunk(100)}),
        json.dumps({"type": "done"}),
    ]
    tts, _ = make_client(monkeypatch, script)
    gate = CollectingGate(active_gen="G7")

    tts.speak("hi", gate=gate, gen="G7", turn_id=3)

    assert [g for g, _ in gate.chunks] == ["G7", "G7"]


def test_gate_refuses_chunks_tagged_with_a_stale_generation(monkeypatch):
    """The gate stays the final authority: mis-tagged audio never plays."""
    script = [
        json.dumps({"type": "chunk", "data": pcm_chunk(100)}),
        json.dumps({"type": "done"}),
    ]
    tts, ws = make_client(monkeypatch, script)
    gate = CollectingGate(active_gen="G2")   # G1 is no longer active

    result = tts.speak("hi", gate=gate, gen="G1")

    assert gate.chunks == [], "the gate must refuse a stale generation's audio"
    assert result.accepted is False
    assert result.reason == "gate_refused"
    assert "clear" in ws.operations, "and Rime is told to stop synthesising"


# --- 3: interruption / clear stops stale chunks -------------------------------------------

def test_fence_midstream_sends_clear_and_stops_feeding(monkeypatch):
    """The verified interrupt command is sent and no further audio is enqueued."""
    script = [json.dumps({"type": "chunk", "data": pcm_chunk(100)}) for _ in range(10)]
    script.append(json.dumps({"type": "done"}))
    tts, ws = make_client(monkeypatch, script)
    gate = CollectingGate()

    valid = {"ok": True}

    def is_valid():
        # Stays valid for the first two chunks, then the user interrupts.
        if len(gate.chunks) >= 2:
            valid["ok"] = False
        return valid["ok"]

    result = tts.speak("a long sentence", gate=gate, gen="G1", is_valid=is_valid)

    assert len(gate.chunks) == 2, "feeding must stop at the fence, not drain all 10 chunks"
    assert ws.operations[-1] == "clear", 'the verified interrupt is {"operation": "clear"}'
    assert result.completed is False
    assert result.reason == "fenced_midstream"
    assert result.accepted is True, "audio genuinely played before the interruption"


def test_no_clear_is_sent_on_a_normal_completion(monkeypatch):
    script = [
        json.dumps({"type": "chunk", "data": pcm_chunk(100)}),
        json.dumps({"type": "done"}),
    ]
    tts, ws = make_client(monkeypatch, script)

    tts.speak("hi", gate=CollectingGate(), gen="G1")

    assert "clear" not in ws.operations


# --- 4: normal completion -----------------------------------------------------------------

def test_normal_completion_reports_completed_and_all_audio(monkeypatch):
    script = [
        json.dumps({"type": "chunk", "data": pcm_chunk(480)}),
        json.dumps({"type": "timestamps", "word_timestamps": {"words": ["hi"], "start": [0.0]}}),
        json.dumps({"type": "chunk", "data": pcm_chunk(480)}),
        json.dumps({"type": "done"}),
    ]
    tts, _ = make_client(monkeypatch, script)
    gate = CollectingGate()

    result = tts.speak("hi there", gate=gate, gen="G1")

    assert result.completed is True
    assert result.accepted is True
    assert result.samples == 960, "timestamps frames carry no audio and must be ignored"
    assert gate.total_samples == 960


def test_odd_byte_chunks_never_split_a_sample(monkeypatch):
    """Rime chops PCM on arbitrary byte boundaries; a dangling byte carries to the next chunk."""
    raw = np.arange(100, dtype="<i2").tobytes()
    script = [
        json.dumps({"type": "chunk", "data": base64.b64encode(raw[:51]).decode()}),   # odd
        json.dumps({"type": "chunk", "data": base64.b64encode(raw[51:]).decode()}),
        json.dumps({"type": "done"}),
    ]
    tts, _ = make_client(monkeypatch, script)
    gate = CollectingGate()

    result = tts.speak("x", gate=gate, gen="G1")

    assert result.samples == 100, "no sample may be lost or split across the boundary"


# --- 5: failures must not crash the session ------------------------------------------------

def test_connection_failure_returns_cleanly(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("websockets.sync.client.connect", boom)
    tts = RimeStreamingTTS(Trace(), config=CFG)

    result = tts.speak("hi", gate=CollectingGate(), gen="G1")   # must not raise

    assert result.accepted is False
    assert result.reason == "OSError"


def test_rime_error_frame_is_handled(monkeypatch):
    tts, _ = make_client(monkeypatch, [json.dumps({"type": "error", "message": "bad speaker"})])

    result = tts.speak("hi", gate=CollectingGate(), gen="G1")

    assert result.accepted is False
    assert result.reason.startswith("rime_error")


def test_recv_timeout_is_handled(monkeypatch):
    tts, _ = make_client(monkeypatch, [])   # server sends nothing

    result = tts.speak("hi", gate=CollectingGate(), gen="G1")

    assert result.accepted is False
    assert result.reason == "recv_timeout"


def test_unconfigured_still_raises_rather_than_failing_silently(monkeypatch):
    from aether.audio.rime import RimeNotConfigured

    tts = RimeStreamingTTS(Trace(), config=RimeConfig())
    assert not tts.configured
    with pytest.raises(RimeNotConfigured):
        tts.speak("hi", gate=CollectingGate(), gen="G1")


# --- transport selection --------------------------------------------------------------------

def test_streaming_is_the_default_transport(monkeypatch):
    monkeypatch.delenv("RIME_TRANSPORT", raising=False)
    tts = build_tts(Trace())
    assert isinstance(tts, RimeStreamingTTS)
    assert tts.transport == "ws3"


def test_http_fallback_is_explicitly_selectable(monkeypatch):
    monkeypatch.setenv("RIME_TRANSPORT", "http")
    tts = build_tts(Trace())
    assert isinstance(tts, RimeHttpSpeaker)
    assert tts.transport == "http"


def test_both_transports_report_rime_as_the_provider(monkeypatch):
    monkeypatch.delenv("RIME_TRANSPORT", raising=False)
    ws = build_tts(Trace())
    monkeypatch.setenv("RIME_TRANSPORT", "http")
    http = build_tts(Trace())
    assert ws.name == "rime" and http.name == "rime", "Rime remains the sole TTS provider"
    assert ws.transport != http.transport, "but which transport spoke is observable"
