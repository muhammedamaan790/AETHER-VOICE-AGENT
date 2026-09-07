"""The web bridge: trace subscription, event forwarding, state folding, static serving.

The bridge is an observer. These tests hold it to that: it must never write to the pipeline, never
do IO on the emitting thread, and never invent a value the engine did not produce.

`UiState` is a pure fold over event dicts, so most of this needs no server, no sockets and no
audio hardware.
"""

from __future__ import annotations

import json
import queue
import threading
import urllib.request

import pytest

from aether.events import EventType
from aether.trace import Trace
from aether.web.server import STATIC_DIR, UiState, WebBridge, event_to_dict


# ============================ Trace.subscribe ============================

def test_a_subscriber_receives_every_event_in_order():
    trace = Trace()
    seen = []
    trace.subscribe(seen.append)

    trace.emit(EventType.SPEECH_ONSET)
    trace.emit(EventType.TRANSCRIPT_FINAL, text="hello")

    assert [e.type for e in seen] == ["SpeechOnset", "TranscriptFinal"]
    assert [e.seq for e in seen] == [1, 2]


def test_unsubscribe_stops_delivery():
    trace = Trace()
    seen = []
    unsubscribe = trace.subscribe(seen.append)

    trace.emit(EventType.SPEECH_ONSET)
    unsubscribe()
    trace.emit(EventType.SPEECH_ENDED)

    assert len(seen) == 1


def test_a_failing_subscriber_cannot_break_the_pipeline():
    """A broken UI must never take the voice agent down with it."""
    trace = Trace()
    good = []

    def explodes(_ev):
        raise RuntimeError("observer blew up")

    trace.subscribe(explodes)
    trace.subscribe(good.append)

    trace.emit(EventType.SPEECH_ONSET)

    assert len(trace.events) == 1, "the event is still recorded"
    assert len(good) == 1, "and other subscribers still receive it"


def test_subscribers_are_dispatched_outside_the_trace_lock():
    """A slow observer must not be able to hold up the next emit."""
    trace = Trace()
    reentered = []

    def reentrant(_ev):
        if len(reentered) == 0:
            reentered.append(True)
            # Would deadlock if subscribers ran while the lock was held.
            trace.emit(EventType.AUDIO_DUCKED)

    trace.subscribe(reentrant)
    trace.emit(EventType.SPEECH_ONSET)

    assert len(trace.events) == 2


def test_the_bridge_subscriber_only_enqueues():
    """The realtime contract: no JSON, no socket, no blocking on the emitting thread."""
    import inspect

    src = inspect.getsource(WebBridge.on_event)
    body = " ".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "put_nowait" in body
    for forbidden in ("json.", "send(", "dumps", "open("):
        assert forbidden not in body, f"{forbidden!r} must not run on the emitting thread"


def test_a_full_queue_drops_events_rather_than_blocking():
    """A stalled browser must cost UI updates, never a stalled voice loop."""
    bridge = WebBridge()
    bridge._queue = queue.Queue(maxsize=2)
    trace = Trace()
    trace.subscribe(bridge.on_event)

    for _ in range(6):
        trace.emit(EventType.SPEECH_ONSET)

    assert bridge.dropped_events == 4
    assert len(trace.events) == 6, "the engine kept running regardless"


# ============================ event forwarding ============================

def test_event_to_dict_flattens_like_the_jsonl_trace():
    trace = Trace()
    ev = trace.emit(EventType.RESPONSE_SPOKEN, turn_id=3, gen="G7", text="hi", provider="rime")

    payload = event_to_dict(ev)

    assert payload["type"] == "ResponseSpoken"
    assert payload["turn_id"] == 3 and payload["gen"] == "G7"
    assert payload["text"] == "hi" and payload["provider"] == "rime"
    json.dumps(payload)          # must be serialisable as-is


def test_queued_events_survive_the_hand_off(monkeypatch):
    bridge = WebBridge()
    trace = Trace()
    trace.subscribe(bridge.on_event)

    trace.emit(EventType.TRANSCRIPT_FINAL, text="what is the capital of France")

    ev = bridge._queue.get_nowait()
    assert event_to_dict(ev)["text"] == "what is the capital of France"


# ============================ state folding ============================

def test_generation_change_records_current_and_previous():
    state = UiState()
    state.apply({"type": "GenerationChanged", "from_gen": "G17", "to_gen": "G18", "turn_id": 18})

    assert state.generation == "G18"
    assert state.previous_generation == "G17"
    assert state.turn_id == 18


def test_a_fence_is_recorded_with_its_reason():
    state = UiState()
    state.apply({"type": "FenceRequested", "gen": "G17", "reason": "meaningful_interruption"})

    assert state.fenced_generation == "G17"
    assert state.fence_reason == "meaningful_interruption"


def test_a_discard_records_why():
    state = UiState()
    state.apply({"type": "ResultDiscarded", "gen": "G17", "reason": "stale_generation_llm"})

    assert state.last_discard == "stale_generation_llm"


def test_response_spoken_populates_rime_and_latency():
    state = UiState()
    state.apply({
        "type": "ResponseSpoken", "gen": "G18", "provider": "rime", "transport": "ws3",
        "model": "mistv2", "voice": "astra", "llm_provider": "gemini:gemini-3.8-flash",
        "stt_ms": 820.0, "llm_ms": 2900.0, "tts_ms": 650.0, "turn_latency_ms": 4370.0,
        "llm_ttft_ms": 1714.0,
    })

    snap = state.snapshot()
    assert snap["rime"] == {"provider": "rime", "transport": "ws3",
                            "model": "mistv2", "voice": "astra"}
    assert snap["llm_provider"] == "gemini:gemini-3.8-flash"
    assert snap["latency"]["llm_ttft_ms"] == 1714.0
    assert snap["latency"]["turn_latency_ms"] == 4370.0


def test_absent_metrics_are_omitted_not_zeroed():
    """A metric the run did not produce must not appear as 0."""
    state = UiState()
    state.apply({"type": "ResponseSpoken", "gen": "G1", "stt_ms": 800.0,
                 "llm_ms": None, "llm_ttft_ms": None})

    latency = state.snapshot()["latency"]
    assert latency == {"stt_ms": 800.0}
    assert "llm_ttft_ms" not in latency and "llm_ms" not in latency


def test_a_fresh_state_invents_nothing():
    snap = UiState().snapshot()
    assert snap["generation"] is None and snap["previous_generation"] is None
    assert snap["latency"] == {} and snap["timeline"] == []
    assert snap["rime"] == {"provider": None, "transport": None, "model": None, "voice": None}


def test_the_timeline_shows_generation_fence_generation():
    """The story the demo has to tell: G17 -> FENCED -> G18."""
    state = UiState()
    state.apply({"type": "GenerationChanged", "to_gen": "G17"})
    state.apply({"type": "FenceRequested", "gen": "G17", "reason": "meaningful_interruption"})
    state.apply({"type": "ResultDiscarded", "gen": "G17", "reason": "stale_generation_llm"})
    state.apply({"type": "GenerationChanged", "from_gen": "G17", "to_gen": "G18"})
    state.apply({"type": "ResponseSpoken", "gen": "G18", "text": "the new answer"})

    kinds = [(s["kind"], s["gen"]) for s in state.snapshot()["timeline"]]
    assert kinds == [
        ("generation", "G17"), ("fenced", "G17"), ("discarded", "G17"),
        ("generation", "G18"), ("spoken", "G18"),
    ]


def test_the_timeline_is_bounded():
    state = UiState()
    for i in range(120):
        state.apply({"type": "GenerationChanged", "to_gen": f"G{i}"})
    assert len(state.snapshot()["timeline"]) == 40


def test_phase_comes_from_the_coordinator_not_from_events():
    bridge = WebBridge(phase_source=lambda: "speaking")
    assert bridge.current_state()["phase"] == "speaking"


def test_an_enum_phase_is_serialised_by_value():
    from aether.interruption import Phase

    bridge = WebBridge(phase_source=lambda: Phase.INTERRUPTED)
    snap = bridge.current_state()
    assert snap["phase"] == "interrupted"
    json.dumps(snap)


def test_a_broken_phase_source_does_not_break_the_snapshot():
    def explodes():
        raise RuntimeError("coordinator gone")

    snap = WebBridge(phase_source=explodes).current_state()
    assert snap["phase"] == "listening", "falls back to the folded default rather than crashing"


def test_the_bridge_never_writes_to_the_pipeline():
    """Structural guard: an observer must not be able to fence, enqueue or allocate."""
    import inspect

    import aether.web.server as mod

    code = " ".join(line.split("#", 1)[0] for line in inspect.getsource(mod).splitlines())
    for forbidden in ("mark_fenced", "fence_generation", "allocate(", "enqueue(",
                      "request_stop", "request_duck", "set_active_generation"):
        assert forbidden not in code, f"the bridge must never call {forbidden}"


# ============================ static serving ============================

def test_the_static_ui_exists_and_is_self_contained():
    index = STATIC_DIR / "index.html"
    assert index.exists()
    html = index.read_text(encoding="utf-8")

    assert "livekit" not in html.lower(), "no LiveKit may enter the core demo"
    assert "industryDD" not in html and "voiceDD" not in html, "pickers were removed"
    for phase in ("listening", "thinking", "speaking", "interrupted", "recovering"):
        assert phase in html, f"the UI must render the {phase} phase"


def test_the_ui_reacts_to_real_canonical_events():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for event_type in ("TranscriptFinal", "ResponseSpoken", "FenceRequested", "ResultDiscarded"):
        assert event_type in html, f"{event_type} should drive the UI"
    for key in ("previous_generation", "fence_reason", "last_discard", "timeline"):
        assert key in html, f"{key} should be rendered"


def test_the_latency_whitelist_lives_in_the_bridge_not_the_markup():
    """The UI renders whatever latency keys arrive, so the engine decides which are real.

    That keeps the markup from hardcoding a metric that the engine may not produce.
    """
    import inspect

    import aether.web.server as mod

    src = inspect.getsource(mod.UiState.apply)
    for metric in ("stt_ms", "llm_ms", "tts_ms", "turn_latency_ms",
                   "llm_ttft_ms", "llm_total_ms"):
        assert metric in src, f"{metric} should be forwarded to the UI"

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "Object.entries(latency" in html, "the UI renders the keys it is given, not a fixed list"


@pytest.fixture
def running_bridge():
    bridge = WebBridge(http_port=8791, ws_port=8792, phase_source=lambda: "listening")
    bridge.start()
    deadline = threading.Event()
    deadline.wait(0.4)          # let the sockets bind
    yield bridge
    bridge.stop()


def test_the_http_server_serves_the_ui(running_bridge):
    with urllib.request.urlopen("http://127.0.0.1:8791/index.html", timeout=3) as resp:
        body = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "AETHER" in body and "transcript" in body
