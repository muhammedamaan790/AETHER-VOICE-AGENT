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

    llm_provider: str | None = None
    latency: dict[str, Any] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)   # G17 -> FENCED -> G18

    def apply(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")

        if kind == "GenerationChanged":
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

        elif kind == "ResponseSpoken":
            self.rime_provider = ev.get("provider")
            self.rime_transport = ev.get("transport")
            self.rime_model = ev.get("model")
            self.rime_voice = ev.get("voice")
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
            },
            "llm_provider": self.llm_provider,
            "latency": dict(self.latency),
            "timeline": list(self.timeline),
        }


def event_to_dict(ev) -> dict[str, Any]:
    """Flatten a trace Event the same way the JSONL trace does."""
    out = {"seq": ev.seq, "t": ev.t, "type": ev.type, "turn_id": ev.turn_id, "gen": ev.gen}
    out.update(ev.fields)
    return out


class WebBridge:
    """Fans trace events out to browsers, and answers "what is the engine doing right now".

    `phase_source` is a zero-argument callable returning the coordinator's derived phase. It is
    passed in rather than reached for, so the bridge never holds a reference it could write
    through, and so it can be tested without an engine.
    """

    def __init__(self, phase_source=None, http_port: int = 8760, ws_port: int = 8761,
                 on_interrupt=None, on_standby=None, listening_source=None):
        # The single permitted write. A zero-argument callable, injected rather than reached for,
        # so the bridge cannot widen its own access later without this signature changing.
        self._on_interrupt = on_interrupt
        # Second, and still injected rather than reached for. Standby mutes the microphone; it is
        # NOT a fence and touches no generation. Kept separate from `on_interrupt` so the bridge
        # cannot conflate "stop talking" with "stop listening" -- the exact conflation that made
        # push-to-talk require a press before the user could speak at all.
        self._on_standby = on_standby
        # Read-only, like phase: the UI must show whether the mic is ACTUALLY open, never just
        # what it optimistically assumed after a click.
        self._listening_source = listening_source
        self.state = UiState()
        self.http_port = http_port
        self.ws_port = ws_port
        self._phase_source = phase_source
        self._queue: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self._clients: set = set()
        self._clients_lock = threading.Lock()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._http = None
        self._ws = None
        self.dropped_events = 0

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
        self._running = True
        self._spawn(self._serve_http, "aether-web-http")
        self._spawn(self._serve_ws, "aether-web-ws")
        self._spawn(self._pump, "aether-web-pump")

    def stop(self) -> None:
        self._running = False
        if self._http is not None:
            try:
                self._http.shutdown()
            except Exception:
                pass
        if self._ws is not None:
            try:
                self._ws.shutdown()
            except Exception:
                pass

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # --- HTTP: one static directory ---------------------------------------------------

    def _serve_http(self) -> None:
        handler = partial(_QuietHandler, directory=str(STATIC_DIR))
        self._http = ThreadingHTTPServer(("127.0.0.1", self.http_port), handler)
        self._http.serve_forever()

    # --- WebSocket: broadcast --------------------------------------------------------

    def _serve_ws(self) -> None:
        from websockets.sync.server import serve

        self._ws = serve(self._handle_client, "127.0.0.1", self.ws_port)
        self._ws.serve_forever()

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
        """The browser's only writes into the pipeline: interrupt, and standby.

        The bridge was observation-only, and these are deliberate, narrow exceptions rather than
        the start of a control API. Exactly TWO actions are accepted -- `interrupt` (fence the
        active task) and `standby` (mute the mic) -- and anything else is ignored without
        complaint, because an unrecognised message from a page someone left open must never become
        a pipeline action.

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

        handler = {"interrupt": self._on_interrupt, "standby": self._on_standby}.get(
            message.get("action")
        )
        if handler is None:
            return                      # unknown action, or no callback wired: ignore silently
        try:
            handler()
        except Exception:
            # A failing control must not kill the socket loop or the voice session.
            pass

    def _pump(self) -> None:
        """Drain the queue, serialise, broadcast. The only thread that touches sockets."""
        while self._running:
            try:
                ev = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._broadcast({"kind": "state", **self.current_state()})
                continue

            payload = event_to_dict(ev)
            self.state.apply(payload)
            self._broadcast({"kind": "event", "event": payload})
            self._broadcast({"kind": "state", **self.current_state()})

    def current_state(self) -> dict[str, Any]:
        """Snapshot: folded event state, plus the live phase read from the coordinator."""
        snap = self.state.snapshot()
        if self._phase_source is not None:
            try:
                phase = self._phase_source()
                snap["phase"] = getattr(phase, "value", phase)
            except Exception:
                pass
        if self._listening_source is not None:
            try:
                snap["listening"] = bool(self._listening_source())
            except Exception:
                pass
        snap["dropped_events"] = self.dropped_events
        return snap

    def _broadcast(self, message: dict[str, Any]) -> None:
        if not self._clients:
            return
        data = json.dumps(message, default=str)
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
