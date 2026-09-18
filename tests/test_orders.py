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


# ============================ taking things off ============================
#
# REPORTED FROM A REAL CALL, and the worst defect this project has had. There was no removal rule
# at all, so "remove paneer butter masala" matched the dish, found no reason not to, and ADDED one.
# The caller tried twice more, each time with a count, and watched their order go:
#
#     "I need biryani naan and paneer butter masala"          -> 1 paneer  (and biryani dropped)
#     "remove paneer butter masala and add chicken butter"    -> 2 paneer
#     "remove the two paneer butter masala, add butter chicken" -> 4 paneer
#
# A removal that adds is worse than no removal at all: the caller is actively correcting it and
# every attempt makes it worse.

@pytest.mark.parametrize("said", [
    "remove the paneer butter masala",
    "take the paneer butter masala off my order",
    "take off the paneer butter masala",
    "get rid of the paneer butter masala",
    "cancel the paneer butter masala from my order",
    "leave out the paneer butter masala",
])
def test_removing_a_dish_never_adds_it(said):
    decision = route(said, ordering=True)
    assert decision is not None, f"{said!r} reached the model"
    assert decision.tool == "remove_from_order", (
        f"{said!r} went to {decision.tool} -- a removal that adds is the reported bug"
    )


def test_a_count_on_a_removal_says_how_many_come_off_not_how_many_go_on():
    """"Remove the TWO paneer butter masala" read the count as how many to ADD. That is what
    turned each correction into a doubling."""
    decision = route("remove the two paneer butter masala", ordering=True)
    assert decision.tool == "remove_from_order"
    assert decision.params["quantity"] == 2


def test_naming_a_dish_does_not_cancel_the_whole_order():
    """"Cancel the chicken kebab from my order" wiped everything. Naming a dish makes it a
    removal; cancelling the lot has to be said without one."""
    assert route("cancel the chicken kebab from my order", ordering=True).tool \
        == "remove_from_order"
    assert route("cancel my order", ordering=True).tool == "cancel_order"


def test_every_dish_in_one_sentence_goes_on_the_order():
    """The caller said three things and one was added. "Naan" is not on the menu and is invisible
    to the router; the other two must both land."""
    decision = route("I need biryani and paneer butter masala", ordering=True)
    assert decision.tool == "add_to_order"
    ordered = {d["dish"] for d in decision.params["dishes"]}
    assert ordered == {"vegetable biryani", "paneer butter masala"}


def test_remove_this_and_add_that_does_both():
    """A swap is one sentence and two intentions. Doing only the removal leaves the caller to ask
    again for the dish they just asked for."""
    decision = route("remove the paneer butter masala and add a butter chicken", ordering=True)
    assert decision.tool == "remove_from_order"
    assert decision.params["dish"] == "paneer butter masala"
    assert [d["dish"] for d in decision.params["then_add"]] == ["butter chicken"]


def test_removing_takes_the_dish_off_and_leaves_the_rest(orders):
    kebab, chai = _dish(name="Chicken Kebab"), _dish(name="Masala Chai")
    orders.add_to_order(dish=kebab.name)
    orders.add_to_order(dish=chai.name, quantity=2)

    order, name, went = orders.remove_from_order(dish=kebab.name)
    assert name == kebab.name and went == 1
    assert [line.name for line in order.lines] == [chai.name]
    assert order.total == 2 * chai.price


def test_removing_some_of_a_line_leaves_the_others(orders):
    chai = _dish(name="Masala Chai")
    orders.add_to_order(dish=chai.name, quantity=3)
    order, _name, went = orders.remove_from_order(dish=chai.name, quantity=1)
    assert went == 1
    assert order.lines[0].quantity == 2


def test_removing_the_last_dish_empties_the_order(orders):
    kebab = _dish(name="Chicken Kebab")
    orders.add_to_order(dish=kebab.name)
    order, _name, _went = orders.remove_from_order(dish=kebab.name)
    assert order is None


def test_removing_something_that_is_not_on_the_order_says_so(orders):
    orders.add_to_order(dish=_dish(name="Chicken Kebab").name)
    with pytest.raises(BookingRefused) as refused:
        orders.remove_from_order(dish=_dish(name="Masala Chai").name)
    assert refused.value.code == "not_on_the_order"
    assert orders.current_order().lines[0].name == _dish(name="Chicken Kebab").name


def test_a_fenced_removal_leaves_the_dish_on_the_order(tmp_path):
    """Removing is a write, so it inherits the invariant: a caller who changes their mind while it
    is in flight leaves the order exactly as it was."""
    store = _hotel(tmp_path)
    kebab = _dish(name="Chicken Kebab")
    store.bookings.add_to_order(dish=kebab.name)

    result = _fenced_runner(store).run(
        "remove_from_order", gen="g", turn_id=1, is_valid=lambda: False, dish=kebab.name)

    assert not result.may_speak
    assert [line.name for line in store.bookings.current_order().lines] == [kebab.name]


def test_the_reported_call_no_longer_makes_it_worse(monkeypatch, tmp_path):
    """The conversation exactly as it happened, end to end."""
    from tests.test_menu_routing import build

    spike, _t, rime, llm = build(monkeypatch, "")
    private = _hotel(tmp_path)
    spike.menu = private
    spike.tools.store = private

    for said in ("Can you write my dinner order down? I need biryani naan and paneer butter"
                 " masala.",
                 "Can you remove paneer butter masala and add chicken butter?",
                 "Remove the two paneer butter masala. I need you to add a butter chicken."):
        spike.stt.text = said
        spike.handle_utterance(AUDIO, 0.0)

    on_it = {line.name: line.quantity for line in private.bookings.current_order().lines}
    assert "Paneer Butter Masala" not in on_it, (
        f"the dish the caller removed twice is still on the order: {on_it}"
    )
    assert on_it == {"Vegetable Biryani": 1, "Butter Chicken": 1}, on_it
    assert llm.calls == [], "an order must never reach the model"


# ============================ ordering in a list ============================
#
# REPORTED FROM A REAL CALL: "I need two plates of biryani, one plate of kandhuri grill, two plates
# of paneer butter masala and four naan." AETHER answered "I have added Paneer Butter Masala. That
# is Paneer Butter Masala, four hundred and eighty rupees so far."
#
# Four items asked for, one added, one of a dish the caller wanted two of, and nothing said about
# the two that are not on the menu. On a telephone that reads as an order that worked.

def test_a_count_survives_a_unit_word():
    """"Two plates of biryani" is two. The number was not adjacent to the dish, so it read as one
    -- a party ordering two of everything got one of everything and only found out from the total."""
    assert route("order two plates of biryani", ordering=True).params["quantity"] == 2
    assert route("three portions of the chicken kebab", ordering=True).params["quantity"] == 3
    assert route("two bowls of fresh fruit bowl", ordering=True).params["quantity"] == 2


def test_a_count_works_on_a_dish_the_caller_named_by_its_short_name():
    """The quantity was searched for in front of the MENU's name. A caller saying "biryani" for
    Vegetable Biryani put the number in front of a phrase that is not in the sentence at all, so
    every shorthand dish silently came back as one."""
    assert route("order two biryani", ordering=True).params["quantity"] == 2
    assert route("order three kebab", ordering=True).params["quantity"] == 3


def test_a_number_somewhere_else_in_the_sentence_is_still_not_a_quantity():
    """The guard that makes the rule above safe."""
    assert route("order the chicken kebab for room two zero five",
                 ordering=True).params["quantity"] == 1


def test_the_whole_reported_list_is_taken_with_its_counts():
    decision = route(
        "I need two plates of biryani, one plate of kandhuri grill, two plates of paneer butter"
        " masala and four naan", ordering=True)
    assert decision.tool == "add_to_order"
    got = {d["dish"]: d["quantity"] for d in decision.params["dishes"]}
    assert got == {"vegetable biryani": 2, "paneer butter masala": 2}, got
    assert set(decision.params["unknown"]) == {"kandhuri grill", "naan"}, decision.params


def test_what_the_hotel_does_not_have_is_said_rather_than_dropped(tmp_path):
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.tools import ToolRunner
    from aether.trace import Trace

    store = _hotel(tmp_path)
    decision = route(
        "I need two plates of biryani, one plate of kandhuri grill, two plates of paneer butter"
        " masala and four naan", ordering=True)
    said = render(ToolRunner(Trace(), store, tools=HOTEL_TOOLS).run(
        decision.tool, gen="g", turn_id=1, is_valid=lambda: True, **decision.params))

    assert "kandhuri grill" in said and "naan" in said, said
    assert "two Vegetable Biryani" in said and "two Paneer Butter Masala" in said, said


@pytest.mark.parametrize("said", [
    "book a table for four people",
    "book a deluxe king for two nights",
    "order the chicken kebab for room two zero five",
    "order two chicken kebab and three masala chai",
])
def test_ordinary_sentences_report_nothing_as_missing(said):
    """The expensive direction. Telling a caller their dish is not on a menu that has it, or that
    "four people" is not on the menu, is worse than saying nothing -- so when this is unsure it
    stays quiet, which is the behaviour it replaces."""
    from aether.hotel._foreign import to_router_language
    from aether.hotel.router import _find_unknown_items, normalise

    assert _find_unknown_items(to_router_language(normalise(said))) == [], said


def test_chasing_a_dish_reads_the_order_back_rather_than_ordering_it_again():
    """"I also ordered two plates of biryani. Where is it?" added two more, taking the caller's
    two to four.

    A caller chasing something they believe they ordered is the last person who should be given
    more of it. The read-back answers the question either way -- if it is on the order they hear
    it, and if it is not they hear that too and can ask for it.
    """
    for said in ("I also ordered two plates of biryani. Where is it?",
                 "I already ordered the chicken kebab",
                 "where is my biryani",
                 "I ordered two biryani earlier"):
        decision = route(said, ordering=True)
        assert decision is not None, said
        assert decision.tool == "repeat_order", f"{said!r} went to {decision.tool}"


def test_actually_ordering_is_still_ordering():
    """The guard on the rule above: present-tense requests are unaffected."""
    for said in ("order two biryani", "I would like to order the chicken kebab",
                 "and two masala chai", "I'll have the paneer tikka"):
        assert route(said, ordering=True).tool == "add_to_order", said


# ============================ editing an order, every phrasing ============================
#
# Two more from a real call:
#
#   "Can you remove one vegetable biryani?"  -> took off BOTH of the caller's two.
#   "Can you replace my paneer butter and masala for butter chicken?"  -> added the butter chicken
#                                                                        and left the paneer on.
#
# The first was a missing distinction: `_find_quantity` defaults a missing number to one, so "no
# number" and "the number one" were the same value, and the removal rule read both as "all of them".
# The second was a missing verb: `replace`, `swap` and `change ... to` matched no removal word at
# all, so both dishes were read as an order.
#
# The table below is the sweep those fixes came from. It is wide rather than deep on purpose: a
# phrasing that stops working shows up here rather than on a call.

_EDIT_CASES = [
    # (sentence, tool, dish going out, dishes coming in)
    ("order the chicken kebab", "add_to_order", None, ["chicken kebab"]),
    ("two plates of biryani please", "add_to_order", None, ["vegetable biryani"]),
    ("and two masala chai", "add_to_order", None, ["masala chai"]),
    ("order a kebab and a brownie", "add_to_order", None,
     ["chicken kebab", "chocolate brownie"]),

    ("remove the vegetable biryani", "remove_from_order", "vegetable biryani", []),
    ("remove one vegetable biryani", "remove_from_order", "vegetable biryani", []),
    ("take the paneer tikka off my order", "remove_from_order", "paneer tikka", []),
    ("take off one masala chai", "remove_from_order", "masala chai", []),
    ("cancel the chicken kebab from my order", "remove_from_order", "chicken kebab", []),
    ("get rid of the brownie", "remove_from_order", "chocolate brownie", []),
    ("leave out the paneer tikka", "remove_from_order", "paneer tikka", []),
    ("drop the masala chai", "remove_from_order", "masala chai", []),

    ("replace the paneer butter masala with butter chicken", "remove_from_order",
     "paneer butter masala", ["butter chicken"]),
    ("swap the chicken kebab for the paneer tikka", "remove_from_order",
     "chicken kebab", ["paneer tikka"]),
    ("change the masala chai to a fresh lime soda", "remove_from_order",
     "masala chai", ["fresh lime soda"]),
    ("I want butter chicken instead of paneer butter masala", "remove_from_order",
     "paneer butter masala", ["butter chicken"]),
    ("remove the biryani and add a butter chicken", "remove_from_order",
     "vegetable biryani", ["butter chicken"]),

    ("repeat my order", "repeat_order", None, []),
    ("read my order back", "repeat_order", None, []),
    ("where is my chicken kebab", "repeat_order", None, []),
    ("that's all, place the order", "place_order", None, []),
    ("send it to the kitchen", "place_order", None, []),
    ("cancel my order", "cancel_order", None, []),
    ("forget the whole order", "cancel_order", None, []),

    # Nothing here may touch the order.
    ("how much is the chicken kebab", "price_of", None, []),
    ("is the fish curry off today", "check_availability", None, []),
    ("what desserts do you have", "list_category", None, []),
    ("does the butter chicken contain nuts", "check_allergens", None, []),
    ("book a table for four at eight", "reserve_table", None, []),
    ("is room three zero five free", "room_status", None, []),
]


@pytest.mark.parametrize(("said", "tool", "going", "coming"), _EDIT_CASES,
                         ids=[c[0] for c in _EDIT_CASES])
def test_every_way_of_editing_an_order(said, tool, going, coming):
    decision = route(said, ordering=True)
    assert decision is not None, f"{said!r} reached the model"
    assert decision.tool == tool, f"{said!r} went to {decision.tool}"

    if decision.tool == "remove_from_order":
        assert decision.params["dish"] == going
        assert [d["dish"] for d in decision.params.get("then_add", [])] == coming
    elif decision.tool == "add_to_order":
        added = ([d["dish"] for d in decision.params["dishes"]]
                 if "dishes" in decision.params else [decision.params["dish"]])
        assert added == coming


def test_no_number_means_the_whole_line_and_one_means_one(orders):
    """The distinction that was missing. A caller with two biryani who asks to remove one keeps
    one; a caller who asks to remove "the" biryani keeps none."""
    biryani = _dish(name="Vegetable Biryani")

    orders.add_to_order(dish=biryani.name, quantity=2)
    assert route("remove one vegetable biryani", ordering=True).params["quantity"] == 1
    order, _name, went = orders.remove_from_order(dish=biryani.name, quantity=1)
    assert went == 1 and order.lines[0].quantity == 1

    assert route("remove the vegetable biryani", ordering=True).params["quantity"] is None
    order, _name, went = orders.remove_from_order(dish=biryani.name, quantity=None)
    assert order is None, "the whole line should have gone"


def test_a_caller_padding_a_dish_name_is_still_understood():
    """"Paneer butter AND masala" -- said by a real caller. It matched nothing, so the dish being
    replaced stayed on the order while the replacement was added beside it."""
    decision = route("replace my paneer butter and masala for butter chicken", ordering=True)
    assert decision.tool == "remove_from_order"
    assert decision.params["dish"] == "paneer butter masala"
    assert [d["dish"] for d in decision.params["then_add"]] == ["butter chicken"]


def test_the_dish_going_out_is_never_also_the_dish_coming_in():
    """"Butter chicken INSTEAD OF paneer butter masala": "instead of" is a removal cue and
    "instead" a swap preposition, and they start at the same character. On that tie the paneer was
    counted as both, and went straight back onto the order it had just come off."""
    for said in ("I want butter chicken instead of paneer butter masala",
                 "replace the paneer butter masala with butter chicken",
                 "swap the chicken kebab for the paneer tikka"):
        decision = route(said, ordering=True)
        coming = [d["dish"] for d in decision.params.get("then_add", [])]
        assert decision.params["dish"] not in coming, said


@pytest.mark.parametrize(("code", "said", "tool"), [
    ("hin", "मसाला चाय हटा दीजिए", "remove_from_order"),
    ("hin", "एक मसाला चाय हटाइए", "remove_from_order"),
    ("hin", "मेरा ऑर्डर दोहराइए", "repeat_order"),
    ("hin", "दो मसाला चाय ऑर्डर कीजिए", "add_to_order"),
    ("spa", "quite el masala chai", "remove_from_order"),
    ("spa", "elimine el masala chai de mi pedido", "remove_from_order"),
    ("spa", "repita mi pedido", "repeat_order"),
    ("spa", "pedir dos masala chai", "add_to_order"),
])
def test_editing_an_order_works_in_every_language(code, said, tool):
    """RULES.md R8b.6: an English-only hotel capability is a defect, and removal was English-only
    when it was written."""
    decision = route(said, ordering=True)
    assert decision is not None, f"[{code}] {said!r} reached the model"
    assert decision.tool == tool, f"[{code}] {said!r} went to {decision.tool}"


# ============================ and what we DO have ============================
#
# "We do not have naan" on its own leaves a caller holding a menu they cannot see, on a line with
# no way to browse it. Three reasons a dish cannot go on, and they deserve three different answers:
# a dish the kitchen has run out of, a dish the recogniser mangled, and a dish this hotel simply
# does not serve.

def _order(store, said):
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.tools import ToolRunner
    from aether.trace import Trace

    decision = route(said, ordering=True)
    assert decision is not None, f"{said!r} reached the model"
    return render(ToolRunner(Trace(), store, tools=HOTEL_TOOLS).run(
        decision.tool, gen="g", turn_id=1, is_valid=lambda: True, **decision.params))


def test_a_dish_we_do_not_serve_is_followed_by_what_we_do(tmp_path):
    said = _order(_hotel(tmp_path), "order two plates of naan and one paneer butter masala")

    assert "Paneer Butter Masala" in said, said
    assert "naan" in said, said
    # The way back into a menu the caller cannot see.
    for category in STORE.categories():
        assert category in said, f"{category!r} missing from: {said}"


def test_a_misheard_dish_is_answered_with_the_real_one(tmp_path):
    """"Chiken kebap" is the Chicken Kebab. Naming the real dish IS the answer, so the categories
    would only be noise after it."""
    said = _order(_hotel(tmp_path), "order a chiken kebap and a masala chai")

    assert "chiken kebap" in said and "Chicken Kebab" in said, said
    assert "starters" not in said, f"the categories were read out unnecessarily: {said}"


def test_a_sold_out_dish_is_not_confused_with_one_we_do_not_serve(tmp_path):
    sold_out = _dish(available=False)
    said = _order(_hotel(tmp_path), f"order the {sold_out.name.lower()} and a masala chai")

    assert "off today" in said, said
    assert "do not have" not in said, f"a real dish was reported as missing: {said}"


def test_missing_dishes_are_grouped_into_one_clause(tmp_path):
    """"We do not have kandhuri grill on the menu. We do not have naan on the menu." is the same
    sentence twice down a telephone."""
    said = _order(_hotel(tmp_path),
                  "order one plate of kandhuri grill, four naan and a masala chai")

    assert said.count("We do not have") == 1, said
    assert "kandhuri grill" in said and "naan" in said, said


def test_an_ordinary_order_says_nothing_about_missing_dishes(tmp_path):
    said = _order(_hotel(tmp_path), "order two chicken kebab and three masala chai")
    assert "do not have" not in said and "We do have" not in said, said


@pytest.mark.parametrize("code", ["eng", "hin", "spa"])
def test_every_language_names_what_is_missing_and_what_we_have(code, tmp_path):
    """RULES.md R8b.6 again: a caller stranded in Hindi needs the way back into the menu too."""
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.lang import by_code
    from aether.tools import ToolRunner
    from aether.trace import Trace

    decision = route("order two plates of naan and one paneer butter masala", ordering=True)
    said = render(ToolRunner(Trace(), _hotel(tmp_path), tools=HOTEL_TOOLS).run(
        decision.tool, gen="g", turn_id=1, is_valid=lambda: True, **decision.params),
        by_code(code))

    assert "naan" in said, f"[{code}] {said}"
    assert "Paneer Butter Masala" in said, f"[{code}] {said}"
    assert len(said.split()) > 12, f"[{code}] the answer lost its detail: {said}"
