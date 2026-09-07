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


def test_the_bridge_may_only_ask_for_a_fence_or_a_mute():
    """Structural guard, NARROWED (not removed) when the interrupt button landed.

    The bridge was observation-only. Push-to-talk gave it exactly one write: request an interrupt.
    The guard therefore changes from "may not write at all" to "may request a fence, and may do
    nothing else" -- which is still worth pinning, because the failure mode is a control API
    growing one convenient method at a time.

    It must still be unable to fence directly, enqueue audio, allocate a generation, or drive the
    gate. It reaches the pipeline only through the injected `on_interrupt` callable.
    """
    import inspect

    import aether.web.server as mod

    code = " ".join(line.split("#", 1)[0] for line in inspect.getsource(mod).splitlines())
    for forbidden in ("mark_fenced", "fence_generation", "allocate(", "enqueue(",
                      "request_stop", "request_duck", "set_active_generation", "set_listening"):
        assert forbidden not in code, f"the bridge must never call {forbidden} itself"
    assert "_on_interrupt" in code and "_on_standby" in code, (
        "its only pipeline writes are the two injected callables"
    )


def test_only_the_two_known_actions_are_accepted():
    """An unrecognised message from a stale open tab must never become a pipeline action."""
    interrupts, standbys = [], []
    bridge = WebBridge(on_interrupt=lambda: interrupts.append(1),
                       on_standby=lambda: standbys.append(1))

    bridge._handle_inbound('{"action":"interrupt"}')
    bridge._handle_inbound('{"action":"standby"}')
    assert (len(interrupts), len(standbys)) == (1, 1)

    for payload in ['{"action":"speak"}', '{"action":"fence"}', '{"action":"mute"}', "not json",
                    "[]", '{"cmd":"interrupt"}', '{}', '"interrupt"', 'null']:
        bridge._handle_inbound(payload)
    assert (len(interrupts), len(standbys)) == (1, 1), (
        "only the two known actions may reach the pipeline"
    )


def test_interrupt_and_standby_are_separate_controls():
    """They mean different things -- one must never trigger the other.

    Conflating "stop talking" with "stop listening" is the bug that made push-to-talk require a
    press before the user could speak at all.
    """
    interrupts, standbys = [], []
    bridge = WebBridge(on_interrupt=lambda: interrupts.append(1),
                       on_standby=lambda: standbys.append(1))

    bridge._handle_inbound('{"action":"interrupt"}')
    assert (len(interrupts), len(standbys)) == (1, 0), "interrupt must not touch listening"

    bridge._handle_inbound('{"action":"standby"}')
    assert (len(interrupts), len(standbys)) == (1, 1), "standby must not fence"


def test_a_failing_standby_does_not_break_the_socket_loop():
    def explodes():
        raise RuntimeError("mic gone")

    WebBridge(on_standby=explodes)._handle_inbound('{"action":"standby"}')   # must not raise


def test_the_snapshot_reports_the_real_mic_state():
    """The UI must show whether the user is actually being heard, not what it assumed."""
    bridge = WebBridge(listening_source=lambda: False)
    assert bridge.current_state()["listening"] is False
    assert WebBridge(listening_source=lambda: True).current_state()["listening"] is True


def test_a_bridge_with_no_interrupt_callback_ignores_the_action():
    bridge = WebBridge()          # observation-only, as before
    bridge._handle_inbound('{"action":"interrupt"}')   # must not raise


def test_a_failing_interrupt_does_not_break_the_socket_loop():
    def explodes():
        raise RuntimeError("pipeline gone")

    bridge = WebBridge(on_interrupt=explodes)
    bridge._handle_inbound('{"action":"interrupt"}')   # must not raise


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


# ============================ transcript and reconnect ============================
#
# The transcript lives in `UiState`, not in the browser. That is what lets a reconnecting tab be
# handed the conversation instead of an empty screen, and it is why there is no second copy of the
# truth to drift out of step.

def fold(*events) -> UiState:
    state = UiState()
    for ev in events:
        state.apply(ev)
    return state


def test_a_customer_utterance_becomes_a_transcript_line():
    state = fold({"type": "TranscriptFinal", "text": "what starters do you have",
                  "turn_id": 1, "gen": None, "seq": 3})
    assert state.transcript == [{
        "role": "customer", "text": "what starters do you have", "status": "said",
        "reason": None, "turn_id": 1, "gen": None, "seq": 3,
    }]


def test_an_empty_transcript_is_not_shown_as_a_turn():
    """A noise burst that transcribed to nothing is not something the caller said."""
    assert fold({"type": "TranscriptFinal", "text": "   "}).transcript == []


def test_a_spoken_answer_and_an_interrupted_one_are_distinguishable():
    """The single most important thing this UI has to make visible."""
    state = fold(
        {"type": "TranscriptFinal", "text": "what starters do you have", "turn_id": 1},
        {"type": "ResponseSpoken", "text": "For starters we have...", "turn_id": 1, "gen": "G1"},
        {"type": "TranscriptFinal", "text": "what desserts do you have", "turn_id": 2},
        {"type": "ResultDiscarded", "reason": "stale_generation_llm", "turn_id": 2, "gen": "G2"},
    )
    assert [row["role"] for row in state.transcript] == [
        "customer", "aether", "customer", "aether"]
    assert state.transcript[1]["status"] == "spoken"
    assert state.transcript[3]["status"] == "interrupted"
    assert state.transcript[3]["reason"] == "stale_generation_llm"
    assert state.transcript[3]["text"] is None, (
        "an interrupted answer has no spoken text; inventing one would present a turn that "
        "never happened"
    )


def test_the_interruption_class_reaches_the_evidence_strip():
    state = fold({"type": "InterruptionClassified", "interruption_class": "BACKCHANNEL",
                  "rule": "closed_set", "gen": "G1"})
    assert state.last_class == "BACKCHANNEL"
    assert state.last_class_rule == "closed_set"
    assert state.snapshot()["last_class"] == "BACKCHANNEL"


def test_a_stale_leak_is_counted_and_surfaced():
    """RULES.md R1.4. The number the whole design exists to keep at zero is never hidden."""
    assert fold().snapshot()["leaks"] == 0
    state = fold({"type": "ResultLeaked", "gen": "G1", "reason": "test"})
    assert state.snapshot()["leaks"] == 1


def test_who_is_speaking_is_folded_from_the_engine_not_guessed_by_the_page():
    state = fold({"type": "SpeechOnset"})
    assert state.snapshot()["speech_active"] is True
    state.apply({"type": "SpeechEnded"})
    assert state.snapshot()["speech_active"] is False


def test_the_transcript_is_bounded():
    """A long call must not grow the snapshot without limit."""
    state = UiState()
    for i in range(200):
        state.apply({"type": "TranscriptFinal", "text": f"line {i}", "turn_id": i})
    assert len(state.transcript) == 60
    assert state.transcript[-1]["text"] == "line 199"


def test_a_reconnecting_browser_is_handed_the_conversation_not_an_empty_screen():
    """The snapshot a new socket receives must carry the history the engine already has."""
    bridge = WebBridge()
    for ev in ({"type": "TranscriptFinal", "text": "what starters do you have", "turn_id": 1},
               {"type": "ResponseSpoken", "text": "For starters we have...", "gen": "G1"}):
        bridge.state.apply(ev)

    snapshot = bridge.current_state()
    assert len(snapshot["transcript"]) == 2
    assert snapshot["transcript"][0]["text"] == "what starters do you have"
    assert "interruptible" in snapshot and "listening" not in snapshot, (
        "listening is only reported when a real source is wired -- never assumed"
    )


def test_the_snapshot_is_json_serialisable():
    """It crosses a websocket. A value that cannot be serialised would silently break the UI."""
    bridge = WebBridge(phase_source=lambda: "speaking", listening_source=lambda: True)
    bridge.state.apply({"type": "TranscriptFinal", "text": "hello", "turn_id": 1})
    json.dumps({"kind": "state", **bridge.current_state()})


def test_the_ui_renders_the_one_toggle_and_a_separate_interrupt():
    """Never both START and STOP at once, and never Interrupt as the way to begin speaking."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "Start Listening" in html and "Stop Listening" in html
    assert 'id="listenBtn"' in html and 'id="interruptBtn"' in html
    assert html.count('id="listenBtn"') == 1, "exactly one listening toggle"
    assert "push to talk" not in html.lower() and "push-to-talk" not in html.lower()
    for key in ("transcript", "interruptible", "last_class", "leaks", "speech_active"):
        assert key in html, f"{key} should drive the UI"
