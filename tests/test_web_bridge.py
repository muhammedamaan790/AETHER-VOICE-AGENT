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
    """It must not crash -- and it must not claim to be listening either.

    STRENGTHENED. The old fallback was `UiState.phase`, which defaults to "listening" and is never
    written by the fold, so a dead coordinator produced a console asserting AETHER was listening.
    A broken source now reports "error", which is the truth and which the page can render.
    """
    def explodes():
        raise RuntimeError("coordinator gone")

    snap = WebBridge(phase_source=explodes).current_state()
    assert snap["phase"] == "error"
    assert snap["interruptible"] is False, "nothing may be offered as interruptible"


def test_a_bridge_with_nothing_attached_says_so():
    """Before the phone rings there is no pipeline, and the console must not pretend otherwise."""
    snap = WebBridge().current_state()
    assert snap["call_active"] is False
    assert snap["phase"] == "no_call"
    assert snap["listening"] is False
    assert snap["interruptible"] is False


def test_listening_is_always_present_and_boolean():
    """It used to VANISH from the payload when no source was wired.

    In the browser that produced a contradiction: `orbStateFor` tests `listening === false`, which
    is false for undefined, so the orb read "Listening"; `renderControls` does `!!listening`, so the
    button read "Start Listening". The two halves of the page disagreed.
    """
    for bridge in (WebBridge(),
                   WebBridge(listening_source=lambda: True),
                   WebBridge(listening_source=lambda: (_ for _ in ()).throw(RuntimeError()))):
        value = bridge.current_state()["listening"]
        assert isinstance(value, bool), value


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
    # The injected callables now live on the frozen `Engine` so they can be swapped per call as
    # one atomic store. The guard follows them there; what it pins is unchanged -- the bridge may
    # ask for a fence and ask for the microphone, and may reach the pipeline no other way.
    assert "on_interrupt" in code and "on_standby" in code and "on_listening" in code, (
        "its only pipeline writes are the injected callables"
    )
    assert "class Engine" in code, "and they are held together, not as loose attributes"


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
    """The page is driven by the canonical vocabulary, not by invented signals.

    NARROWED when the evidence grid and event log were removed for the demo: the page no longer
    paints `previous_generation`, `fence_reason`, `last_discard` or the timeline. The bridge still
    computes and sends all of them -- `test_the_snapshot_is_json_serialisable` and the `UiState`
    fold tests pin that end -- so the data is there for a page that wants it.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for event_type in ("TranscriptFinal", "ResponseSpoken", "FenceRequested", "ResultDiscarded"):
        assert event_type in html, f"{event_type} should drive the UI"


def test_the_bridge_still_sends_what_the_page_stopped_painting():
    """Removing a panel must not quietly remove the data behind it."""
    bridge = WebBridge()
    for ev in ({"type": "GenerationChanged", "from_gen": "G1", "to_gen": "G2"},
               {"type": "FenceRequested", "gen": "G2", "reason": "button_interrupt"},
               {"type": "ResultDiscarded", "gen": "G2", "reason": "stale_generation_llm"}):
        bridge.state.apply(ev)

    snap = bridge.current_state()
    for key in ("previous_generation", "fence_reason", "last_discard", "timeline",
                "last_class", "leaks", "latency"):
        assert key in snap, f"{key} must still reach the browser"


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
    # The markup half of this test went with the evidence grid. What it protected -- the engine
    # deciding which metrics are real, rather than the page hardcoding a list -- lives here.


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
    assert snapshot["interruptible"] is False
    assert snapshot["listening"] is False, (
        "explicitly false with no source wired -- never absent, or the page contradicts itself"
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
    for key in ("transcript", "interruptible", "speech_active"):
        assert key in html, f"{key} should drive the UI"


# ============================ port binding ============================
#
# From a real call log: the websocket port was already in use, and the bare OSError traceback from
# the dying daemon thread landed in the middle of the call's output, where it read like a fault in
# the call. The `try` around `start_console` could not catch it, because it was raised on another
# thread.

def test_a_busy_port_raises_at_start_not_in_a_thread():
    """The caller must be able to catch it. A phone call continues without a UI; it must not see
    a traceback it cannot handle."""
    first = WebBridge(http_port=8796, ws_port=8797)
    first.start()
    try:
        second = WebBridge(http_port=8796, ws_port=8797)
        with pytest.raises(OSError):
            second.start()
    finally:
        first.stop()


def test_a_failed_start_releases_whatever_did_bind():
    """Half-bound is worse than not bound: it would block a retry on the other port."""
    holder = WebBridge(http_port=8798, ws_port=8799)
    holder.start()
    try:
        # Same ws port, free http port: http binds, ws fails, http must not be left holding 8800.
        clashing = WebBridge(http_port=8800, ws_port=8799)
        with pytest.raises(OSError):
            clashing.start()

        reuse = WebBridge(http_port=8800, ws_port=8801)
        reuse.start()                      # would raise if 8800 were still held
        reuse.stop()
    finally:
        holder.stop()


def test_stop_releases_both_ports_for_the_next_call():
    """Each phone call starts its own console on the same ports."""
    for _ in range(3):
        bridge = WebBridge(http_port=8802, ws_port=8803)
        bridge.start()
        bridge.stop()


# ============================ one console, many calls ============================
#
# The console used to be built and destroyed per call. That meant there was nothing to open before
# the phone rang, and the browser's socket dropped at every hangup -- which is why running a second
# `python -m aether.web` looked necessary, and that is a whole second pipeline competing for CPU
# with the live call.

class _Spike:
    """The five things the console may ask of a pipeline."""

    def __init__(self, name, phase="listening", listening=True):
        self.name = name
        self.phase = phase
        self.listening = listening
        self.interrupts = 0
        self.listen_calls = []

    def interrupt(self):
        self.interrupts += 1
        return f"G-{self.name}"

    def set_listening(self, on):
        self.listen_calls.append(on)
        self.listening = bool(on)
        return self.listening


def _attach(bridge, spike, trace=None, label="call"):
    bridge.attach(
        phase_source=lambda: spike.phase,
        listening_source=lambda: spike.listening,
        on_interrupt=spike.interrupt,
        on_listening=spike.set_listening,
        trace=trace, label=label,
    )


def test_attach_forgets_the_previous_callers_conversation():
    """THE PRIVACY ONE. Two members of the public must never see each other's words."""
    bridge = WebBridge()
    first = Trace()
    _attach(bridge, _Spike("a"), trace=first)
    first.emit(EventType.TRANSCRIPT_FINAL, turn_id=1, text="my card number is on the booking")
    bridge.state.apply(event_to_dict(first.events[-1]))
    assert bridge.current_state()["transcript"], "the first caller was recorded"

    _attach(bridge, _Spike("b"), trace=Trace())

    snap = bridge.current_state()
    assert snap["transcript"] == [], "the next caller starts from nothing"
    assert snap["timeline"] == []
    assert snap["latency"] == {}
    assert snap["leaks"] == 0
    assert snap["generation"] is None
    assert snap["last_class"] is None
    assert snap["speech_active"] is False


def test_detach_reports_no_call_rather_than_claiming_to_listen():
    bridge = WebBridge()
    spike = _Spike("a")
    _attach(bridge, spike)
    assert bridge.current_state()["call_active"] is True

    bridge.detach()
    snap = bridge.current_state()
    assert snap["call_active"] is False
    assert snap["phase"] == "no_call", "not 'listening' -- there is nothing to be listening"
    assert snap["listening"] is False
    assert snap["interruptible"] is False


def test_a_detached_console_drives_nothing():
    """Buttons on a page left open between calls must be inert, not silently effective."""
    bridge = WebBridge()
    spike = _Spike("a")
    _attach(bridge, spike)
    bridge.detach()

    bridge._handle_inbound('{"action":"interrupt"}')
    bridge._handle_inbound('{"action":"listening","on":false}')
    assert spike.interrupts == 0
    assert spike.listen_calls == []


def test_a_second_attach_rebinds_the_controls_to_the_new_call():
    """Otherwise INTERRUPT would fence call 1 while the page displayed call 2."""
    bridge = WebBridge()
    first, second = _Spike("a"), _Spike("b")
    _attach(bridge, first)
    _attach(bridge, second)

    bridge._handle_inbound('{"action":"interrupt"}')
    bridge._handle_inbound('{"action":"listening","on":false}')
    assert (first.interrupts, first.listen_calls) == (0, [])
    assert (second.interrupts, second.listen_calls) == (1, [False])


def test_attach_unsubscribes_from_the_previous_trace():
    """A leaked subscription would fold a dead call's events into the live one."""
    bridge = WebBridge()
    old_trace, new_trace = Trace(), Trace()
    _attach(bridge, _Spike("a"), trace=old_trace)
    _attach(bridge, _Spike("b"), trace=new_trace)

    assert old_trace._subscribers == [], "the old trace no longer feeds this console"
    assert bridge.on_event in new_trace._subscribers


def test_detach_is_idempotent_and_safe_with_nothing_attached():
    bridge = WebBridge()
    bridge.detach()
    bridge.detach()
    assert bridge.current_state()["call_active"] is False


def test_the_recording_path_reaches_the_snapshot(tmp_path):
    """The judge-facing proof that the conversation is on disk."""
    bridge = WebBridge()
    assert bridge.current_state()["recording"]["path"] is None

    trace = Trace(tmp_path / "run-x.jsonl")
    _attach(bridge, _Spike("a"), trace=trace)
    trace.emit(EventType.SPEECH_ONSET)
    bridge.state.apply(event_to_dict(trace.events[-1]))

    rec = bridge.current_state()["recording"]
    assert rec["path"] is not None and rec["path"].endswith("run-x.jsonl")
    assert rec["events"] == 1

    bridge.detach()
    assert bridge.current_state()["recording"]["path"] is None


def test_the_ports_bind_once_across_many_calls():
    """The browser tab must survive every hangup: no rebinding between calls."""
    bridge = WebBridge(http_port=8804, ws_port=8805)
    bridge.start()
    try:
        for name in ("a", "b", "c"):
            _attach(bridge, _Spike(name), trace=Trace())
            assert bridge.current_state()["call_active"] is True
            bridge.detach()
            assert bridge.current_state()["call_active"] is False
        with urllib.request.urlopen("http://127.0.0.1:8804/index.html", timeout=3) as resp:
            assert resp.status == 200, "still serving after three calls"
    finally:
        bridge.stop()


def test_the_ui_renders_the_waiting_and_recording_states():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "call_active" in html, "the page must distinguish no-call from listening"
    # A distinct no-call ORB STATE, not a particular wording. The copy changed with the console
    # restyle; what must not change is that the page has somewhere honest to sit before a call,
    # rather than falling through to "listening".
    assert "waiting:" in html, "the orb needs a state for 'no call yet'"
    assert 'call_active === false) return "waiting"' in html, (
        "and no-call must map to it, not to a listening state"
    )
    # The page keys off `call_active`, not off the phase string, so `no_call` is deliberately
    # absent here -- `test_detach_reports_no_call_rather_than_claiming_to_listen` pins that end.
    assert "recording" in html, "the trace path must stay visible -- it is the saved-log proof"


def test_the_conversation_is_the_largest_thing_on_the_page():
    """The demo is a conversation. It comes first in the DOM and gets the wider column."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert html.index('id="transcript"') < html.index('id="orb"'), (
        "the conversation must precede the orb, so it reads first and lays out wider"
    )
    assert "EVIDENCE" not in html and "EVENT LOG" not in html, (
        "the evidence grid and event log were removed to give the conversation the space"
    )


def test_an_idle_console_stops_re_sending_the_same_snapshot():
    """The pump ticks five times a second so live `phase` changes reach the browser with no event
    to carry them. It must not RESEND an unchanged snapshot: with the worker's DEBUG logging on,
    an idle console printed several lines a second to the terminal and buried the call's own
    output."""
    bridge = WebBridge()
    sent = []
    bridge._broadcast_raw = lambda data: sent.append(data)

    for _ in range(10):
        bridge._broadcast_state()
    assert len(sent) == 1, "the first snapshot goes out, the nine identical ones do not"

    bridge.state.apply({"type": "SpeechOnset"})
    bridge._broadcast_state()
    assert len(sent) == 2, "a real change is always delivered"


def test_attach_and_detach_always_deliver_the_next_snapshot():
    """A call starting or ending is exactly when the browser must not be left on a stale screen."""
    bridge = WebBridge()
    sent = []
    bridge._broadcast_raw = lambda data: sent.append(data)
    bridge._broadcast_state()

    _attach(bridge, _Spike("a"), trace=Trace())
    bridge._broadcast_state()
    assert len(sent) == 2

    bridge.detach()
    bridge._broadcast_state()
    assert len(sent) == 3


def test_the_worker_silences_transport_chatter():
    """`dev` mode sets the ROOT logger to DEBUG, which turns on every third-party logger too."""
    import inspect

    from aether.telephony import agent

    src = inspect.getsource(agent.start_console)
    assert "websockets" in src and "logging.WARNING" in src


def test_the_console_is_never_cached_by_the_browser():
    """It cost a real debugging round: the page had been rebuilt, the server was serving the new
    one, and the operator was looking at the old one after a normal reload.

    `SimpleHTTPRequestHandler` sends `Last-Modified` and nothing else, which leaves the browser
    free to reuse what it has. This page is small, local, and reloaded by hand between runs -- there
    is nothing to gain by caching it.
    """
    bridge = WebBridge(http_port=8806, ws_port=8807)
    bridge.start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8806/index.html", timeout=3) as resp:
            cache = resp.headers.get("Cache-Control", "")
        assert "no-store" in cache, f"the console must not be cached; got {cache!r}"
    finally:
        bridge.stop()


def test_the_page_served_is_the_page_on_disk():
    """A stale in-memory copy would be worse than a stale browser copy."""
    bridge = WebBridge(http_port=8808, ws_port=8809)
    bridge.start()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8808/index.html", timeout=3) as resp:
            served = resp.read().decode("utf-8")
    finally:
        bridge.stop()
    assert served == (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# --- console legibility and layout ------------------------------------------------------------
#
# These are presentation properties, but each one was a reported defect on a live call console:
# the operator could not scroll back through the conversation, the listening and interrupt
# controls were pushed below the fold by a long call, and the labels were too dim to read.


def _console_css() -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


def test_the_conversation_scrolls_inside_its_own_box():
    """The reported defect: a long call grew the PAGE, so scrolling back meant scrolling the whole
    console and the controls left the screen. The transcript must be the thing that scrolls."""
    css = _console_css()
    blocks = [chunk.split("}", 1)[0] for chunk in css.split("#transcript{")[1:]]
    assert blocks, "no #transcript rule at all"

    # There is deliberately more than one: the desktop rule, and a narrow-screen override where
    # the page IS meant to scroll because two stacked panels cannot share a phone screen. The
    # desktop rule is the one that owns the scrolling.
    desktop = [b for b in blocks if "overflow-y:auto" in b]
    assert len(desktop) == 1, f"expected exactly one scrolling #transcript rule, got {blocks!r}"
    block = desktop[0]

    # A box that cannot shrink cannot scroll -- it pushes the page taller instead. This is exactly
    # what `min-height:52vh` did here, so the guard is against a floor, not against min-height:0.
    assert "min-height:0" in block, "the transcript must be allowed to shrink, or it cannot scroll"
    assert "vh" not in block, f"a viewport-height floor stops the transcript scrolling: {block!r}"


def test_the_console_is_a_fixed_frame_so_the_controls_never_scroll_away():
    css = _console_css()
    shell = css.split(".shell{", 1)[1].split("}", 1)[0]
    assert "height:100vh" in shell and "min-height:100vh" not in shell, shell


def test_the_scrollbar_is_visible_rather_than_an_overlay_that_fades():
    """A reader who cannot see a scrollbar does not know there is anything above to scroll to."""
    css = _console_css()
    assert "#transcript::-webkit-scrollbar" in css
    assert "scrollbar-width:thin" in css and "scrollbar-color:" in css


def test_both_controls_are_present_and_live_beside_the_conversation():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    aside = html.split('<aside class="aside">', 1)[1].split("</aside>", 1)[0]
    assert 'id="listenBtn"' in aside and 'id="interruptBtn"' in aside
    # and they must still be wired -- a visible button that does nothing is worse than none
    assert "listenBtn" in html.split("</style>", 1)[1]
    assert "interruptBtn" in html.split("</style>", 1)[1]


def test_body_text_is_large_enough_to_read_across_a_room():
    """A demo console is read at a distance, and by a judge who is not sitting at the keyboard."""
    css = _console_css()
    bubble = css.split(".bubble{", 1)[1].split("}", 1)[0]
    size = float(bubble.split("font-size:", 1)[1].split("px", 1)[0])
    assert size >= 17, f"conversation text is {size}px, too small to read at a distance"


def test_no_text_colour_is_left_near_the_panel_background():
    """The reported defect: label text sat so close to the panel colour it was invisible. The dim
    inks carry the labels, the hint and the recording path, so they are the ones that must clear
    the background by a real margin."""
    css = _console_css()

    def rgb(token: str) -> tuple[int, int, int]:
        val = css.split(f"--{token}:", 1)[1].split(";", 1)[0].strip().lstrip("#")
        return tuple(int(val[i:i + 2], 16) for i in (0, 2, 4))

    def luminance(c: tuple[int, int, int]) -> float:
        chan = []
        for v in c:
            v = v / 255
            chan.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]

    def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
        la, lb = sorted((luminance(a), luminance(b)), reverse=True)
        return (la + 0.05) / (lb + 0.05)

    panel = rgb("panel")
    for token in ("ink", "ink-dim", "ink-faint"):
        ratio = contrast(rgb(token), panel)
        assert ratio >= 4.5, f"--{token} on --panel is {ratio:.1f}:1, below the 4.5:1 readable floor"
