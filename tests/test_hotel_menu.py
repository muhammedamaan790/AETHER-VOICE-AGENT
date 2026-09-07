"""The hotel menu fixture, its tools, and the spoken templates.

Two properties carry over from the warehouse layer and are re-asserted here because they are the
reason this reuses `ToolRunner` rather than growing a second one:

  * a fenced generation's result is never speakable, and
  * the caller never reaches the store except through a named tool.

Everything else is about answering correctly and saying it in a way a phone caller can understand:
prices as words, no symbols, and an honest "I could not find that" instead of a plausible guess.
"""

from __future__ import annotations

import pytest

from aether.events import EventType
from aether.hotel import (
    MENU,
    Category,
    Diet,
    MenuStore,
    UnknownDish,
    say_list,
    say_number,
    say_price,
)
from aether.hotel.tools import HOTEL_TOOLS, NOT_FOUND, render
from aether.tools import STALE_TOOL, ToolRunner
from aether.trace import Trace


@pytest.fixture
def store():
    return MenuStore()


@pytest.fixture
def runner(store):
    return ToolRunner(Trace(), store, tools=HOTEL_TOOLS)


# ============================ the fixture ============================

def test_the_menu_is_deterministic():
    a = [d.dish_id for d in MenuStore().dishes()]
    b = [d.dish_id for d in MenuStore().dishes()]
    assert a == b == [d.dish_id for d in MENU]


def test_every_category_has_something_to_offer(store):
    for category in Category:
        assert store.in_category(category), f"{category.value} must not be empty on a demo menu"


def test_an_unavailable_dish_exists_so_that_answer_shape_is_exercised(store):
    unavailable = [d for d in store.dishes() if not d.available]
    assert unavailable, "the fixture needs at least one sold-out dish"
    assert all(d.available for d in store.in_category(Category.STARTERS)), (
        "listing a category must hide what cannot be ordered"
    )


def test_dishes_are_snapshots_not_live_references(store):
    before = store.dish("D-101")
    store.set_available("D-101", False)
    assert before.available is True, "a record handed out earlier must not change under its holder"
    assert store.dish("D-101").available is False


def test_only_mutation_bumps_state_version(store):
    start = store.state_version
    store.dishes(); store.in_category(Category.MAINS); store.find("chicken kebab")
    assert store.state_version == start
    store.set_available("D-101", False)
    assert store.state_version > start


# ============================ finding a dish from speech ============================

@pytest.mark.parametrize("spoken", [
    "chicken kebab", "Chicken Kebab", "  chicken   kebab  ",
    "the chicken kebab", "chicken kebabs",
])
def test_a_dish_is_found_from_realistic_speech(store, spoken):
    """STT will not hand us the exact menu string, so containment matters."""
    assert store.find(spoken).dish_id == "D-101"


def test_an_unknown_dish_raises_rather_than_guessing(store):
    with pytest.raises(UnknownDish):
        store.find("lobster thermidor")


def test_an_ambiguous_name_raises_rather_than_picking_one(store):
    """Quoting the wrong price is worse than admitting we did not catch it."""
    with pytest.raises(UnknownDish, match="more than one"):
        store.find("chicken")          # matches kebab and butter chicken


# ============================ the tools ============================

def test_list_category_answers_the_opening_demo_question(runner):
    result = runner.run("list_category", gen="G1", category="starters")
    names = [r["name"] for r in result.records]
    assert "chicken kebab" in names
    assert "seafood platter" not in names, "a sold-out dish must not be offered"


@pytest.mark.parametrize("spoken", ["starters", "starter", "appetisers", "appetizer"])
def test_category_synonyms_a_caller_actually_uses(runner, spoken):
    assert runner.run("list_category", gen="G1", category=spoken).count > 0


def test_price_of(runner):
    result = runner.run("price_of", gen="G1", dish="chicken kebab")
    assert result.summary["price"] == 380.0


def test_vegetarian_includes_vegan(runner):
    """A caller asking for vegetarian wants everything they can eat, not a taxonomy lesson."""
    result = runner.run("find_by_diet", gen="G1", diet="vegetarian", category="mains")
    names = [r["name"] for r in result.records]
    assert "dal makhani" in names and "chana masala" in names   # veg and vegan
    assert "butter chicken" not in names


def test_check_availability_reports_sold_out(runner):
    assert runner.run("check_availability", gen="G1", dish="seafood platter").summary[
        "available"] is False


def test_an_unknown_dish_is_answerable_not_fatal(runner):
    """"We do not have that" is a true answer to a live question, so it may be spoken."""
    result = runner.run("price_of", gen="G1", dish="lobster thermidor")
    assert result.stale is False and result.may_speak is True
    assert result.reason == "unknown_record"
    assert result.records == [], "nothing is invented to fill the gap"


def test_no_menu_tool_can_change_the_menu(runner, store):
    """A caller asking about the menu must never be able to edit it."""
    before = store.state_version
    for name, (_fn, mutates) in HOTEL_TOOLS.items():
        assert mutates is False, f"{name} is exposed to callers and must not mutate"
    runner.run("list_category", gen="G1", category="mains")
    runner.run("price_of", gen="G1", dish="butter chicken")
    assert store.state_version == before


# ============================ spoken rendering ============================

@pytest.mark.parametrize(("n", "expected"), [
    (0, "zero"), (7, "seven"), (19, "nineteen"), (20, "twenty"), (42, "forty two"),
    (90, "ninety"), (100, "one hundred"), (180, "one hundred and eighty"),
    (380, "three hundred and eighty"), (890, "eight hundred and ninety"),
    (1000, "one thousand"), (1240, "one thousand two hundred and forty"),
])
def test_numbers_are_spoken_not_printed(n, expected):
    assert say_number(n) == expected


def test_prices_are_spoken_with_the_currency():
    assert say_price(380.0) == "three hundred and eighty rupees"


@pytest.mark.parametrize(("items", "expected"), [
    ([], ""), (["a"], "a"), (["a", "b"], "a and b"), (["a", "b", "c"], "a, b and c"),
])
def test_lists_are_spoken_the_way_a_person_reads_them(items, expected):
    assert say_list(items) == expected


def test_no_spoken_answer_contains_a_digit_or_symbol(runner):
    """It is going to Rime, so it must be words. A numeral is a gamble on the TTS engine."""
    import re

    spoken = [
        render(runner.run("list_category", gen="G1", category="starters")),
        render(runner.run("price_of", gen="G1", dish="chicken kebab")),
        render(runner.run("find_by_diet", gen="G1", diet="vegetarian", category="mains")),
        render(runner.run("check_availability", gen="G1", dish="seafood platter")),
    ]
    for line in spoken:
        assert line, "every tool must produce something sayable"
        assert not re.search(r"[\d*_`#|<>{}\[\]]", line), f"not speakable: {line!r}"


def test_the_price_answer_is_a_short_natural_sentence(runner):
    spoken = render(runner.run("price_of", gen="G1", dish="chicken kebab"))
    assert spoken == "The chicken kebab is three hundred and eighty rupees."


def test_a_sold_out_dish_is_said_plainly(runner):
    spoken = render(runner.run("check_availability", gen="G1", dish="seafood platter"))
    assert "not available" in spoken


def test_an_unknown_dish_is_admitted_not_invented(runner):
    assert render(runner.run("price_of", gen="G1", dish="lobster thermidor")) == NOT_FOUND


# ============================ fencing carries over ============================

def test_a_fenced_menu_lookup_is_never_spoken(store):
    """The golden invariant, on the hotel path: a stale answer produces no speech at all."""
    state = {"valid": True}

    def fence_during_sleep(_seconds):
        state["valid"] = False

    trace = Trace()
    runner = ToolRunner(trace, store, tools=HOTEL_TOOLS, delay_ms=500.0, sleep=fence_during_sleep)

    result = runner.run("price_of", gen="G1", dish="chicken kebab",
                        is_valid=lambda: state["valid"])

    assert result.stale is True and result.may_speak is False
    assert render(result) is None, "a stale result must render to nothing, not to a sentence"
    assert result.reason == STALE_TOOL
    assert trace.all(EventType.RESULT_RECEIVED) == []


def test_an_unfenced_lookup_does_speak(store):
    """The control: same delay, same path, generation still valid."""
    runner = ToolRunner(Trace(), store, tools=HOTEL_TOOLS, delay_ms=500.0, sleep=lambda _s: None)
    result = runner.run("price_of", gen="G1", dish="chicken kebab", is_valid=lambda: True)

    assert result.stale is False
    assert render(result) == "The chicken kebab is three hundred and eighty rupees."


def test_the_hotel_path_emits_only_canonical_events(store):
    trace = Trace()
    runner = ToolRunner(trace, store, tools=HOTEL_TOOLS)
    runner.run("list_category", gen="G1", category="desserts")
    runner.run("price_of", gen="G1", dish="nothing on this menu")

    known = {e.value for e in EventType}
    assert {e.type for e in trace.events} <= known


def test_the_runner_is_shared_with_the_warehouse_not_reimplemented():
    """One tool layer, one set of fencing semantics.

    Inspects CODE, not prose. Grepping raw source made this file's own docstring -- which names
    `ResultDiscarded` precisely to say the hotel does not emit it -- trip the guard that checks the
    hotel does not emit it.
    """
    import io
    import tokenize

    import aether.hotel.tools as hotel_tools

    src = open(hotel_tools.__file__, encoding="utf-8").read()
    code = " ".join(
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    for forbidden in ("ToolRunner", "EventType", "emit", "fence_now", "is_valid"):
        assert forbidden not in code, (
            f"the hotel must not reimplement {forbidden}; the shared runner owns it"
        )
    assert "HOTEL_TOOLS" in code, "it contributes a registry, and that is all"
