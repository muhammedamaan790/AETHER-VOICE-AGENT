"""Generation ID allocation only.

Day 1 scope: allocate monotonic IDs so every event can be stamped with the generation that
produced it, and so the trace is already shaped for fencing. It is deliberately NOT the continuity
engine -- there is no fencing decision, no Output Gate validity check, and no salvage here. Those
arrive on Day 3/4 (PHASES.md).

The GenerationStatus enum already exists in aether.events with exactly two states (active, fenced).
This module can mark a generation fenced, but nothing consumes that fact yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..events import EventType, GenerationStatus
from ..trace import Trace


@dataclass
class Generation:
    id: str
    status: GenerationStatus = GenerationStatus.ACTIVE


class GenerationRegistry:
    """Monotonic G1, G2, G3, ... Exactly one active at a time (RULES.md R4.2)."""

    def __init__(self, trace: Trace):
        self._trace = trace
        self._n = 0
        self._gens: dict[str, Generation] = {}
        self.active: Generation | None = None

    def allocate(self, *, turn_id: int | None = None) -> Generation:
        self._n += 1
        gen = Generation(id=f"G{self._n}")
        self._gens[gen.id] = gen
        previous = self.active
        if previous is not None:
            previous.status = GenerationStatus.FENCED
        self.active = gen
        self._trace.emit(
            EventType.GENERATION_CHANGED,
            turn_id=turn_id,
            gen=gen.id,
            from_gen=previous.id if previous else None,
            to_gen=gen.id,
            from_status=previous.status.value if previous else None,
            to_status=gen.status.value,
        )
        return gen

    def mark_fenced(self, gen_id: str | None) -> None:
        """Fence a generation immediately. Realtime-safe: plain state, no trace IO.

        Called from the PortAudio input callback when a barge-in is confirmed, so it must not
        allocate or write to the trace (locked decision 14). The caller emits `FenceRequested`
        from a normal thread once it is back on the main loop.

        After this, `is_active(gen_id)` is False, which is what lets a slow in-flight result --
        an LLM answer that arrives after the user already moved on -- be recognised as stale and
        discarded before it can be spoken (RULES.md R1).
        """
        if gen_id is None:
            return
        gen = self._gens.get(gen_id)
        if gen is not None:
            gen.status = GenerationStatus.FENCED
        if self.active is not None and self.active.id == gen_id:
            self.active = None

    def get(self, gen_id: str) -> Generation | None:
        return self._gens.get(gen_id)

    def is_active(self, gen_id: str) -> bool:
        """True only if this generation is still the one allowed to produce spoken output."""
        return self.active is not None and self.active.id == gen_id
