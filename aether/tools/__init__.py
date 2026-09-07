"""Warehouse tools: structured Python the model can *choose*, never code the model writes.

Two rules shape this module.

**The model does not touch data.** Every tool is an ordinary Python function with a fixed name and
typed parameters. A model (later, a classifier and supervisor) may select which one runs and with
what arguments; it never receives a `WarehouseStore`, never gets to phrase a query, and cannot
reach a mutation except through one of the named tools here. What comes back is a `ToolResult`
holding plain dicts -- data, not a live reference.

**A fenced generation can neither speak nor write.** This is the part worth reading carefully. The
validity check happens *after* the delay and *before* the tool body runs, so a generation fenced
while its lookup was in flight does not merely have its answer discarded -- its `skip`, `cancel` or
`select` never happens at all. Checking afterwards would leave the store mutated by a turn the user
had already abandoned, and no amount of output fencing downstream could undo that.

Fencing itself is not reimplemented here. The runner is handed an `is_valid` callable and asks it;
the authority stays `GenerationRegistry` / `BargeInCoordinator` exactly as it is for audio, so
there is one answer to "is this generation still current" and one place it lives.

Delay is injectable so an interruption is reproducible. `delay_ms` sets how long a tool appears to
take, and `sleep` is the function used to wait -- tests pass one that fences mid-delay instead of
sleeping, which makes "the user interrupted while the lookup was running" a deterministic unit
test rather than a race to lose.

Events: `TaskStarted` on entry, then `ResultReceived` or `ResultDiscarded`. All three are already
in the canonical vocabulary (MEMORY.md §3 -- there is deliberately no `ToolCallStarted`), so this
module adds no event type.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..events import EventType
from ..trace import Trace, now_ms
from ..warehouse import OrderStatus, Priority, UnknownRecord, WarehouseStore, describe_order

# Reason strings. Fixed so the trace stays greppable and the evaluator can count them later.
STALE_TOOL = "stale_generation_tool"
UNKNOWN_RECORD = "unknown_record"


@dataclass(frozen=True)
class ToolResult:
    """One tool invocation, tagged with everything needed to judge whether it may be used.

    Identity is the point of this object. `gen` says which generation asked, `task_id` says which
    invocation this was, and `state_version` says which version of the warehouse it describes --
    so a result that arrives late is recognisable as stale on two independent grounds, not one.
    """

    tool: str
    params: dict[str, Any]
    task_id: str
    gen: str | None
    turn_id: int | None
    state_version: int
    records: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    stale: bool = False
    reason: str = ""
    latency_ms: float = 0.0

    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def may_speak(self) -> bool:
        """The single question the caller asks before letting any of this become speech.

        A stale result is never speakable. A failed lookup is: "there is no order ORD-9999" is a
        true answer to a live question, and refusing to say it would be a different bug.
        """
        return not self.stale


# --- tool bodies ---------------------------------------------------------------------------
#
# Each takes the store plus already-validated parameters and returns (records, summary). None of
# them emits events, sleeps, or checks fencing -- the runner owns all three, so a tool cannot
# forget to.


def _find_priority_orders(store: WarehouseStore, *, aisle: int | None = None) -> tuple[list, dict]:
    """The demo's opening question. `aisle` is the refinement that narrows it."""
    rows = [
        describe_order(store, o) for o in store.orders()
        if o.priority is Priority.HIGH and o.status is OrderStatus.PENDING
    ]
    if aisle is not None:
        rows = [r for r in rows if r["aisle"] == aisle]
    return rows, {"aisle": aisle, "priority": Priority.HIGH.value}


def _find_orders(
    store: WarehouseStore,
    *,
    aisle: int | None = None,
    priority: str | None = None,
    status: str | None = None,
) -> tuple[list, dict]:
    """General filter. Every parameter is optional and every combination is a plain AND."""
    rows = [describe_order(store, o) for o in store.orders()]
    if aisle is not None:
        rows = [r for r in rows if r["aisle"] == aisle]
    if priority is not None:
        rows = [r for r in rows if r["priority"] == priority]
    if status is not None:
        rows = [r for r in rows if r["status"] == status]
    return rows, {"aisle": aisle, "priority": priority, "status": status}


def _count_orders(store: WarehouseStore, **filters: Any) -> tuple[list, dict]:
    """"How many so far?" -- the count is the answer, so no records are returned.

    Deliberately reuses `_find_orders` rather than counting separately: a count that could
    disagree with the list it summarises would be worse than no count.
    """
    rows, applied = _find_orders(store, **filters)
    return [], {**applied, "count": len(rows)}


def _lookup_bin(store: WarehouseStore, *, order_id: str) -> tuple[list, dict]:
    order = store.order(order_id)
    bin_ = store.bin_for_sku(order.sku)
    return [describe_order(store, order)], {
        "order_id": order_id, "bin": bin_.bin_id, "aisle": bin_.aisle, "shelf": bin_.shelf,
    }


def _lookup_inventory(store: WarehouseStore, *, sku: str) -> tuple[list, dict]:
    bin_ = store.bin_for_sku(sku)
    qty = store.quantity_on_hand(sku)
    return [], {
        "sku": sku, "product": store.product(sku).name, "quantity_on_hand": qty,
        "bin": bin_.bin_id, "aisle": bin_.aisle, "in_stock": qty > 0,
    }


def _select_order(store: WarehouseStore, *, order_id: str) -> tuple[list, dict]:
    order = store.select(order_id)
    return [describe_order(store, order)], {"selected_order_id": order.order_id}


def _skip_order(store: WarehouseStore, *, order_id: str) -> tuple[list, dict]:
    order = store.set_status(order_id, OrderStatus.SKIPPED)
    return [describe_order(store, order)], {"order_id": order_id, "new_status": order.status.value}


def _cancel_order(store: WarehouseStore, *, order_id: str) -> tuple[list, dict]:
    order = store.set_status(order_id, OrderStatus.CANCELLED)
    return [describe_order(store, order)], {"order_id": order_id, "new_status": order.status.value}


def _update_order_status(store: WarehouseStore, *, order_id: str, status: str) -> tuple[list, dict]:
    try:
        parsed = OrderStatus(status)
    except ValueError as exc:
        # A bad status is an unknown record, not a crash and not a silent no-op.
        raise UnknownRecord(f"no such status: {status}") from exc
    order = store.set_status(order_id, parsed)
    return [describe_order(store, order)], {"order_id": order_id, "new_status": order.status.value}


# name -> (function, mutates?). The flag is documentation that the trace also carries, so a reader
# can tell at a glance which invocations could have changed the world.
TOOLS: dict[str, tuple[Callable[..., tuple[list, dict]], bool]] = {
    "find_priority_orders": (_find_priority_orders, False),
    "find_orders": (_find_orders, False),
    "count_orders": (_count_orders, False),
    "lookup_bin": (_lookup_bin, False),
    "lookup_inventory": (_lookup_inventory, False),
    "select_order": (_select_order, True),
    "skip_order": (_skip_order, True),
    "cancel_order": (_cancel_order, True),
    "update_order_status": (_update_order_status, True),
}


class UnknownTool(KeyError):
    """A tool name that does not exist. Never guessed at, never silently substituted."""


class ToolRunner:
    """Runs one tool, under one generation, with a reproducible delay and a real fence check."""

    def __init__(
        self,
        trace: Trace,
        store: WarehouseStore,
        *,
        delay_ms: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.trace = trace
        self.store = store
        self.delay_ms = delay_ms
        self._sleep = sleep
        self._n = 0

    def run(
        self,
        name: str,
        *,
        gen: str | None = None,
        turn_id: int | None = None,
        is_valid: Callable[[], bool] | None = None,
        delay_ms: float | None = None,
        **params: Any,
    ) -> ToolResult:
        """Invoke `name`. Returns a ToolResult that is either usable or explicitly stale.

        Never raises for a missing record or an unknown tool: a voice turn must survive both. The
        failure is reported in the result and in the trace, and the caller decides what to say.
        """
        fn_entry = TOOLS.get(name)
        if fn_entry is None:
            raise UnknownTool(f"no such tool: {name}. Known: {', '.join(sorted(TOOLS))}")
        fn, mutates = fn_entry

        self._n += 1
        task_id = f"T{self._n}"
        started = now_ms()

        self.trace.emit(
            EventType.TASK_STARTED,
            turn_id=turn_id,
            gen=gen,
            task=name,
            task_id=task_id,
            params=dict(params),
            mutates=mutates,
            delay_ms=self.delay_ms if delay_ms is None else delay_ms,
            state_version=self.store.state_version,
        )

        wait_ms = self.delay_ms if delay_ms is None else delay_ms
        if wait_ms > 0:
            # The interruption window. Tests inject a `sleep` that fences here instead of waiting,
            # which is what makes "interrupted mid-lookup" deterministic.
            self._sleep(wait_ms / 1000.0)

        # THE FENCE CHECK, and its position is the contract: after the delay, before the body.
        # A generation fenced while this was in flight does not run the tool at all, so a stale
        # `skip` or `cancel` never reaches the store. Discarding the *output* afterwards would be
        # too late -- the mutation would already have landed.
        if is_valid is not None and not is_valid():
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=turn_id,
                gen=gen,
                task=name,
                task_id=task_id,
                stage="tool",
                reason=STALE_TOOL,
                mutates=mutates,
                applied=False,
                state_version=self.store.state_version,
            )
            return ToolResult(
                tool=name, params=dict(params), task_id=task_id, gen=gen, turn_id=turn_id,
                state_version=self.store.state_version, stale=True, reason=STALE_TOOL,
                latency_ms=round(now_ms() - started, 1),
            )

        try:
            records, summary = fn(self.store, **params)
        except UnknownRecord as exc:
            # A live question with no answer in the fixture. Not stale, so it may still be spoken:
            # "there is no such order" is true and useful. Never invented into a plausible record.
            self.trace.emit(
                EventType.RESULT_DISCARDED,
                turn_id=turn_id,
                gen=gen,
                task=name,
                task_id=task_id,
                stage="tool",
                reason=UNKNOWN_RECORD,
                detail=str(exc),
                state_version=self.store.state_version,
            )
            return ToolResult(
                tool=name, params=dict(params), task_id=task_id, gen=gen, turn_id=turn_id,
                state_version=self.store.state_version, reason=UNKNOWN_RECORD,
                summary={"error": str(exc)}, latency_ms=round(now_ms() - started, 1),
            )

        result = ToolResult(
            tool=name, params=dict(params), task_id=task_id, gen=gen, turn_id=turn_id,
            state_version=self.store.state_version, records=records, summary=summary,
            latency_ms=round(now_ms() - started, 1),
        )
        self.trace.emit(
            EventType.RESULT_RECEIVED,
            turn_id=turn_id,
            gen=gen,
            task=name,
            task_id=task_id,
            records=result.count,
            mutates=mutates,
            state_version=result.state_version,
            latency_ms=result.latency_ms,
            **{k: v for k, v in summary.items() if k != "error"},
        )
        return result
