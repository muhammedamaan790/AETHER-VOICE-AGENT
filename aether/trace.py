"""Append-only event trace.

The single writer for the canonical event vocabulary (aether.events). Every component emits
through here; the evaluator (Day 4) reads what this writes. See RULES.md R3.4 -- append only,
never rewritten.

Clock: time.perf_counter() converted to float milliseconds, captured at emission. All latency
metrics in PRD.md section 6 are differences between two `t` values from this clock, so they are
immune to wall-clock adjustment.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import EventType

_T0 = time.perf_counter()


def now_ms() -> float:
    """Monotonic milliseconds since process start."""
    return (time.perf_counter() - _T0) * 1000.0


@dataclass
class Event:
    seq: int
    t: float                      # monotonic ms since process start
    type: str                     # EventType value
    turn_id: int | None = None
    gen: str | None = None        # e.g. "G1"; None for pre-generation audio events
    fields: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        payload = d.pop("fields")
        d.update(payload)  # flatten per-event fields alongside the envelope
        return json.dumps(d, ensure_ascii=False, default=str)


class Trace:
    """Append-only JSONL trace. Thread-safe: audio callbacks emit from non-main threads."""

    def __init__(self, path: str | Path | None = None, echo: bool = False):
        self._lock = threading.Lock()
        self._seq = 0
        self.events: list[Event] = []
        self.echo = echo
        self.path: Path | None = None
        self._fh = None
        if path is not None:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")

    @classmethod
    def new_run(cls, trace_dir: str | Path = "traces", echo: bool = False) -> "Trace":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return cls(Path(trace_dir) / f"run-{stamp}.jsonl", echo=echo)

    def emit(
        self,
        type_: EventType,
        *,
        turn_id: int | None = None,
        gen: str | None = None,
        t: float | None = None,
        **fields: Any,
    ) -> Event:
        """Emit an event.

        `t` may be overridden when the moment being recorded happened in another thread -- the
        audio callback applies duck/stop and captures the timestamp there, then the control thread
        emits the event with that timestamp. `seq` still reflects emission order, so `t` can be
        very slightly out of order relative to `seq`. Metrics use `t`.
        """
        with self._lock:
            self._seq += 1
            ev = Event(
                seq=self._seq,
                t=now_ms() if t is None else t,
                type=type_.value,
                turn_id=turn_id,
                gen=gen,
                fields=fields,
            )
            self.events.append(ev)
            if self._fh is not None:
                self._fh.write(ev.to_json() + "\n")
                self._fh.flush()
            if self.echo:
                extra = " ".join(f"{k}={v}" for k, v in fields.items() if k != "text")
                text = fields.get("text")
                line = f"[{ev.t:9.1f}ms] {ev.type:<24} {('gen=' + gen) if gen else '':<7} {extra}"
                if text:
                    line += f'  "{text}"'
                print(line, flush=True)
        return ev

    # --- read helpers (used by tests and the latency harness) ----------------------

    def first(self, type_: EventType) -> Event | None:
        return next((e for e in self.events if e.type == type_.value), None)

    def last(self, type_: EventType) -> Event | None:
        return next((e for e in reversed(self.events) if e.type == type_.value), None)

    def all(self, type_: EventType) -> list[Event]:
        return [e for e in self.events if e.type == type_.value]

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


def read_trace(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL trace back. Used by the Day-4 evaluator and by the latency harness."""
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
