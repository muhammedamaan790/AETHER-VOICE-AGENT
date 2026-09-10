"""Taking a booking: the first thing in AETHER that changes the hotel, and the fence around it.

Until now nothing could write. `HotelStore` opened the database `mode=ro`, so a write was refused by
SQLite rather than by convention, and the documentation said so loudly. Bookings give that up -- and
the point of this file is that they give up as little as possible.

Three properties, in the order of how much damage getting them wrong would do:

1. **A fenced booking never lands.** A caller who changes their mind while the booking is in flight
   must leave no row behind. This is the strongest form of the project's whole claim: not merely
   that a stale answer is never spoken, but that a stale intention never happens.
2. **Facts are still unwritable, and SQLite still says so.** Prices, allergens, policies and room
   numbers are refused by the driver mid-statement, whatever the code asks for.
3. **The hotel does not contradict itself one turn later.** Book a room, ask if it is free, and the
   answer has to have changed.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from aether.events import EventType
from aether.hotel import HotelStore
from aether.hotel.bookings import BookingRefused, Bookings
from aether.hotel.router import route
from aether.hotel.tools import HOTEL_TOOLS, render
from aether.lang import ENGLISH, HINDI, SPANISH
from aether.tools import ToolRunner
from aether.trace import Trace

BOOKING_TOOLS = ("reserve_room", "reserve_table", "cancel_booking")


@pytest.fixture
def hotel(tmp_path: Path):
    """A private copy of the hotel, so one test's booking is invisible to the next."""
    from aether.hotel.db import default_db_path

    copy = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), copy)
    store = HotelStore(path=copy)
    yield store
    store.bookings.close()


@pytest.fixture
def runner(hotel):
    return ToolRunner(Trace(), hotel, tools=HOTEL_TOOLS)


def _speak(runner, tool, language=ENGLISH, **params) -> str:
    result = runner.run(tool, gen="g", turn_id=1, is_valid=lambda: True, **params)
    return render(result, language)


# --- 1. a fenced booking never lands -------------------------------------------------------------

def test_a_fenced_room_booking_leaves_no_reservation(hotel) -> None:
    """The claim that matters most, and the reason `ToolRunner` checks validity AFTER its delay and
    BEFORE the tool body: checking afterwards would leave the row written."""
    before = _count(hotel, "reservations")
    fenced = ToolRunner(Trace(), hotel, tools=HOTEL_TOOLS, delay_ms=50.0, sleep=lambda _s: None)

    result = fenced.run("reserve_room", gen="g", turn_id=1, is_valid=lambda: False,
                        room_type="Deluxe King")

    assert not result.may_speak, "a fenced booking must not be spoken"
    assert _count(hotel, "reservations") == before, "a fenced booking wrote a reservation"


def test_a_fenced_table_booking_leaves_no_row(hotel) -> None:
    before = _count(hotel, "table_bookings")
    fenced = ToolRunner(Trace(), hotel, tools=HOTEL_TOOLS, delay_ms=50.0, sleep=lambda _s: None)

    fenced.run("reserve_table", gen="g", turn_id=1, is_valid=lambda: False, party_size=4)

    assert _count(hotel, "table_bookings") == before


def test_a_fenced_booking_leaves_the_room_free(hotel) -> None:
    """Not just the reservation row: the room must not be marked reserved either, or the next
    caller is told a free room is taken by a booking that never happened."""
    fenced = ToolRunner(Trace(), hotel, tools=HOTEL_TOOLS, delay_ms=50.0, sleep=lambda _s: None)
    free_before = len(hotel.available_rooms())

    fenced.run("reserve_room", gen="g", turn_id=1, is_valid=lambda: False)

    hotel.record_change()          # drop caches, so this reads the database and not a snapshot
    assert len(hotel.available_rooms()) == free_before


def test_the_interrupted_caller_gets_the_booking_they_actually_asked_for(monkeypatch) -> None:
    """End to end through the real turn loop: interrupt a booking, then ask for a different one."""
    from tests.test_menu_routing import AUDIO, build

    spike, trace, rime, _llm = build(monkeypatch, "book me a deluxe king")

    def caller_interrupts(_seconds):
        spike.barge.on_speech_onset()
        spike.barge.on_voiced_progress(400.0)

    spike.tools = ToolRunner(spike.trace, spike.menu, tools=HOTEL_TOOLS,
                             delay_ms=500.0, sleep=caller_interrupts)
    spike.handle_utterance(AUDIO, 0.0)

    assert trace.all(EventType.RESULT_LEAKED) == []
    assert _count(spike.menu, "reservations") == 3, "the abandoned booking was written anyway"


# --- 2. the facts are still unwritable ------------------------------------------------------------

@pytest.mark.parametrize("statement", [
    "UPDATE menu_items SET price_inr = 1",
    "UPDATE room_types SET base_rate_inr = 0",
    "UPDATE hotel_policies SET fee_inr = 0",
    "UPDATE hotel SET check_in_time = '00:00'",
    "UPDATE rooms SET room_number = '999' WHERE room_id = 1",
    "DELETE FROM menu_items",
    "DELETE FROM room_types",
    "INSERT INTO menu_items (item_id, name) VALUES (99, 'Invented Dish')",
    "DROP TABLE menu_items",
])
def test_sqlite_itself_refuses_every_write_outside_the_booking_tables(hotel, statement) -> None:
    """Refused BY THE DRIVER, mid-statement, whatever the code asks for.

    This is the same kind of guarantee `mode=ro` gave, narrowed rather than abandoned. A tool with
    a bug cannot reprice the menu; nor can a model, which never reaches this class at all.
    """
    with pytest.raises(sqlite3.DatabaseError):
        hotel.bookings._con.execute(statement)


def test_the_one_column_a_booking_may_change_is_writable(hotel) -> None:
    """The other half: a whitelist that refused everything would be a read-only database with extra
    steps. `rooms.status` has to move, or availability stops telling the truth after a booking."""
    hotel.bookings._con.execute("UPDATE rooms SET status = 'available' WHERE room_id = 1")


def test_the_shipped_database_is_not_what_the_tests_write_to() -> None:
    """The suite wrote two reservations into the committed database once. Never again."""
    from aether.hotel.db import SHIPPED_DB_PATH, default_db_path

    assert default_db_path() != SHIPPED_DB_PATH, (
        "tests are pointed at the shipped database; tests/conftest.py should have redirected them"
    )


# --- 3. the hotel does not contradict itself -------------------------------------------------------

def test_a_booked_room_stops_being_free(hotel, runner) -> None:
    """A judge will book a room and immediately ask whether it is free. It had better have changed.

    Rooms are cached on first read, so this fails without the cache drop in `record_change()` --
    and it fails by answering "yes, it is free" about a room booked one turn earlier.
    """
    said = _speak(runner, "reserve_room", room="101")
    assert "one zero one" in said

    status = _speak(runner, "room_status", room="101")
    assert "free" not in status, f"a room booked a moment ago is still free: {status!r}"


def test_a_booked_room_leaves_the_available_count_one_lower(hotel, runner) -> None:
    before = int(_count_sql(hotel, "SELECT COUNT(*) FROM rooms WHERE status = 'available'"))
    _speak(runner, "reserve_room")
    after = int(_count_sql(hotel, "SELECT COUNT(*) FROM rooms WHERE status = 'available'"))
    assert after == before - 1


def test_cancelling_puts_the_room_back(hotel, runner) -> None:
    said = _speak(runner, "reserve_room", room="101")
    reference = _last_reference(hotel, "reservations", "reservation_id")

    cancelled = _speak(runner, "cancel_booking", reference=reference)
    assert "cancel" in cancelled.lower()

    status = _speak(runner, "room_status", room="101")
    assert "free" in status, f"a cancelled room did not come back: {status!r}"


def test_a_room_that_is_not_free_is_refused_rather_than_double_booked(hotel, runner) -> None:
    _speak(runner, "reserve_room", room="101")
    said = _speak(runner, "reserve_room", room="101")
    assert "not free" in said
    assert _count(hotel, "reservations") == 4, "the same room was booked twice"


# --- what the caller hears -------------------------------------------------------------------------

@pytest.mark.parametrize("language", [ENGLISH, HINDI, SPANISH], ids=lambda lang: lang.code)
def test_every_booking_answer_exists_in_every_language(hotel, runner, language) -> None:
    """R8b.6. A hotel that can only take a booking in English is an English-only hotel."""
    from aether.hotel.tools import _renderer_for

    _templates, not_found = _renderer_for(language)
    for said in (_speak(runner, "reserve_room", language=language, room_type="Deluxe King"),
                 _speak(runner, "reserve_table", language=language, party_size=4),
                 _speak(runner, "table_availability", language=language),
                 _speak(runner, "cancel_booking", language=language, reference="9999")):
        assert said and said != not_found, f"{language.code} has no sentence for a booking"


@pytest.mark.parametrize("language", [ENGLISH, HINDI, SPANISH], ids=lambda lang: lang.code)
def test_no_booking_answer_speaks_a_digit(hotel, runner, language) -> None:
    """Rime is handed words. A refusal once said "room 305 is not free" -- built from an exception
    message rather than from the row -- which reaches the voice as a bare numeral and leaves the
    other two languages reading out English."""
    import re

    for said in (_speak(runner, "reserve_room", language=language, room="101"),
                 _speak(runner, "reserve_room", language=language, room="101"),   # now refused
                 _speak(runner, "reserve_table", language=language, party_size=4),
                 _speak(runner, "cancel_booking", language=language, reference="9999")):
        assert not re.search(r"\d", said), f"{language.code}: digit in {said!r}"


def test_a_refusal_says_why_rather_than_asking_the_caller_to_repeat(hotel, runner) -> None:
    """A refusal used to come back with no records, which rendered the generic not-found line --
    so a caller who asked clearly for a taken room was asked to say it again."""
    _speak(runner, "reserve_room", room="101")
    said = _speak(runner, "reserve_room", room="101")
    assert "again" not in said.lower()
    assert "not free" in said


def test_the_restaurant_refuses_a_sitting_outside_its_own_hours(hotel) -> None:
    """The hours come from `hotel_policies`, so this cannot drift from what AETHER says they are."""
    with pytest.raises(BookingRefused) as refused:
        hotel.bookings.reserve_table(party_size=2, sitting="03:00")
    assert refused.value.code == "outside_hours"


def test_a_party_larger_than_the_largest_table_is_refused(hotel) -> None:
    with pytest.raises(BookingRefused) as refused:
        hotel.bookings.reserve_table(party_size=40)
    assert refused.value.code == "party_too_large"


# --- routing ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("said", "tool"), [
    ("book me a deluxe king", "reserve_room"),
    ("i want to book a room", "reserve_room"),
    ("book room three zero five", "reserve_room"),
    ("can i make a booking", "reserve_room"),
    ("can i reserve a table for four", "reserve_table"),
    ("reserve a table for 6", "reserve_table"),
    ("do you have a table free at eight", "table_availability"),
    ("cancel booking one zero zero four", "cancel_booking"),
    ("मुझे एक कमरा बुक करना है", "reserve_room"),
    ("reservar una mesa para cuatro", "reserve_table"),
])
def test_the_caller_reaches_the_booking_they_asked_for(said: str, tool: str) -> None:
    decision = route(said)
    assert decision is not None, f"{said!r} fell through to the model"
    assert decision.tool == tool, f"{said!r} -> {decision.tool}"


@pytest.mark.parametrize(("said", "tool"), [
    # A LOOKUP must never become a WRITE. `book*` as a stem matched "booking", so this exact
    # sentence took a new booking on room 202 -- the worst failure available to a mutating tool.
    ("is there a booking on room two zero two", "reservation_for_room"),
    ("is room two zero two reserved", "reservation_for_room"),
    ("what is your cancellation policy", "hotel_policy"),
    # And a menu question that happens to contain "table".
    ("what type of dishes are available on the table", "menu_overview"),
])
def test_a_question_about_bookings_is_never_answered_by_making_one(said: str, tool: str) -> None:
    decision = route(said)
    assert decision is not None, f"{said!r} fell through"
    assert decision.tool == tool, f"{said!r} -> {decision.tool}"
    assert decision.tool not in BOOKING_TOOLS, f"{said!r} would have written to the database"


def test_a_duration_is_not_read_as_a_party_size() -> None:
    """"book me a deluxe king for two nights" matched the "for two" frame and booked a table for
    two -- the caller asked to sleep somewhere and was offered dinner."""
    decision = route("book me a deluxe king for two nights")
    assert decision is not None and decision.tool == "reserve_room"
    assert decision.params.get("nights") == 2
    assert "party_size" not in decision.params


def test_hindi_counts_survive_the_router() -> None:
    """`\\w` does not match a Devanagari matra, so `\\w+` matched only "द" of "दो" and every Hindi
    count silently failed. The same fact that broke `normalise`, found a third time."""
    decision = route("मुझे दो रात के लिए कमरा बुक करना है")
    assert decision is not None and decision.params.get("nights") == 2


# --- helpers ----------------------------------------------------------------------------------------

def _count(store, table: str) -> int:
    return int(_count_sql(store, f"SELECT COUNT(*) FROM {table}"))


def _count_sql(store, sql: str) -> int:
    con = sqlite3.connect(f"file:{Path(store.path).as_posix()}?mode=ro", uri=True)
    try:
        return int(con.execute(sql).fetchone()[0])
    finally:
        con.close()


def _last_reference(store, table: str, column: str) -> str:
    con = sqlite3.connect(f"file:{Path(store.path).as_posix()}?mode=ro", uri=True)
    try:
        return str(con.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0])
    finally:
        con.close()
