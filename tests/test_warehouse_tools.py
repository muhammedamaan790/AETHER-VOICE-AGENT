"""The warehouse fixture and the tool layer.

Two properties matter more than the lookups themselves and most of this file is about them:

* a fenced generation's tool result is never speakable, and
* a fenced generation's *mutation never happens at all*.

The second is the one that cannot be fixed downstream. Output fencing can stop a stale answer
being spoken; nothing can un-skip an order that a turn the user already abandoned went on to skip.
"""

from __future__ import annotations

import pytest

from aether.events import EventType
from aether.tools import STALE_TOOL, TOOLS, ToolRunner, UnknownTool
from aether.trace import Trace
from aether.warehouse import ORDERS, OrderStatus, Priority, WarehouseStore


@pytest.fixture
def store():
    return WarehouseStore()


@pytest.fixture
def runner(store):
    return ToolRunner(Trace(), store)


# ============================ the fixture ============================

def test_the_fixture_supports_the_demo_sentence():
    """`data/README.md`: "8 priority orders, actually only aisle 9" must be a real refinement."""
    store = WarehouseStore()
    priority = [o for o in store.orders() if o.priority is Priority.HIGH]
    in_aisle_9 = [o for o in priority if store.aisle_for_order(o.order_id) == 9]

    assert len(priority) == 8, "the opening answer is a list of 8"
    assert len(in_aisle_9) == 3, "and aisle 9 is a strict, non-trivial subset"
    assert 0 < len(in_aisle_9) < len(priority), "narrowing must actually discard something"


def test_the_fixture_is_deterministic():
    """Two stores must be byte-identical: no clocks, no randomness, no time-derived IDs."""
    a = [o.order_id for o in WarehouseStore().orders()]
    b = [o.order_id for o in WarehouseStore().orders()]
    assert a == b == [o.order_id for o in ORDERS]


def test_orders_are_snapshots_not_live_references(store):
    """A record handed out earlier must never change under its holder."""
    before = store.order("ORD-4471")
    store.set_status("ORD-4471", OrderStatus.SKIPPED)

    assert before.status is OrderStatus.PENDING, "the old snapshot is unchanged"
    assert store.order("ORD-4471").status is OrderStatus.SKIPPED


def test_every_mutation_bumps_state_version(store):
    versions = [store.state_version]
    store.select("ORD-4471"); versions.append(store.state_version)
    store.set_status("ORD-4472", OrderStatus.SKIPPED); versions.append(store.state_version)
    store.clear_selection(); versions.append(store.state_version)

    assert versions == sorted(set(versions)), "strictly increasing, never reused"


def test_reads_do_not_bump_state_version(store):
    before = store.state_version
    store.orders(); store.order("ORD-4471"); store.bin_for_sku("SKU-1001")
    assert store.state_version == before


# ============================ the tools ============================

def test_priority_lookup_then_aisle_refinement(runner):
    """The demo path: 8, then narrowed to 3, using the same tool with one more constraint."""
    broad = runner.run("find_priority_orders", gen="G1")
    narrow = runner.run("find_priority_orders", gen="G2", aisle=9)

    assert broad.count == 8
    assert narrow.count == 3
    assert {r["order_id"] for r in narrow.records} <= {r["order_id"] for r in broad.records}, (
        "the refinement is a subset of the broad answer -- what makes salvage possible later"
    )


def test_count_tool_agrees_with_the_list_it_summarises(runner):
    listed = runner.run("find_orders", gen="G1", aisle=9)
    counted = runner.run("count_orders", gen="G1", aisle=9)

    assert counted.summary["count"] == listed.count
    assert counted.records == [], "a count answers with a number, not a list"


def test_filter_tool_applies_every_constraint(runner):
    result = runner.run("find_orders", gen="G1", aisle=3, priority="high", status="pending")
    assert result.count == 2
    assert all(r["aisle"] == 3 and r["priority"] == "high" for r in result.records)


def test_bin_and_inventory_lookup(runner):
    bin_result = runner.run("lookup_bin", gen="G1", order_id="ORD-4471")
    inv = runner.run("lookup_inventory", gen="G1", sku="SKU-1001")

    assert bin_result.summary["bin"] == "B-09-A" and bin_result.summary["aisle"] == 9
    assert inv.summary["quantity_on_hand"] == 14 and inv.summary["in_stock"] is True


def test_out_of_stock_is_reported_not_hidden(runner):
    inv = runner.run("lookup_inventory", gen="G1", sku="SKU-1003")
    assert inv.summary["quantity_on_hand"] == 0
    assert inv.summary["in_stock"] is False


def test_select_skip_cancel_and_update(runner, store):
    assert runner.run("select_order", gen="G1", order_id="ORD-4471").summary[
        "selected_order_id"] == "ORD-4471"
    assert store.selected_order_id == "ORD-4471"

    runner.run("skip_order", gen="G1", order_id="ORD-4472")
    runner.run("cancel_order", gen="G1", order_id="ORD-4473")
    runner.run("update_order_status", gen="G1", order_id="ORD-4474", status="picked")

    assert store.order("ORD-4472").status is OrderStatus.SKIPPED
    assert store.order("ORD-4473").status is OrderStatus.CANCELLED
    assert store.order("ORD-4474").status is OrderStatus.PICKED


def test_closing_the_selected_order_clears_the_selection(runner, store):
    runner.run("select_order", gen="G1", order_id="ORD-4471")
    runner.run("skip_order", gen="G1", order_id="ORD-4471")
    assert store.selected_order_id is None, "no dangling selection on a closed order"


# ============================ failures stay survivable ============================

def test_unknown_record_is_answerable_not_fatal(runner):
    """"There is no such order" is a true answer to a live question, so it may be spoken."""
    result = runner.run("lookup_bin", gen="G1", order_id="ORD-9999")

    assert result.stale is False and result.may_speak is True
    assert result.reason == "unknown_record"
    assert result.records == [], "nothing is invented to fill the gap"


def test_unknown_status_is_rejected_without_mutating(runner, store):
    before = store.state_version
    result = runner.run("update_order_status", gen="G1", order_id="ORD-4471", status="teleported")

    assert result.reason == "unknown_record"
    assert store.state_version == before, "a rejected status must not touch the store"
    assert store.order("ORD-4471").status is OrderStatus.PENDING


def test_unknown_tool_raises(runner):
    with pytest.raises(UnknownTool):
        runner.run("drop_database", gen="G1")


# ============================ fencing ============================

def _fence_during_sleep(runner_state):
    """A sleep that fences instead of waiting: the interruption lands mid-tool, deterministically."""
    def sleeper(_seconds):
        runner_state["valid"] = False
    return sleeper


def test_a_result_fenced_mid_lookup_is_never_speakable(store):
    state = {"valid": True}
    trace = Trace()
    runner = ToolRunner(trace, store, delay_ms=500.0, sleep=_fence_during_sleep(state))

    result = runner.run("find_priority_orders", gen="G1", is_valid=lambda: state["valid"])

    assert result.stale is True
    assert result.may_speak is False, "the golden invariant: a stale result cannot become speech"
    assert result.records == [], "and it carries nothing that could be spoken by accident"
    assert result.reason == STALE_TOOL


def test_a_mutation_fenced_mid_flight_never_reaches_the_store(store):
    """The property output fencing cannot provide: the skip must not happen at all."""
    state = {"valid": True}
    runner = ToolRunner(Trace(), store, delay_ms=500.0, sleep=_fence_during_sleep(state))
    before_version = store.state_version

    result = runner.run(
        "skip_order", gen="G1", order_id="ORD-4471", is_valid=lambda: state["valid"]
    )

    assert result.stale is True
    assert store.order("ORD-4471").status is OrderStatus.PENDING, "the skip never landed"
    assert store.state_version == before_version, "the world did not move"


def test_an_unfenced_mutation_does_reach_the_store(store):
    """The control: same delay, same code path, generation still valid."""
    runner = ToolRunner(Trace(), store, delay_ms=500.0, sleep=lambda _s: None)

    result = runner.run("skip_order", gen="G1", order_id="ORD-4471", is_valid=lambda: True)

    assert result.stale is False
    assert store.order("ORD-4471").status is OrderStatus.SKIPPED


def _code_only(module) -> str:
    """Module source with comments and string literals removed.

    Structural guards must look at code, not prose. Grepping raw source made this file's own
    docstring -- which names `GenerationRegistry` precisely to say tools do NOT touch it -- trip
    the guard that checks tools do not touch it.
    """
    import inspect
    import io
    import tokenize

    src = inspect.getsource(module)
    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(tok.string)
    return " ".join(kept)


def test_fencing_uses_the_callers_authority_not_its_own():
    """The runner must not reimplement fencing -- it asks, and the answer comes from outside."""
    import aether.tools as mod

    code = _code_only(mod)
    for forbidden in ("GenerationRegistry", "mark_fenced", "BargeInCoordinator", "AudioGate"):
        assert forbidden not in code, (
            f"tools must not reach for {forbidden}; fencing authority stays with the caller"
        )
    assert "is_valid" in code, "it asks the caller instead"


def test_the_llm_cannot_mutate_data_directly():
    """Structural: mutation exists only behind named tools, and no tool takes free-form input."""
    import inspect

    import aether.tools as mod
    import aether.warehouse as wh

    # The two store mutators a tool may call. `clear_selection` is deliberately absent: it is
    # store-internal housekeeping invoked by `set_status`, not a tool entry point.
    code = _code_only(mod)
    for mutator in ("set_status", "select"):
        assert f"store . {mutator}" in code or f"store.{mutator}" in code, (
            f"{mutator} should be reached through the store, from a named tool"
        )

    # No tool accepts raw code, SQL, or a callable to run -- the model picks a tool, never writes
    # one. This is the half of the guard that actually constrains what a model can do.
    for name, (fn, _mutates) in TOOLS.items():
        params = inspect.signature(fn).parameters
        for bad in ("query", "sql", "code", "expr", "eval", "fn", "callback", "filter_fn"):
            assert bad not in params, f"{name} must not accept free-form {bad!r}"

    assert not hasattr(wh, "execute"), "the warehouse exposes no general execution entry point"
    assert not hasattr(wh, "eval"), "and no evaluation entry point"


# ============================ trace integration ============================

def test_a_successful_tool_emits_task_started_then_result_received(store):
    trace = Trace()
    runner = ToolRunner(trace, store)

    runner.run("find_priority_orders", gen="G1", turn_id=7)

    kinds = [e.type for e in trace.events]
    assert kinds == ["TaskStarted", "ResultReceived"]
    started, received = trace.events
    assert started.gen == "G1" and started.turn_id == 7
    assert started.fields["task"] == "find_priority_orders"
    assert received.fields["records"] == 8
    assert received.fields["task_id"] == started.fields["task_id"], "one task, one identity"


def test_a_fenced_tool_emits_result_discarded_and_no_result_received(store):
    state = {"valid": True}
    trace = Trace()
    runner = ToolRunner(trace, store, delay_ms=100.0, sleep=_fence_during_sleep(state))

    runner.run("skip_order", gen="G1", order_id="ORD-4471", is_valid=lambda: state["valid"])

    assert [e.type for e in trace.events] == ["TaskStarted", "ResultDiscarded"]
    discarded = trace.events[-1]
    assert discarded.fields["reason"] == STALE_TOOL
    assert discarded.fields["stage"] == "tool"
    assert discarded.fields["applied"] is False, "the trace records that nothing was applied"
    assert trace.all(EventType.RESULT_RECEIVED) == []


def test_the_trace_marks_which_invocations_could_have_changed_the_world(store):
    trace = Trace()
    runner = ToolRunner(trace, store)

    runner.run("find_priority_orders", gen="G1")
    runner.run("skip_order", gen="G1", order_id="ORD-4471")

    mutates = [e.fields["mutates"] for e in trace.all(EventType.TASK_STARTED)]
    assert mutates == [False, True]


def test_only_canonical_events_are_emitted(store):
    """The vocabulary is frozen at 18 (RULES.md R3); tools add none."""
    trace = Trace()
    runner = ToolRunner(trace, store)
    runner.run("find_priority_orders", gen="G1")
    runner.run("lookup_bin", gen="G1", order_id="ORD-9999")

    known = {e.value for e in EventType}
    assert {e.type for e in trace.events} <= known


# ============================ identity ============================

def test_every_result_carries_task_generation_and_state_identity(runner):
    result = runner.run("find_priority_orders", gen="G4", turn_id=2)

    assert result.gen == "G4"
    assert result.turn_id == 2
    assert result.task_id.startswith("T")
    assert isinstance(result.state_version, int)


def test_task_ids_are_monotonic_within_a_runner(runner):
    ids = [runner.run("count_orders", gen="G1").task_id for _ in range(3)]
    assert ids == ["T1", "T2", "T3"]


# ============================ end-to-end, against the real fencing ============================
#
# Everything above fences through a stub `is_valid`. These use the real GenerationRegistry,
# AudioGate and BargeInCoordinator, because the requirement is that tool results are stopped by
# AETHER's *existing* fencing -- a stub would only prove the stub was consulted.

def test_a_real_barge_in_mid_tool_stops_the_result_and_the_mutation(store):
    """The whole point, wired end to end: user speaks while a skip is in flight."""
    import numpy as np

    from aether.audio.player import AudioGate
    from aether.interruption import BargeInCoordinator
    from aether.supervisor.generations import GenerationRegistry

    trace = Trace()
    gens = GenerationRegistry(trace)
    gate = AudioGate(trace)                      # real gate; its stream is never started
    barge = BargeInCoordinator(trace, gens, gate, meaningful_speech_ms=300.0)

    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id, turn_id=1)

    def user_interrupts(_seconds):
        # Exactly what the PortAudio input thread does on a real barge-in.
        barge.on_speech_onset()
        barge.on_voiced_progress(400.0)          # past MEANINGFUL_SPEECH_MS -> fence

    runner = ToolRunner(trace, store, delay_ms=500.0, sleep=user_interrupts)
    result = runner.run(
        "skip_order", gen=gen.id, order_id="ORD-4471",
        is_valid=lambda: barge.is_valid(gen.id),
    )

    assert result.stale is True and result.may_speak is False
    assert store.order("ORD-4471").status is OrderStatus.PENDING, "the mutation never landed"
    assert gens.is_active(gen.id) is False, "the real registry fenced it"

    # Defence in depth: even had a caller ignored `may_speak`, the real gate refuses the audio.
    pcm = np.zeros(480, dtype=np.int16)
    assert gate.enqueue(pcm, turn_id=1, gen=gen.id) is False, (
        "the AudioGate refuses audio for a fenced generation"
    )


def test_without_an_interruption_the_same_path_completes(store):
    """The control for the test above: identical wiring, nobody interrupts."""
    from aether.audio.player import AudioGate
    from aether.interruption import BargeInCoordinator
    from aether.supervisor.generations import GenerationRegistry

    trace = Trace()
    gens = GenerationRegistry(trace)
    gate = AudioGate(trace)
    barge = BargeInCoordinator(trace, gens, gate, meaningful_speech_ms=300.0)

    gen = barge.begin_turn(turn_id=1)
    gate.set_active_generation(gen.id, turn_id=1)

    runner = ToolRunner(trace, store, delay_ms=500.0, sleep=lambda _s: None)
    result = runner.run(
        "skip_order", gen=gen.id, order_id="ORD-4471",
        is_valid=lambda: barge.is_valid(gen.id),
    )

    assert result.stale is False and result.may_speak is True
    assert store.order("ORD-4471").status is OrderStatus.SKIPPED
    assert gens.is_active(gen.id) is True


def test_a_result_records_the_state_version_it_was_computed_from(store):
    runner = ToolRunner(Trace(), store)
    early = runner.run("find_priority_orders", gen="G1")
    runner.run("skip_order", gen="G1", order_id="ORD-4471")
    later = runner.run("find_priority_orders", gen="G1")

    assert later.state_version > early.state_version, (
        "a late result is recognisable as describing a world that has moved on, "
        "independently of generation fencing"
    )
    assert later.count == early.count - 1, "and the skipped order is genuinely gone"
