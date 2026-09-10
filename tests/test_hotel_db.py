"""The hotel database, and every answer AETHER draws from it.

Replaces `test_hotel_menu.py`, which tested a hand-written fixture that no longer exists.

Three layers, and the third is the one that matters:

* **the store** -- does SQLite give back the right rows, and does it refuse the wrong ones?
* **the tools** -- do they turn rows into the right facts, through the real `ToolRunner` with real
  fencing?
* **the spoken answer** -- is what Rime would actually say correct, and sayable?

The property under test throughout is that **no hotel fact is written in Python**. Prices, room
numbers, allergens, opening hours and extensions all come from `data/aether_hotel.db`, so these
tests read their expectations from the database too. A test that hardcoded "four hundred and twenty
rupees" would pass while the database said something else, which is exactly the failure the
database was brought in to prevent.
"""

from __future__ import annotations

import sqlite3

import pytest

from aether.hotel import (
    DEFAULT_DB_PATH,
    HotelDB,
    HotelStore,
    UnknownRecord,
    menu_for_prompt,
    say_a,
    say_date,
    say_list,
    say_number,
    say_price,
    say_room_number,
    say_time,
)
from aether.hotel.router import route
from aether.hotel.tools import HOTEL_TOOLS, NOT_FOUND, render
from aether.tools import ToolRunner
from aether.trace import Trace


@pytest.fixture(scope="module")
def store():
    return HotelStore()


@pytest.fixture
def runner(store):
    """The REAL ToolRunner: real fence check, real delay hook, real identity stamping."""
    return ToolRunner(Trace(), store, tools=HOTEL_TOOLS)


def ask(runner, question):
    """Route a question and speak the answer, exactly as `Day1Spike._menu_answer` does."""
    decision = route(question)
    if decision is None:
        return None, None
    result = runner.run(decision.tool, gen="G1", turn_id=1, is_valid=lambda: True,
                        **decision.params)
    return decision.tool, render(result)


def raw(sql, params=()):
    """Query the database directly, so a test's expectation comes from the data and not from me."""
    db = sqlite3.connect(f"file:{DEFAULT_DB_PATH.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


# ============================ the database itself ============================

def test_the_database_is_present_and_is_the_source_of_truth():
    assert DEFAULT_DB_PATH.exists(), f"the hotel database must exist at {DEFAULT_DB_PATH}"


def test_the_connection_is_read_only_and_enforced(store):
    """Not a promise -- SQLite refuses. Reservation creation and order placement are out of scope,
    and this layer could not perform them if asked."""
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.db._db.execute("UPDATE menu_items SET price_inr = 1")


def test_a_missing_database_fails_loudly_rather_than_answering_around_it():
    from aether.hotel import HotelDataUnavailable

    with pytest.raises(HotelDataUnavailable):
        HotelDB("data/there_is_no_such_hotel.db")


def test_no_hotel_fact_is_hardcoded_in_the_tools():
    """Structural guard. Every price, room number, extension and opening time must come from the
    database; a literal in this file is a fact that can silently disagree with it."""
    import inspect
    import io
    import re
    import tokenize

    import aether.hotel.tools as mod

    src = inspect.getsource(mod)
    numbers = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.NUMBER
    ]
    # Single digits only: list caps, indices, small counts. Every hotel fact in this database is
    # two digits or more -- the cheapest thing on the menu is eighty rupees and the lowest room
    # number is 101 -- so a multi-digit literal here is a fact that can silently disagree with the
    # database.
    for value in numbers:
        assert re.fullmatch(r"\d", value), (
            f"{value!r} looks like a hotel fact hardcoded in tools.py"
        )


# ============================ menu ============================

def test_the_menu_matches_the_database_exactly(store):
    rows = raw("SELECT COUNT(*) n FROM menu_items")
    assert len(store.menu()) == rows[0]["n"]
    for item in store.menu():
        db_row = raw("SELECT price_inr, available FROM menu_items WHERE item_id = ?",
                     (item.item_id,))[0]
        assert item.price == db_row["price_inr"]
        assert item.available == bool(db_row["available"])


def test_categories_come_from_the_database_not_from_an_enum(store):
    expected = [r["name"].lower() for r in
                raw("SELECT name FROM menu_categories ORDER BY display_order")]
    assert store.categories() == expected


def test_listing_a_category_speaks_its_available_items(runner, store):
    tool, said = ask(runner, "What starters do you have?")
    assert tool == "list_category"
    for item in store.in_category("starters"):
        assert item.name in said


def test_a_sold_out_item_is_named_as_off_today_not_omitted_silently(runner, store):
    """The caller hears that it exists and is unavailable, which is a different fact from
    never having existed."""
    sold_out = [i for i in store.menu() if not i.available]
    assert sold_out, "the database must keep at least one sold-out item for this demo"
    tool, said = ask(runner, "What starters do you have?")
    starters_off = [i.name for i in store.in_category("starters", available_only=False)
                    if not i.available]
    for name in starters_off:
        assert name in said and "off today" in said


def test_the_price_spoken_is_the_price_stored(runner, store):
    item = store.find_item("chicken kebab")
    tool, said = ask(runner, "How much is the chicken kebab?")
    assert tool == "price_of"
    assert say_price(item.price) in said


def test_asking_for_a_sold_out_item_gets_an_honest_no(runner, store):
    sold_out = next(i for i in store.menu() if not i.available)
    tool, said = ask(runner, f"Is the {sold_out.name.lower()} available?")
    assert tool == "check_availability"
    assert "not available today" in said


def test_an_unknown_dish_is_admitted_not_invented(runner):
    tool, said = ask(runner, "How much is the lobster thermidor?")
    # Either it does not route at all, or the tool admits it. Never a made-up price.
    assert tool is None or said == NOT_FOUND


# --- dietary ---

def test_vegan_options_are_exactly_the_vegan_rows(runner, store):
    tool, said = ask(runner, "Do you have vegan options?")
    assert tool == "find_by_diet"
    for item in store.by_diet("vegan"):
        assert item.name in said
    for item in store.menu():
        if item.available and not item.vegan:
            assert item.name not in said


def test_vegetarian_includes_vegan_because_a_caller_can_eat_both(store):
    vegetarian = {i.name for i in store.by_diet("vegetarian")}
    for item in store.by_diet("vegan"):
        assert item.name in vegetarian, "excluding vegan would be technically-correct and wrong"


def test_a_dietary_question_can_be_narrowed_to_a_category(runner, store):
    tool, said = ask(runner, "Do you have vegetarian mains?")
    assert tool == "find_by_diet"
    for item in store.by_diet("vegetarian", category="vegetarian mains"):
        assert item.name in said
    assert "vegetarian vegetarian" not in said, "the category already names the diet"


# --- allergens ---

def test_allergens_are_read_from_the_database(runner, store):
    item = store.find_item("paneer butter masala")
    assert item.allergens, "this dish is the allergen fixture; it must list some"
    tool, said = ask(runner, "Does the paneer butter masala contain nuts?")
    assert tool == "check_allergens"
    for allergen in item.allergens:
        assert allergen in said


def test_an_allergy_with_no_dish_named_suggests_only_safe_dishes(runner, store):
    tool, said = ask(runner, "I have a nut allergy, what can I eat?")
    assert tool == "safe_for"
    for item in store.menu():
        if item.available and "nuts" in item.allergens:
            assert item.name not in said, f"{item.name} contains nuts and must not be suggested"


def test_an_allergen_the_menu_does_not_record_raises_rather_than_reassuring(runner):
    """"Nothing contains it" is the one wrong answer here that could put somebody in hospital."""
    result = runner.run("safe_for", gen="G1", turn_id=1, is_valid=lambda: True,
                        allergen="celery")
    assert render(result) == NOT_FOUND


# --- description, since the database records no spice level ---

def test_a_question_about_spice_reads_the_hotels_own_description(runner, store):
    """The old fixture had a spice column. The database does not, and faking one would be the
    exact class of invention this path exists to prevent."""
    item = store.find_item("paneer tikka")
    tool, said = ask(runner, "Is the paneer tikka spicy?")
    assert tool == "describe_item"
    assert said == item.description


def test_no_tool_claims_to_know_a_spice_level():
    assert "find_by_spice" not in HOTEL_TOOLS
    assert "spice_of" not in HOTEL_TOOLS


# ============================ rooms ============================

def test_the_hotel_has_the_rooms_the_database_says(store):
    assert len(store.rooms()) == raw("SELECT COUNT(*) n FROM rooms")[0]["n"]


def test_a_room_lookup_returns_that_rooms_real_status(runner, store):
    room = store.rooms()[0]
    tool, said = ask(runner, f"Is room {room.number} free?")
    assert tool == "room_status"
    assert say_room_number(room.number) in said


@pytest.mark.parametrize("status", ["available", "occupied"])
def test_each_room_status_is_spoken_distinctly(runner, store, status):
    room = next((r for r in store.rooms() if r.status == status), None)
    if room is None:
        pytest.skip(f"the database has no room with status {status!r}")
    _tool, said = ask(runner, f"Is room {room.number} free?")
    if status == "available":
        assert "free" in said and say_price(room.rate) in said
    else:
        assert "free" not in said


def test_an_unknown_room_is_admitted_not_answered_around(runner, store):
    """"Is room four one two free" -- a number this hotel does not have. It must not be answered
    with general availability, which is what the router did before the tool was allowed to see it."""
    known = {r.number for r in store.rooms()}
    missing = next(str(n) for n in range(100, 999) if str(n) not in known)
    tool, said = ask(runner, f"Is room {missing} free?")
    assert tool == "room_status"
    assert said == NOT_FOUND


def test_room_availability_counts_only_available_rooms(runner, store):
    tool, said = ask(runner, "Do you have anything free tonight?")
    assert tool == "room_availability"
    assert say_number(len(store.available_rooms())) in said


def test_availability_can_be_narrowed_to_a_room_type(runner, store):
    tool, said = ask(runner, "Do you have any suites free?")
    assert tool == "room_availability"
    free = store.available_rooms("Executive Suite")
    assert say_number(len(free)) in said or len(free) == 1


def test_room_types_and_their_rates_come_from_the_database(runner, store):
    tool, said = ask(runner, "What rooms do you have?")
    assert tool == "list_room_types"
    for room_type in store.room_types():
        assert room_type.name in said or len(store.room_types()) > 6
    cheapest = min(store.room_types(), key=lambda t: t.rate)
    assert say_price(cheapest.rate) in said


def test_a_room_price_is_the_stored_rate(runner, store):
    room_type = store.room_type("Executive Suite")
    tool, said = ask(runner, "How much is an executive suite?")
    assert tool == "room_price"
    assert say_price(room_type.rate) in said
    assert say_number(room_type.max_guests) in said


def test_room_amenities_are_the_stored_amenities(runner, store):
    room_type = store.room_type("Deluxe King")
    tool, said = ask(runner, "What comes with a deluxe king?")
    assert tool == "room_amenities"
    for amenity in room_type.amenities:
        assert amenity.lower() in said.lower()


def test_an_unknown_room_type_raises(store):
    with pytest.raises(UnknownRecord):
        store.room_type("Presidential Penthouse")


# ============================ services, timings, reservations ============================

def test_services_are_listed_from_the_database(runner, store):
    tool, said = ask(runner, "What services do you offer?")
    assert tool == "list_services"
    assert len(store.services()) == raw("SELECT COUNT(*) n FROM hotel_services")[0]["n"]


def test_service_hours_and_extension_are_the_stored_ones(runner, store):
    service = store.service("Room Service")
    tool, said = ask(runner, "When is room service available?")
    assert tool == "service_hours"
    opens, _, closes = service.availability.partition("-")
    assert say_time(opens) in said and say_time(closes) in said
    assert say_room_number(service.extension) in said


def test_a_round_the_clock_service_is_not_read_as_a_time_range(runner, store):
    service = next((s for s in store.services() if "24" in s.availability), None)
    if service is None:
        pytest.skip("the database has no 24-hour service")
    result = runner.run("service_hours", gen="G1", turn_id=1, is_valid=lambda: True,
                        service=service.name)
    assert "around the clock" in render(result)


def test_check_in_and_check_out_come_from_the_hotel_row(runner, store):
    info = store.hotel()
    tool, said = ask(runner, "What time is check in?")
    assert tool == "check_in_out"
    assert say_time(info.check_in_time) in said
    assert say_time(info.check_out_time) in said


def test_a_reservation_is_reported_by_room_and_never_by_guest_name(runner, store):
    """A hotel line answers to whoever dials it. The guest's name is in the record and must stay
    there -- reading it to an unauthenticated caller is a disclosure this product will not make."""
    reservation = store.reservations()[0]
    tool, said = ask(runner, f"Is there a booking on room {reservation.room_number}?")
    assert tool == "reservation_for_room"
    assert say_room_number(reservation.room_number) in said
    assert reservation.guest_name not in said
    first_name = reservation.guest_name.split()[0]
    assert first_name not in said, "not even the first name"


def test_a_room_with_no_reservation_is_admitted(runner, store):
    booked = {r.room_number for r in store.reservations()}
    free_room = next(r for r in store.rooms() if r.number not in booked)
    tool, said = ask(runner, f"Is there a booking on room {free_room.number}?")
    assert said == NOT_FOUND


# ============================ Gemini must not be asked ============================

DETERMINISTIC_QUESTIONS = [
    "What is on the menu?", "What starters do you have?", "What desserts do you have?",
    "How much is the chicken kebab?", "Is the fish curry available?",
    "Do you have vegetarian mains?", "Do you have vegan options?",
    "Does the paneer butter masala contain nuts?", "I have a nut allergy, what can I eat?",
    "Is the paneer tikka spicy?",
    "Is room 301 available?", "Is room three oh five free?", "What rooms do you have?",
    "How much is an executive suite?", "What comes with a deluxe king?",
    "Do you have any suites free?", "Do you have anything free tonight?",
    "Is there a booking on room 302?",
    "What services do you offer?", "When is room service available?",
    "What time is check in?", "What time is check out?",
]


@pytest.mark.parametrize("question", DETERMINISTIC_QUESTIONS)
def test_a_database_question_never_reaches_the_model(question):
    """The whole point. These are facts, and a fact goes to a lookup."""
    assert route(question) is not None, f"{question!r} must be answered from the database"


# Questions the hotel genuinely holds no row for. "Do you have a swimming pool?" and "Do you have a
# gym?" used to live here and were MOVED OUT on 2026-09-10, because the database now answers them --
# which is the change working, not the test weakening. The assertion below is untouched; only the
# examples moved, and each replacement was checked to have no row behind it.
@pytest.mark.parametrize("question", [
    "What time do you close?", "Can I book a table for eight?",
    "Hello, how are you?", "Do you have a rooftop terrace?",
    "What is your star rating?", "Is there a temple nearby?",
])
def test_a_question_the_database_cannot_answer_still_reaches_the_model(question):
    """The router must stay conservative. A wrong tool is worse than a slower answer.

    "Is there parking?" and "Where are you located?" used to be in this list and have MOVED to the
    deterministic side: the database grew a `hotel_policies` table and a hotel-info tool, so they
    are now facts rather than guesses. The examples that remain are things this hotel genuinely has
    no record of, which is the property being tested.
    """
    assert route(question) is None


@pytest.mark.parametrize(("question", "tool"), [
    ("Is there parking?", "hotel_policy"),
    ("Do you have wifi?", "hotel_policy"),
    ("Can I bring my dog?", "hotel_policy"),
    ("Is breakfast included?", "hotel_policy"),
    ("Can I have a late check out?", "hotel_policy"),
    ("What is your cancellation policy?", "hotel_policy"),
    ("How can I pay?", "hotel_policy"),
    ("Where are you located?", "hotel_info"),
    ("How many floors does the hotel have?", "hotel_info"),
])
def test_the_commonest_hotel_questions_are_now_answered_from_the_database(question, tool):
    """These are what a hotel line is actually asked, and until the policies table existed every
    one of them reached the model with no fact behind it."""
    decision = route(question)
    assert decision is not None, f"{question!r} must not need the model"
    assert decision.tool == tool


def test_late_check_out_is_not_swallowed_by_the_check_in_rule():
    """"Late check out" contains the check-out words but is a different question: one asks a time,
    the other asks whether it is possible and what it costs."""
    assert route("what time is check out").tool == "check_in_out"
    assert route("can I have a late check out").tool == "hotel_policy"
    assert route("is early check in possible").params["topic"] == "early_check_in"


def test_every_deterministic_answer_is_speakable(runner):
    """No digits, no symbols, no markdown -- Rime is handed what a person would say."""
    import re

    for question in DETERMINISTIC_QUESTIONS:
        _tool, said = ask(runner, question)
        assert said, question
        assert not re.search(r"[*_`#|<>{}\[\]]", said), f"{question}: {said}"
        assert not re.search(r"\d", said), f"{question}: unspoken digits in {said!r}"


# ============================ the model's safety net ============================

def test_the_model_is_given_the_whole_hotel(store):
    """The router will miss again. When it does, the model must have facts rather than invent."""
    from aether.llm import SYSTEM_PROMPT

    for item in store.menu():
        assert item.name in SYSTEM_PROMPT
    for room_type in store.room_types():
        assert room_type.name in SYSTEM_PROMPT
    for service in store.services():
        assert service.name in SYSTEM_PROMPT
    assert "No other room numbers exist" in SYSTEM_PROMPT


def test_the_prompt_marks_what_is_unavailable_rather_than_hiding_it(store):
    from aether.llm import SYSTEM_PROMPT

    for item in store.menu():
        if not item.available:
            assert "NOT AVAILABLE TODAY" in SYSTEM_PROMPT


def test_the_prompt_is_generated_from_the_database_not_transcribed():
    """A hand-copied menu would drift from the database the moment either changed."""
    text = menu_for_prompt()
    assert "Chicken Kebab" in text and "Executive Suite" in text and "Room Service" in text


# ============================ spoken helpers ============================

def test_a_room_number_is_said_as_a_room_number_not_a_quantity():
    """A guest sent to "three hundred and five" has been given a number, not a door."""
    assert say_room_number("305") == "three oh five"
    assert say_room_number("101") == "one oh one"


def test_times_are_words_not_a_colon():
    assert say_time("06:00") == "six in the morning"
    assert say_time("12:00") == "twelve noon"
    assert say_time("23:00") == "eleven at night"
    assert say_time("00:00") == "twelve midnight"


def test_dates_are_words_not_an_iso_string():
    assert say_date("2026-09-07") == "the seventh of September"
    assert say_date("2026-09-12") == "the twelfth of September"


def test_the_article_agrees():
    assert say_a("Executive Suite") == "an Executive Suite"
    assert say_a("Deluxe King") == "a Deluxe King"


def test_prices_are_spoken_in_the_databases_currency(store):
    item = store.menu()[0]
    assert say_price(item.price).endswith("rupees")
    assert say_list(["a", "b", "c"]) == "a, b and c"


def test_every_hand_written_synonym_points_at_a_real_database_name():
    """The router maps colloquial words -- "suite", "reception", "bags" -- onto database names, and
    those targets are written by hand. Rename a room type in the database and the synonym would
    silently stop resolving, so a caller saying "a suite" would fall through to the model. This is
    the guard that turns that into a failing test instead of a bad call."""
    from aether.hotel.router import _CATEGORY_WORDS, _ROOM_TYPE_WORDS, _SERVICE_WORDS

    store = HotelStore()

    categories = set(store.categories())
    for word, target in _CATEGORY_WORDS:
        assert target in categories, f"{word!r} points at category {target!r}, which does not exist"

    room_types = {t.name for t in store.room_types()}
    for word, target in _ROOM_TYPE_WORDS:
        assert target in room_types, f"{word!r} points at room type {target!r}, which does not exist"

    services = {s.name for s in store.services()}
    for word, target in _SERVICE_WORDS:
        assert target in services, f"{word!r} points at service {target!r}, which does not exist"


# --- surviving the recogniser ------------------------------------------------------------------
#
# Every case below is an utterance from a real trace, or the shorthand form of one. The router used
# to answer only the tidy phrasing, and a phone call does not produce tidy phrasing.


def test_a_dish_can_be_named_by_its_shorthand():
    """"How much is the kebab?" is in a real trace, and it used to fall through to the model."""
    from aether.hotel.router import route

    for said, dish in [("how much is the kebab", "chicken kebab"),
                       ("how much is the brownie", "chocolate brownie"),
                       ("how much is the biryani", "vegetable biryani"),
                       ("is the curry available", "fish curry")]:
        decision = route(said)
        assert decision is not None, f"{said!r} must not need the model"
        assert decision.params["dish"] == dish, said


def test_an_ambiguous_shorthand_is_refused_rather_than_guessed():
    """The rule `HotelStore.find_item` already applies, applied one stage earlier.

    "the chicken" could be Butter Chicken or Chicken Kebab, and "the masala" could be Paneer Butter
    Masala or Masala Chai. Picking one would be the confident wrong answer this router exists to
    avoid, so both reach the model instead.
    """
    from aether.hotel.router import _DISH_SHORTHAND, route

    shorthand = dict(_DISH_SHORTHAND)
    assert "chicken" not in shorthand, "Butter Chicken and Chicken Kebab both claim it"
    assert "masala" not in shorthand, "Paneer Butter Masala and Masala Chai both claim it"
    assert route("how much is the chicken") is None
    assert route("how much is the masala") is None


def test_a_word_that_means_something_else_in_a_hotel_is_not_a_dish():
    """"Is there hot water?" is about plumbing. Answering it with the price of a bottle of Mineral
    Water would be confidently wrong, so `water` is held out of the shorthand table by name."""
    from aether.hotel.router import _DISH_SHORTHAND, route

    assert "water" not in dict(_DISH_SHORTHAND)
    assert route("is there hot water") is None


def test_the_recogniser_losing_the_e_from_suite_does_not_cost_a_turn():
    """From `run-20260908T154108Z`: the caller said "what comes with an executive suite?", it was
    heard as "executive suit", and the turn spent 1360 ms in Gemini answering what the database
    answers for nothing. "suite" is pronounced "sweet", so both spellings come back."""
    from aether.hotel.router import route

    for said in ("what comes with an executive suit", "what comes with an executive sweet"):
        decision = route(said)
        assert decision is not None and decision.tool == "room_amenities", said
        assert decision.params["room_type"] == "Executive Suite"
    assert route("how much is an executive suit").tool == "room_price"


def test_repairing_suite_does_not_break_sweets():
    """A bare "sweet" still means desserts -- the repair is anchored to a room-type word precisely
    so that "what sweets do you have" keeps its menu meaning."""
    from aether.hotel.router import route

    decision = route("what sweets do you have")
    assert decision is not None and decision.tool == "list_category"
    assert decision.params["category"] == "desserts"


def test_room_meaning_space_is_not_answered_as_room_types():
    """From a real call: "do you have room service" was heard as "Do you have room for this?", and
    the general-rooms rule answered it with the list of room types -- a confident answer to a
    question nobody asked. Falling through to the model is the correct behaviour here."""
    from aether.hotel.router import route

    assert route("do you have room for this") is None
    assert route("do you have room service").tool == "service_hours", "the real question still works"


# --- the database must not contradict itself --------------------------------------------------
#
# Referential and semantic consistency, asserted rather than assumed. A hotel that says 41 rooms are
# free in one place and lists 39 in another is worse than one that says nothing: the caller cannot
# tell which answer is the lie.


def _raw():
    import sqlite3

    from aether.hotel import DEFAULT_DB_PATH

    db = sqlite3.connect(f"file:{DEFAULT_DB_PATH.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def test_every_room_number_agrees_with_its_floor():
    """Room 305 is on floor 3. A room whose number and floor disagree would make two answers about
    the same room contradict each other."""
    db = _raw()
    try:
        wrong = [(r["room_number"], r["floor"]) for r in db.execute("select * from rooms")
                 if int(str(r["room_number"])[0]) != r["floor"]]
    finally:
        db.close()
    assert not wrong, f"room number and floor disagree: {wrong}"


def test_the_views_agree_with_the_tables_they_summarise():
    """`available_rooms` and `available_menu` are convenience views. If they drift from the base
    tables the hotel has two different opinions about what is free."""
    db = _raw()
    try:
        assert (db.execute("select count(*) from available_rooms").fetchone()[0]
                == db.execute("select count(*) from rooms where status='available'").fetchone()[0])
        assert (db.execute("select count(*) from available_menu").fetchone()[0]
                == db.execute("select count(*) from menu_items where available=1").fetchone()[0])
    finally:
        db.close()


def test_dietary_labels_do_not_contradict_each_other():
    """A vegan dish is necessarily vegetarian, and a vegetarian dish cannot declare fish.

    This is the allergy path's data integrity: `safe_for` suggests dishes to somebody avoiding an
    ingredient, so a mislabelled row is the one bug here that could actually hurt a person.
    """
    db = _raw()
    try:
        rows = db.execute("select * from menu_items").fetchall()
    finally:
        db.close()
    assert not [r["name"] for r in rows if r["vegan"] and not r["vegetarian"]]
    assert not [r["name"] for r in rows if r["vegetarian"] and "fish" in (r["allergens"] or "").lower()]


def test_reservations_reference_real_rooms_and_guests():
    db = _raw()
    try:
        rooms = {r["room_id"] for r in db.execute("select room_id from rooms")}
        guests = {g["guest_id"] for g in db.execute("select guest_id from guests")}
        res = db.execute("select * from reservations").fetchall()
    finally:
        db.close()
    for r in res:
        assert r["room_id"] in rooms, r["reservation_id"]
        assert r["guest_id"] in guests, r["reservation_id"]
        assert r["check_in"] < r["check_out"], r["reservation_id"]


def test_no_room_is_both_free_and_actively_booked():
    """The contradiction a caller would actually catch: told a room is free, then told it is booked."""
    db = _raw()
    try:
        booked = {r["room_id"] for r in db.execute(
            "select room_id from reservations where status in ('confirmed','checked_in')")}
        clash = [r["room_number"] for r in db.execute("select * from rooms")
                 if r["room_id"] in booked and r["status"] == "available"]
    finally:
        db.close()
    assert not clash, f"rooms marked available but actively booked: {clash}"


def test_a_policy_the_hotel_does_not_offer_is_not_also_priced():
    """"We do not offer currency exchange, and it costs 500 rupees" is not an answer."""
    db = _raw()
    try:
        bad = [p["topic"] for p in db.execute("select * from hotel_policies")
               if not p["available"] and p["fee_inr"] is not None]
    finally:
        db.close()
    assert not bad, bad
