"""A booking must never invent the thing the caller did not say.

THE DEFECT, from a live call on 2026-09-19. The caller said *"I need a reservation for a deluxe
room"*. The router did not resolve the bare word "deluxe" -- its table held only the catalogue name
"Deluxe King" -- so `reserve_room` was called with no room type at all. `Bookings.reserve_room` then
did what it was documented to do and took the lowest-numbered free room in the hotel, and AETHER
said, with no hedge:

    "Done. I have reserved the Standard King, room one zero one, for one night at six thousand
     five hundred rupees a night. Your reference is one zero zero eight."

A dropped word became a confident wrong booking two thousand rupees below the room that was asked
for. That is worse than any refusal, because the caller has no way to detect it -- the sentence is
fluent, specific, and wrong.

Two independent fixes, and this file holds both to account:

* the router now resolves the distinguishing word a caller actually says, and
* a booking with no room type named ASKS instead of choosing.

The second is the one that matters. The first reduces how often it is needed; only the second makes
the failure safe.
"""

from __future__ import annotations

import pytest

from aether.hotel.bookings import BookingRefused
from aether.hotel.db import HotelStore
from aether.hotel.router import route
from aether.hotel.tools import HOTEL_TOOLS


@pytest.fixture()
def store(tmp_path):
    """A private copy of the hotel. Two tests below make real bookings, and the session-wide copy
    in conftest is shared across files -- booking room 101 there broke four tests in
    test_bookings.py that assume 101 is free. Same pattern as that file's own `hotel` fixture."""
    import shutil

    from aether.hotel.db import default_db_path

    copy = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), copy)
    hotel = HotelStore(path=copy)
    yield hotel
    hotel.bookings.close()


# --- the safety property --------------------------------------------------------------------------

def test_a_booking_with_no_room_type_asks_rather_than_choosing(store):
    """The property that makes a dropped word harmless."""
    fn, mutating = HOTEL_TOOLS["reserve_room"]
    assert mutating, "reserve_room must stay declared mutating, or the fence stops covering it"

    rows, summary = fn(store)
    assert summary["booked"] is False, "an unnamed room type was booked instead of asked about"
    assert summary["why"] == "room_type_not_given"
    assert rows == []


def test_the_refusal_names_every_room_type_so_the_caller_can_choose():
    """A question the caller cannot answer is not a question. On a phone there is no list to read,
    so the refusal has to carry the options itself."""
    import types

    import aether.hotel.tools as en
    import aether.hotel.tools_es as es
    import aether.hotel.tools_hi as hi

    result = types.SimpleNamespace(
        tool="reserve_room", rows=[], may_speak=True,
        summary={"booked": False, "why": "room_type_not_given"})

    for label, module in (("English", en), ("Hindi", hi), ("Spanish", es)):
        spoken = module.SPEAK["reserve_room"](result)
        assert spoken, f"{label} has no line for room_type_not_given"
        for room_type in ("Standard King", "Deluxe King", "Executive Suite", "Family Suite"):
            assert room_type in spoken, f"{label} refusal does not offer the {room_type}"


def test_the_store_refuses_before_it_touches_the_database(store):
    """Raised before `BEGIN IMMEDIATE`, so an unnamed booking never takes the write lock."""
    with pytest.raises(BookingRefused) as raised:
        store.bookings.reserve_room()
    assert raised.value.code == "room_type_not_given"


def test_naming_a_specific_room_still_works_without_a_type(store):
    """`room=` alone is enough -- the guard is about having NEITHER, not about requiring a type."""
    free = next(r for r in store.rooms() if r.status == "available")
    booking = store.bookings.reserve_room(room=str(free.number))
    assert booking.room_number == str(free.number)


# --- the routing that made it likely ----------------------------------------------------------------

@pytest.mark.parametrize(("said", "expected"), [
    ("I need a reservation for a deluxe room", "Deluxe King"),
    ("book a deluxe room", "Deluxe King"),
    ("can I have a deluxe room for two nights", "Deluxe King"),
    ("reserve an executive suite", "Executive Suite"),
    ("book me an executive room", "Executive Suite"),
    ("I want a family room", "Family Suite"),
])
def test_the_distinguishing_word_is_enough(said, expected):
    """Callers say "a deluxe room", not "a Deluxe King". Each of these words belongs to exactly one
    room type, so the shorthand is unambiguous."""
    decision = route(said)
    assert decision is not None, f"{said!r} routed nowhere"
    assert decision.params.get("room_type") == expected, (
        f"{said!r} resolved to {decision.params.get('room_type')!r}"
    )


def test_standard_is_deliberately_not_a_shortcut():
    """There are two Standards -- King and Twin -- so "a standard room" must NOT silently pick one.

    This is the same failure as the original defect, and adding "standard" to the shorthand table
    to make a sentence route would reintroduce it in a new place.
    """
    decision = route("book a standard room")
    resolved = decision.params.get("room_type") if decision else None
    assert resolved is None, (
        f'"a standard room" resolved to {resolved!r}; there are two Standards and it must be asked '
        f"about rather than guessed"
    )


def test_a_deluxe_request_end_to_end_books_a_deluxe(store):
    """The whole path: the sentence a caller said, through the router, into the booking."""
    decision = route("I need a reservation for a deluxe room")
    fn, _mutating = HOTEL_TOOLS[decision.tool]
    _rows, summary = fn(store, **decision.params)
    assert summary["booked"] is True
    assert summary["room_type"] == "Deluxe King", (
        f"booked a {summary['room_type']!r} for a caller who asked for a deluxe room"
    )


# --- the other dropped word ---------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "I need another vegetable starter",
    "No, can you stop that? I need another vegetable starter.",
    "do you have any vegetable dishes",
])
def test_vegetable_is_understood_as_vegetarian(said):
    """A caller asked for "another vegetable starter" and was answered "did you mean starters?" --
    having just said the word starter. "vegetable" is what people say and what Whisper returns; the
    diet table only held "vegetarian", "veggie" and "veg"."""
    decision = route(said)
    assert decision is not None, f"{said!r} routed nowhere"
    assert decision.params.get("diet") == "vegetarian"


def test_vegetable_does_not_shadow_vegetarian():
    """The table is matched longest-first, so the shorter word cannot capture the longer one."""
    decision = route("what vegetarian starters do you have")
    assert decision.params.get("diet") == "vegetarian"
    assert decision.params.get("category") == "starters"
