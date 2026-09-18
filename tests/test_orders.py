"""Taking an order over the telephone, and reading it back.

WHAT THIS REPLACED. "What did I order?" routed to `menu_overview` and AETHER read the caller the
entire menu. "Repeat my order" reached the model, which has no order to repeat. Both are the same
defect the router already fixed once for "do you have room service?" -- a confident answer to a
question nobody asked -- and both are pinned here so they cannot come back.

THE ORDER LIVES IN THE DATABASE, not in the model's context. A list carried in the conversation is
re-read every turn and can come back one dish longer; a row cannot. That is what makes "repeat my
order" the same answer every time it is asked, with `llm_ms = 0`.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from aether.hotel import HotelStore
from aether.hotel.bookings import BookingRefused, Bookings
from aether.hotel.db import UnknownRecord
from aether.hotel.router import route

AUDIO = np.zeros(16000, np.int16)
STORE = HotelStore()


@pytest.fixture
def orders(tmp_path):
    """A `Bookings` on a scratch copy -- one caller, one session, one open order."""
    import shutil

    from aether.hotel.db import default_db_path

    path = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), path)
    made = Bookings(path)
    yield made
    made.close()


def _dish(**kw):
    """A real dish from the database, so no expectation here is written down."""
    return next(i for i in STORE.menu()
                if all(getattr(i, k) == v for k, v in kw.items()))


# ============================ the router ============================

@pytest.mark.parametrize("said", [
    "I would like to order the chicken kebab",
    "order a chicken kebab please",
    "can I have the chicken kebab",
    "I'll have the chicken kebab",
    "give me the chicken kebab",
])
def test_ordering_a_dish_is_not_a_price_question(said):
    """The verb is the whole distinction. These sentences and "how much is the chicken kebab"
    share a dish and nothing else."""
    decision = route(said)
    assert decision is not None and decision.tool == "add_to_order", said
    assert decision.params["dish"] == "chicken kebab"


@pytest.mark.parametrize("said", [
    "how much is the chicken kebab",
    "what is the price of the chicken kebab",
    "do you have the chicken kebab",
    "tell me about the chicken kebab",
    "is the chicken kebab available",
    "does the chicken kebab contain nuts",
])
def test_asking_about_a_dish_never_orders_it(said):
    """The expensive direction of the same rule. A caller browsing the menu must not find a dish on
    their bill, so a question is never a write."""
    decision = route(said)
    assert decision is not None, said
    assert decision.tool != "add_to_order", f"{said!r} ordered a dish nobody asked for"


@pytest.mark.parametrize("said", [
    "repeat my order", "what did I order", "read back my order",
    "what is on my order so far", "run through my order",
])
def test_reading_the_order_back_does_not_read_the_menu(said):
    """The defect this feature exists to remove, pinned by name."""
    decision = route(said)
    assert decision is not None and decision.tool == "repeat_order", said


def test_a_bare_dish_name_joins_an_order_that_is_already_open():
    """"And two masala chai" carries no verb at all.

    Off an order it is a price question; mid-order it is another item, and nothing in the sentence
    can tell the two apart -- so the session says which. A waiter who answers "and two masala chai"
    with a price is not listening.
    """
    assert route("and two masala chai").tool == "price_of"
    assert route("and two masala chai", ordering=True).tool == "add_to_order"


def test_an_open_order_does_not_turn_browsing_into_ordering():
    """The guard on the rule above. Mid-order a caller may still ask what things cost."""
    for said in ("how much is the masala chai", "what desserts do you have",
                 "is the masala chai available", "tell me about the masala chai"):
        decision = route(said, ordering=True)
        assert decision.tool != "add_to_order", f"{said!r} ordered a dish nobody asked for"


def test_quantities_are_read_from_before_the_dish_only():
    """"The chicken kebab, and send it to room two zero five" must not order 205 kebabs."""
    assert route("order two chicken kebab please").params["quantity"] == 2
    assert route("order the chicken kebab").params["quantity"] == 1
    assert route("order the chicken kebab for room two zero five").params["quantity"] == 1
    # And a bare dish name with a number is still not an order -- the verb is what makes it one.
    assert route("two chicken kebab").tool == "price_of"


# ============================ the store ============================

def test_an_order_survives_across_turns(orders):
    kebab, chai = _dish(name="Chicken Kebab"), _dish(name="Masala Chai")
    orders.add_to_order(dish=kebab.name)
    order = orders.add_to_order(dish=chai.name, quantity=2)
    assert [line.name for line in order.lines] == [kebab.name, chai.name]
    assert order.total == kebab.price + 2 * chai.price
    assert order.items == 3


def test_the_same_dish_twice_is_one_line_not_two(orders):
    """"A kebab, and another kebab" and "two kebabs" must read back the same."""
    kebab = _dish(name="Chicken Kebab")
    orders.add_to_order(dish=kebab.name)
    order = orders.add_to_order(dish=kebab.name)
    assert len(order.lines) == 1
    assert order.lines[0].quantity == 2
    assert order.total == 2 * kebab.price


def test_the_price_is_copied_at_the_moment_of_ordering(orders):
    """An order totals what the caller was told, whatever the kitchen does to the menu later --
    and the menu row itself is never touched, which the authorizer enforces separately."""
    kebab = _dish(name="Chicken Kebab")
    order = orders.add_to_order(dish=kebab.name)
    assert order.lines[0].unit_price == kebab.price

    db = sqlite3.connect(str(orders.path))
    try:
        stored = db.execute(
            "SELECT unit_price_inr FROM restaurant_order_items"
            " WHERE order_id = ?", (int(order.reference),)).fetchone()[0]
    finally:
        db.close()
    assert stored == kebab.price


def test_a_dish_the_kitchen_has_run_out_of_is_refused_not_ordered(orders):
    """`check_availability` already answers this question; an order must not quietly disagree."""
    sold_out = _dish(available=False)
    with pytest.raises(BookingRefused) as refused:
        orders.add_to_order(dish=sold_out.name)
    assert refused.value.code == "dish_unavailable"
    assert refused.value.detail["dish"] == sold_out.name
    assert orders.current_order() is None, "a refused dish opened an order anyway"


def test_a_dish_that_does_not_exist_is_refused(orders):
    with pytest.raises(UnknownRecord):
        orders.add_to_order(dish="unicorn steak")


def test_nothing_is_ordered_before_anything_is_ordered(orders):
    assert orders.current_order() is None
    with pytest.raises(BookingRefused) as refused:
        orders.place_order()
    assert refused.value.code == "nothing_ordered"


def test_placing_an_order_sends_it_and_closes_it(orders):
    kebab = _dish(name="Chicken Kebab")
    orders.add_to_order(dish=kebab.name)
    placed = orders.place_order()
    assert placed.placed is True

    db = sqlite3.connect(str(orders.path))
    try:
        status = db.execute("SELECT status FROM restaurant_orders WHERE order_id = ?",
                            (int(placed.reference),)).fetchone()[0]
    finally:
        db.close()
    assert status == "preparing", "the kitchen never heard about it"
    # A placed order is finished: the next dish starts a new one rather than reopening this.
    assert orders.current_order() is None
    assert orders.add_to_order(dish=kebab.name).reference != placed.reference


def test_an_order_already_with_the_kitchen_cannot_be_cancelled_by_the_agent(orders):
    orders.add_to_order(dish=_dish(name="Chicken Kebab").name)
    orders.place_order()
    assert orders.current_order() is None
    with pytest.raises(BookingRefused) as refused:
        orders.cancel_order()
    assert refused.value.code == "nothing_ordered"


def test_cancelling_keeps_the_record_and_stops_the_order(orders):
    kebab = _dish(name="Chicken Kebab")
    order = orders.add_to_order(dish=kebab.name)
    orders.cancel_order()
    assert orders.current_order() is None

    db = sqlite3.connect(str(orders.path))
    try:
        status = db.execute("SELECT status FROM restaurant_orders WHERE order_id = ?",
                            (int(order.reference),)).fetchone()[0]
    finally:
        db.close()
    assert status == "cancelled", "a cancelled order should still be on the record"


def test_one_caller_cannot_add_to_another_callers_order(orders, tmp_path):
    """Every call builds its own store and so its own `Bookings`. Two callers write to one database
    and must never see each other's order."""
    kebab, chai = _dish(name="Chicken Kebab"), _dish(name="Masala Chai")
    orders.add_to_order(dish=kebab.name)

    other = Bookings(orders.path)
    try:
        assert other.current_order() is None, "a second caller inherited an order"
        theirs = other.add_to_order(dish=chai.name)
        assert theirs.reference != orders.current_order().reference
        assert [line.name for line in theirs.lines] == [chai.name]
    finally:
        other.close()
    assert [line.name for line in orders.current_order().lines] == [kebab.name]


def test_an_absurd_quantity_is_refused_with_the_limit(orders):
    kebab = _dish(name="Chicken Kebab")
    with pytest.raises(BookingRefused) as refused:
        orders.add_to_order(dish=kebab.name, quantity=500)
    assert refused.value.code == "too_many_of_one_dish"
    assert refused.value.detail["most"] == 20


def test_the_limit_counts_the_whole_line_not_one_request(orders):
    """Ordering eleven twice is twenty-two, and must be refused the same way."""
    kebab = _dish(name="Chicken Kebab")
    orders.add_to_order(dish=kebab.name, quantity=11)
    with pytest.raises(BookingRefused):
        orders.add_to_order(dish=kebab.name, quantity=11)
    assert orders.current_order().lines[0].quantity == 11, "the refused half was written anyway"


# ============================ the whole call ============================

def test_a_caller_orders_two_things_and_hears_them_read_back(monkeypatch, tmp_path):
    """The manager's own question: order this, then repeat my order.

    One `Day1Spike` for the whole conversation, because an order is session state -- a fresh spike
    per turn is a fresh caller per turn, which is a different and much easier test. On a PRIVATE
    copy of the hotel, because this writes: the suite shares one scratch database, and rows left
    behind here would be visible to every later test.
    """
    from tests.test_menu_routing import build

    kebab, chai = _dish(name="Chicken Kebab"), _dish(name="Masala Chai")
    spike, _t, rime, llm = build(monkeypatch, "")
    private = _hotel(tmp_path)
    spike.menu = private
    spike.tools.store = private

    for said in (f"I would like to order the {kebab.name.lower()}",
                 f"and two {chai.name.lower()}",
                 "repeat my order"):
        spike.stt.text = said
        spike.handle_utterance(AUDIO, 0.0)

    read_back = rime.spoken[-1]
    assert kebab.name in read_back and chai.name in read_back
    assert llm.calls == [], "an order must never reach the model"

    from aether.hotel import say_price
    assert say_price(kebab.price + 2 * chai.price) in read_back, (
        f"the total is wrong or unspoken: {read_back!r}"
    )


def test_repeat_my_order_before_ordering_does_not_read_the_menu(monkeypatch):
    """The exact defect, through the real pipeline. It used to answer with every category."""
    from tests.test_menu_routing import build

    spike, _t, rime, llm = build(monkeypatch, "what did I order")
    spike.handle_utterance(AUDIO, 0.0)

    said = rime.spoken[-1]
    assert llm.calls == []
    for item in STORE.menu():
        assert item.name not in said, f"the whole menu was read back: {said!r}"
    assert "not ordered anything" in said.lower()


# ============================ fencing ============================
#
# An order is a WRITE, so it inherits the project's central claim: a caller who changes their mind
# while the tool is in flight must leave no row behind. Nothing new is built for this -- `ToolRunner`
# checks validity after its delay and before the tool body, for every tool.
#
# Two different things are checked below, and it is worth being exact about which is which, because
# the obvious reading is wrong. `ToolRunner` does NOT consult the mutating flag when deciding to
# fence; that flag is trace metadata, so a reader can tell at a glance which invocations could have
# changed the world. The behavioural tests are therefore verified against the mutation that matters
# -- moving the fence check after the tool body, which fails both of them -- and the structural test
# guards the declaration on its own terms.

def _fenced_runner(store):
    from aether.tools import ToolRunner
    from aether.hotel.tools import HOTEL_TOOLS
    from aether.trace import Trace

    return ToolRunner(Trace(), store, tools=HOTEL_TOOLS, delay_ms=50.0, sleep=lambda _s: None)


def _hotel(tmp_path):
    import shutil

    from aether.hotel.db import default_db_path

    path = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), path)
    return HotelStore(path=path)


def _rows(store, table):
    import sqlite3

    db = sqlite3.connect(str(store.path))
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        db.close()


def test_a_fenced_dish_never_reaches_the_order(tmp_path):
    """The caller said "the chicken kebab" and then changed their mind. Nothing is ordered."""
    store = _hotel(tmp_path)
    before = _rows(store, "restaurant_order_items")

    result = _fenced_runner(store).run(
        "add_to_order", gen="g", turn_id=1, is_valid=lambda: False,
        dish=_dish(name="Chicken Kebab").name)

    assert not result.may_speak, "a fenced order must not be spoken"
    assert _rows(store, "restaurant_order_items") == before, "a fenced dish was ordered anyway"
    assert store.bookings.current_order() is None


def test_a_fenced_place_never_reaches_the_kitchen(tmp_path):
    """The worse half. An order sent to the kitchen cannot be unsent by saying sorry."""
    import sqlite3

    store = _hotel(tmp_path)
    order = store.bookings.add_to_order(dish=_dish(name="Chicken Kebab").name)

    result = _fenced_runner(store).run(
        "place_order", gen="g", turn_id=1, is_valid=lambda: False)

    assert not result.may_speak
    db = sqlite3.connect(str(store.path))
    try:
        status = db.execute("SELECT status FROM restaurant_orders WHERE order_id = ?",
                            (int(order.reference),)).fetchone()[0]
    finally:
        db.close()
    assert status == "pending", "a fenced order was sent to the kitchen"


def test_every_order_tool_that_writes_is_declared_mutating():
    """Structural, and about observability rather than about safety.

    `mutates` is stamped on every `ResultDiscarded`, so a trace says which discarded invocations
    could have changed the world. A write declared non-mutating is still fenced -- the check does
    not consult the flag -- but the trace would quietly claim nothing was at stake, and a reader
    auditing a recorded call would believe it.
    """
    from aether.hotel.tools import HOTEL_TOOLS

    for name in ("add_to_order", "place_order", "cancel_order"):
        _fn, mutating = HOTEL_TOOLS[name]
        assert mutating is True, f"{name} writes and is not declared mutating"
    # And the read is declared a read: reading an order back must never change it, and a mutating
    # declaration would make a plain question fenceable for no reason.
    _fn, mutating = HOTEL_TOOLS["repeat_order"]
    assert mutating is False
