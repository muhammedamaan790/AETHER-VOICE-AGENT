"""Web bridge: a read-only window onto a running AETHER engine.

The browser observes; it does not drive. The microphone and the speaker belong to the Python
process (`MicVAD` owns a PortAudio input stream, `AudioGate` owns the output), so moving capture
into the browser would mean rebuilding VAD, ducking, the gate and fencing in JavaScript and adding
a transport to carry audio. That is the opposite of leaving the pipeline alone, so the browser gets
a view instead.

Nothing here writes to the registry, the gate or the coordinator. It reads `phase` and the active
generation, and folds the canonical event stream into a snapshot. There is no LiveKit, and the
event vocabulary is untouched.

Threading:

    PortAudio / main thread ──emit()──▶ on_event()  = queue.put_nowait ONLY
                                            │
                                    (bounded queue)
                                            │
                                    pump thread ──▶ JSON ──▶ websocket clients

Serialisation and sockets happen on the pump thread. The realtime threads only ever put an object
into a queue, and a full queue drops the event rather than blocking audio.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

STATIC_DIR = Path(__file__).parent / "static"

# Bounded on purpose: a stalled browser must cost dropped UI updates, never a stalled voice loop.
EVENT_QUEUE_SIZE = 512


@dataclass
class UiState:
    """Everything the UI shows, folded from the canonical event stream.

    A pure fold: `apply()` takes an event dict and updates the snapshot. No engine objects are
    referenced, so the whole thing is testable without audio hardware, and it cannot accidentally
    write to the pipeline.
    """

    phase: str = "listening"
    generation: str | None = None
    previous_generation: str | None = None
    fenced_generation: str | None = None
    fence_reason: str | None = None
    last_discard: str | None = None          # why the most recent result was thrown away
    turn_id: int | None = None

    # Rime, as actually reported by a spoken turn -- never assumed from config.
    rime_provider: str | None = None
    rime_transport: str | None = None
    rime_model: str | None = None
    rime_voice: str | None = None
    rime_language: str | None = None

    llm_provider: str | None = None
    latency: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)   # G17 -> FENCED -> G18

    # The conversation, folded from the same canonical events. Held HERE rather than in the
    # browser so a reconnecting tab is handed the history instead of an empty screen, and so the
    # page has no second copy of the truth to drift from.
    transcript: list[dict[str, Any]] = field(default_factory=list)
    # The last interruption judgement, so the evidence strip can show it without the browser
    # having to interpret the event stream itself.
    last_class: str | None = None
    last_class_rule: str | None = None
    # Stale output that reached a listener anyway. The number the whole design exists to keep at
    # zero, so it is surfaced rather than buried in the timeline.
    leaks: int = 0
    # Whether the caller is talking RIGHT NOW, folded from the same VAD events the engine emits.
    # Held here rather than derived in the browser so the orb and the engine cannot disagree about
    # who is speaking -- there is one source of truth, and it is this one.
    speech_active: bool = False
    # How many canonical events this conversation has produced. Shown next to the trace path, so
    # the console can prove the log is being written rather than merely claiming a filename.
    event_count: int = 0

    def reset(self) -> None:
        """Forget the previous conversation entirely. Called when a new call attaches.

        There was no reset at all while the bridge died with the call. Once one console serves many
        calls, every field here becomes carryover -- and the worst of them is `transcript`, which
        would render up to sixty lines of one caller's words to the next caller. That is a privacy
        leak between two members of the public, not a cosmetic bug.

        `leaks` resets too: the golden invariant is claimed per call, and a leak in one call must
        not turn the counter red for the next.
        """
        fresh = UiState()
        for field_name in vars(fresh):
            setattr(self, field_name, getattr(fresh, field_name))

    def apply(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        self.event_count += 1

        if kind == "SpeechOnset":
            self.speech_active = True

        elif kind == "SpeechEnded":
            self.speech_active = False

        elif kind == "GenerationChanged":
            self.previous_generation = ev.get("from_gen")
            self.generation = ev.get("to_gen")
            self.turn_id = ev.get("turn_id")
            self._push_timeline("generation", ev.get("to_gen"), ev.get("from_status"))

        elif kind == "FenceRequested":
            self.fenced_generation = ev.get("gen")
            self.fence_reason = ev.get("reason")
            self._push_timeline("fenced", ev.get("gen"), ev.get("reason"))

        elif kind == "ResultDiscarded":
            self.last_discard = ev.get("reason")
            self._push_timeline("discarded", ev.get("gen"), ev.get("reason"))
            # An answer that was produced and never heard. Recorded as its own transcript entry
            # rather than silently dropped: "AETHER started answering and was cut off" is the
            # single most important thing this product has to make visible, and a UI that just
            # omitted it would present an interrupted turn as though it never happened.
            self._push_turn("aether", None, ev, status="interrupted", reason=ev.get("reason"))

        elif kind == "TranscriptFinal":
            text = (ev.get("text") or "").strip()
            if text:
                self._push_turn("customer", text, ev, status="said")

        elif kind == "InterruptionClassified":
            self.last_class = ev.get("interruption_class")
            self.last_class_rule = ev.get("rule")
            self._push_timeline("classified", ev.get("gen"), ev.get("interruption_class"))

        elif kind == "BackchannelDetected":
            self._push_timeline("backchannel", ev.get("gen"), ev.get("text"))

        elif kind == "CancellationResolved":
            self._push_timeline("cancelled", ev.get("gen"), "cancelled_by_caller")

        elif kind == "ResultLeaked":
            # RULES.md R1.4. Counted, never hidden -- a demo that quietly dropped this number
            # would be claiming a guarantee it had just broken.
            self.leaks += 1
            self._push_timeline("leaked", ev.get("gen"), ev.get("reason"))

        elif kind == "ResponseSpoken":
            self.rime_provider = ev.get("provider")
            self.rime_transport = ev.get("transport")
            self.rime_model = ev.get("model")
            self.rime_voice = ev.get("voice")
            self.rime_language = ev.get("voice_language")
            self.llm_provider = ev.get("llm_provider")
            # Only real measurements: a metric the run did not produce stays absent.
            self.latency = {
                key: ev.get(key)
                for key in ("stt_ms", "llm_ms", "tts_ms", "turn_latency_ms",
                            "response_latency_ms", "llm_ttft_ms", "llm_total_ms",
                            "llm_first_sentence_ms", "llm_thoughts_tokens")
                if ev.get(key) is not None
            }
            self._push_timeline("spoken", ev.get("gen"), None)
            self._push_turn("aether", ev.get("text"), ev, status="spoken")

    def _push_turn(self, role: str, text: str | None, ev: dict[str, Any],
                   *, status: str, reason: str | None = None) -> None:
        """Append one conversation line.

        `status` is the honest part: "said" for the caller, and for AETHER either "spoken" -- the
        gate accepted the audio, so it really was heard -- or "interrupted", meaning the words
        existed but never reached anybody. The UI must render those two differently, because
        showing an interrupted answer as a completed one is exactly the lie this product is built
        to avoid.
        """
        self.transcript.append({
            "role": role, "text": text, "status": status, "reason": reason,
            "turn_id": ev.get("turn_id"), "gen": ev.get("gen"), "seq": ev.get("seq"),
        })
        del self.transcript[:-60]

    def _push_timeline(self, kind: str, gen: str | None, detail: str | None) -> None:
        self.timeline.append({"kind": kind, "gen": gen, "detail": detail})
        del self.timeline[:-40]      # a demo needs the recent past, not the whole session

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "generation": self.generation,
            "previous_generation": self.previous_generation,
            "fenced_generation": self.fenced_generation,
            "fence_reason": self.fence_reason,
            "last_discard": self.last_discard,
            "turn_id": self.turn_id,
            "rime": {
                "provider": self.rime_provider, "transport": self.rime_transport,
                "model": self.rime_model, "voice": self.rime_voice,
                "language": self.rime_language,
            },
            "llm_provider": self.llm_provider,
            "latency": dict(self.latency),
            "timeline": list(self.timeline),
            "transcript": list(self.transcript),
            "last_class": self.last_class,
            "last_class_rule": self.last_class_rule,
            "leaks": self.leaks,
            "speech_active": self.speech_active,
            "event_count": self.event_count,
        }


def event_to_dict(ev) -> dict[str, Any]:
    """Flatten a trace Event the same way the JSONL trace does."""
    out = {"seq": ev.seq, "t": ev.t, "type": ev.type, "turn_id": ev.turn_id, "gen": ev.gen}
    out.update(ev.fields)
    return out


@dataclass(frozen=True)
class Engine:
    """The five things the console may ask of a running pipeline, plus the trace it reads.

    Frozen and swapped as ONE attribute store, because the swap happens on the entrypoint thread
    while `current_state()` runs on the pump thread and on every websocket handler thread. Rebinding
    five separate attributes would let a torn read show the new call's phase while INTERRUPT still
    fenced the old call's generation.

    `None` everywhere is the honest resting state: no call, nothing to read, nothing to drive.
    """

    phase_source: Any = None
    listening_source: Any = None
    on_interrupt: Any = None
    on_listening: Any = None
    on_standby: Any = None
    trace_path: str | None = None
    label: str | None = None

    @property
    def active(self) -> bool:
        return self.phase_source is not None


DETACHED = Engine()


class WebBridge:
    """Fans trace events out to browsers, and answers "what is the engine doing right now".

    `phase_source` is a zero-argument callable returning the coordinator's derived phase. It is
    passed in rather than reached for, so the bridge never holds a reference it could write
    through, and so it can be tested without an engine.
    """

    def __init__(self, phase_source=None, http_port: int = 8760, ws_port: int = 8761,
                 on_interrupt=None, on_standby=None, listening_source=None,
                 on_listening=None):
        # The single permitted write. A zero-argument callable, injected rather than reached for,
        # so the bridge cannot widen its own access later without this signature changing.
        # THE ONLY BINDING TO A PIPELINE, and it is one attribute so it can be swapped whole.
        #
        # Every callable here is injected rather than reached for, so the bridge still cannot
        # widen its own access: it may ask for a fence and ask for the microphone to open or
        # close, and that is all. It holds no reference to the registry, the gate or the
        # coordinator, and cannot enqueue audio or allocate a generation.
        #
        # Interrupt and listening stay separate on purpose. "Stop talking" and "stop listening"
        # are independent intentions, and one control that did both is the exact conflation that
        # once made push-to-talk require a press before the user could speak at all.
        self._engine = Engine(
            phase_source=phase_source, listening_source=listening_source,
            on_interrupt=on_interrupt, on_listening=on_listening, on_standby=on_standby,
        )
        if not any((phase_source, listening_source, on_interrupt, on_listening, on_standby)):
            # Nothing passed: the telephony worker's shape. It attaches per call instead.
            self._engine = DETACHED
        # Held BY THE BRIDGE, not monkey-patched on from the caller. One slot, cleared on detach,
        # so re-attaching cannot silently drop a subscription and leak the previous call's Trace.
        self._detach_trace = None
        # The last state payload actually sent, so an unchanged snapshot is not re-sent. Reset on
        # attach/detach so the next real change is always delivered.
        self._last_state_json: str | None = None
        self.state = UiState()
        self.http_port = http_port
        self.ws_port = ws_port
        self._queue: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self._clients: set = set()
        self._clients_lock = threading.Lock()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._http = None
        self._ws = None
        # Set once each server's `serve_forever` has actually begun. See `stop`.
        self._http_up = threading.Event()
        self._ws_up = threading.Event()
        self.dropped_events = 0

    # --- binding to a pipeline ---------------------------------------------------------

    def attach(self, *, phase_source=None, listening_source=None, on_interrupt=None,
               on_listening=None, on_standby=None, trace=None, label=None) -> None:
        """Point this console at a pipeline, and forget the previous one.

        The telephony worker calls this when a call starts, so ONE console serves every call
        instead of one being built and destroyed per call. That is what lets the page be open
        before the phone rings, and what stops the browser's socket dropping at every hangup.

        Resets the conversation first: see `UiState.reset` for why that is not optional.

        The five callables are swapped as a single frozen `Engine`, in one attribute store, because
        `current_state()` reads them from the pump thread and from every websocket handler thread.
        """
        self.detach()
        self.state.reset()
        self._last_state_json = None
        # Drop anything the previous call left queued, so its tail cannot fold into this call.
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        if trace is not None:
            self._detach_trace = trace.subscribe(self.on_event)
        self._engine = Engine(
            phase_source=phase_source, listening_source=listening_source,
            on_interrupt=on_interrupt, on_listening=on_listening, on_standby=on_standby,
            trace_path=str(getattr(trace, "path", "") or "") or None, label=label,
        )

    def detach(self) -> None:
        """Let go of the pipeline. Idempotent, and safe when nothing was ever attached.

        After this the snapshot reports `call_active: false` and `phase: "no_call"` -- not
        `"listening"`, which is what the console used to claim with no engine behind it at all.
        """
        if self._detach_trace is not None:
            try:
                self._detach_trace()
            except Exception:
                pass
            self._detach_trace = None
        self._engine = DETACHED
        self._last_state_json = None

    # --- the realtime-safe end --------------------------------------------------------

    def on_event(self, ev) -> None:
        """Trace subscriber. Runs on the emitting thread, including the audio callback.

        Deliberately the whole implementation: one `put_nowait`, and a counter when the browser
        cannot keep up. No JSON, no socket, no lock that another thread holds while doing IO.
        """
        try:
            self._queue.put_nowait(ev)
        except queue.Full:
            self.dropped_events += 1

    # --- lifecycle --------------------------------------------------------------------

    def start(self) -> None:
        """Bind both ports, THEN serve. Raises if a port is taken.

        Binding used to happen inside the worker threads, so a port already in use produced a bare
        `OSError` traceback from a dying daemon thread -- and it landed in the middle of a phone
        call's log, where it read like a fault in the call itself. Worse, the caller's `try` around
        `start()` could not catch it, because it was raised on another thread.

        Binding here means a busy port is an ordinary exception at the call site, which the
        telephony worker already handles by continuing the call without a UI.
        """
        self._running = True
        try:
            self._http = ThreadingHTTPServer(
                ("127.0.0.1", self.http_port), partial(_QuietHandler, directory=str(STATIC_DIR))
            )
            self._ws = self._bind_ws()
        except Exception:
            # Release whichever half did bind, so a retry on other ports is not blocked by this one.
            self.stop()
            raise
        self._spawn(self._serving(self._http, self._http_up), "aether-web-http")
        self._spawn(self._serving(self._ws, self._ws_up), "aether-web-ws")
        self._spawn(self._pump, "aether-web-pump")

    @staticmethod
    def _serving(server, started: threading.Event):
        """Run `serve_forever`, announcing that it has actually begun.

        `shutdown()` blocks until the serve loop notices it, and DEADLOCKS if the loop was never
        entered -- a real hazard when a call ends immediately after it starts, because the thread
        may not have been scheduled yet. `stop()` waits on this before asking for a shutdown.
        """
        def run() -> None:
            started.set()
            server.serve_forever()

        return run

    def stop(self) -> None:
        """Stop serving and RELEASE THE PORTS.

        `shutdown()` alone stops the serve loop but leaves the listening socket bound, so a second
        session -- the next phone call starting its own console -- would fail to bind and lose its
        UI with no obvious cause. Closing is separate from stopping in `http.server`, and both are
        needed.
        """
        self._running = False
        for server, started in ((self._http, self._http_up), (self._ws, self._ws_up)):
            if server is None:
                continue
            # Only ask for a shutdown once the loop is actually running; otherwise `shutdown()`
            # waits for a loop that will never report back. The wait is bounded so a wedged
            # server cannot hold up the teardown of a phone call.
            if started.wait(timeout=2.0):
                try:
                    server.shutdown()
                except Exception:
                    pass
            try:
                server.server_close()
            except Exception:
                pass
        self._http = self._ws = None
        self._http_up.clear()
        self._ws_up.clear()

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # --- WebSocket: broadcast --------------------------------------------------------

    def _bind_ws(self):
        """Bind the websocket port. Imported here because `websockets` is only needed to serve."""
        from websockets.sync.server import serve

        return serve(self._handle_client, "127.0.0.1", self.ws_port)

    def _handle_client(self, connection) -> None:
        with self._clients_lock:
            self._clients.add(connection)
        try:
            # A browser that connects mid-session must not see an empty screen.
            connection.send(json.dumps({"kind": "state", **self.current_state()}))
            for raw in connection:
                self._handle_inbound(raw)
        except Exception:
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(connection)

    def _handle_inbound(self, raw) -> None:
        """The browser's only writes into the pipeline: interrupt, and the listening toggle.

        The bridge was observation-only, and these are deliberate, narrow exceptions rather than
        the start of a control API. Exactly THREE actions are accepted:

            interrupt              fence the active task. Listening is not touched.
            listening {on: bool}   the one START/STOP toggle. No generation is touched.
            standby                the same toggle, flipped, kept for the terminal listener.

        Anything else is ignored without complaint, because an unrecognised message from a page
        someone left open must never become a pipeline action.

        They are separate on purpose. "Stop talking" and "stop listening" are independent
        intentions, and a single control that did both would recreate the bug where the user had
        to press something before the agent could hear them at all.

        What they can reach is equally narrow. Both are injected callables, so the bridge still
        holds no reference to the registry, the gate or the coordinator, and still cannot enqueue
        audio, allocate a generation or drive the gate directly. It can ask for a fence or a mute,
        and that is all. The structural guard in tests/test_web_bridge.py pins exactly that.

        Runs on the websocket thread, never on a realtime audio thread.
        """
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return

        action = message.get("action")
        engine = self._engine          # one read: the call cannot be swapped out mid-dispatch
        try:
            if action == "listening":
                if engine.on_listening is None:
                    return
                # Absent means "start". A malformed value must not silently mute the caller, so
                # anything that is not an explicit false is read as on.
                engine.on_listening(message.get("on", True) is not False)
                return
            handler = {"interrupt": engine.on_interrupt,
                       "standby": engine.on_standby}.get(action)
            if handler is None:
                return                  # unknown action, or no callback wired: ignore silently
            handler()
        except Exception:
            # A failing control must not kill the socket loop or the voice session.
            pass

    def _pump(self) -> None:
        """Drain the queue, serialise, broadcast. The only thread that touches sockets.

        The idle tick exists because `phase` and `listening` are read live from the pipeline and
        can change with no event to carry them. It re-sends only when the snapshot has ACTUALLY
        CHANGED: an idle console otherwise pushed five identical messages a second, forever, to
        every open tab -- which under the worker's DEBUG logging printed five lines a second and
        buried the call's own output.
        """
        while self._running:
            try:
                ev = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._broadcast_state()
                continue

            payload = event_to_dict(ev)
            self.state.apply(payload)
            self._broadcast({"kind": "event", "event": payload})
            self._broadcast_state()

    def _broadcast_state(self) -> None:
        """Send the snapshot, unless it is byte-identical to the one already sent."""
        message = json.dumps({"kind": "state", **self.current_state()}, default=str)
        if message == self._last_state_json:
            return
        self._last_state_json = message
        self._broadcast_raw(message)

    def current_state(self) -> dict[str, Any]:
        """Snapshot: folded event state, plus the live phase read from the coordinator."""
        snap = self.state.snapshot()
        engine = self._engine          # one read, so the whole snapshot describes one pipeline
        snap["call_active"] = engine.active
        snap["recording"] = {"path": engine.trace_path, "events": self.state.event_count}
        snap["label"] = engine.label

        # NOT `phase` defaulting to "listening". With nothing attached there is no pipeline to be
        # listening, and `UiState.phase` is never written by the fold -- so the old default made
        # the console assert that AETHER was listening when no call existed at all.
        snap["phase"] = "no_call"
        snap["listening"] = False
        if engine.phase_source is not None:
            try:
                phase = engine.phase_source()
                snap["phase"] = getattr(phase, "value", phase)
            except Exception:
                snap["phase"] = "error"
        if engine.listening_source is not None:
            try:
                # Explicitly false rather than absent. When the key vanished the orb read
                # "Listening" (undefined !== false) while the button read "Start Listening", and
                # the two halves of the page contradicted each other.
                snap["listening"] = bool(engine.listening_source())
            except Exception:
                snap["listening"] = False
        # Derived here, not in the browser, so "is there anything to interrupt?" has exactly one
        # answer. The INTERRUPT button is emphasised on this, and is a safe no-op when it is false.
        snap["interruptible"] = snap.get("phase") in ("thinking", "speaking")
        snap["dropped_events"] = self.dropped_events
        return snap

    def _broadcast(self, message: dict[str, Any]) -> None:
        self._broadcast_raw(json.dumps(message, default=str))

    def _broadcast_raw(self, data: str) -> None:
        if not self._clients:
            return
        with self._clients_lock:
            clients = tuple(self._clients)
        for client in clients:
            try:
                client.send(data)
            except Exception:
                with self._clients_lock:
                    self._clients.discard(client)


class _QuietHandler(SimpleHTTPRequestHandler):
    """Static files without logging every request over the engine's own console output."""

    def log_message(self, *args, **kwargs) -> None:  # noqa: ARG002
        pass

    def end_headers(self) -> None:
        """Never let a browser hold on to an old console.

        `SimpleHTTPRequestHandler` sends `Last-Modified` and nothing else, which leaves the browser
        free to reuse whatever it already has. That cost a real debugging round: the page had been
        rebuilt, the server was serving the new one, and the operator was looking at the old one on
        a normal reload.

        The console is a single small local page reloaded by hand between runs, so there is nothing
        to gain by caching it and a whole class of confusion to lose. `no-store` rather than
        `no-cache`, because `no-cache` still permits a stored copy to be revalidated and this page
        should simply never be stored.
        """
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()
