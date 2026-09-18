"""What the hotel knows the moment after it takes a booking.

The manager's complaint, in their words: *"I ask the agent to reserve this table of four for us at
this time, now it should update its memory or database that this thing (room or table) is reserved,
and when someone asks 'when will this room be available' it should answer every question related
to it."*

Three separate claims, and they fail in different ways:

* **The world changed.** A booking moves `rooms.status` and the free-table count, so the very next
  question is answered against the new state. `test_bookings.py` owns the write; this owns what the
  caller is told afterwards.
* **This call remembers.** "What was my reference?" has nothing to look a booking up BY -- a
  telephone caller gave no name and no number -- so the booking is remembered on the session. It
  used to reach the model, and the clarifier offered "taxi booking".
* **The hotel can say when.** "When will room X be available" was answered "room X is already
  reserved": true, and not the question. A caller asking *when* wants a date.
"""

from __future__ import annotations

import numpy as np
import pytest

from aether.hotel import HotelStore
from aether.hotel.router import route

AUDIO = np.zeros(16000, np.int16)
STORE = HotelStore()


def _one_call(monkeypatch, tmp_path):
    """One `Day1Spike` and a `say()` that drives it, on a PRIVATE copy of the hotel.

    One spike, because a booking reference is session state and a fresh spike per turn would be a
    fresh caller per turn. A private copy, because these tests book rooms: the suite shares one
    scratch database, and without this the free-room count drifts under every later test --
    `test_demo_script.py` quotes "forty one rooms free" and started failing in the full run while
    passing alone. `test_bookings.py` isolates itself the same way, for the same reason.
    """
    import shutil

    from aether.hotel.db import HotelStore, default_db_path
    from tests.test_menu_routing import build

    copy = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), copy)

    spike, _trace, rime, llm = build(monkeypatch, "")
    private = HotelStore(path=copy)
    spike.menu = private
    spike.tools.store = private

    def say(text):
        spike.stt.text = text
        before, calls = len(rime.spoken), len(llm.calls)
        spike.handle_utterance(AUDIO, 0.0)
        said = rime.spoken[before:]
        return (said[-1] if said else ""), len(llm.calls) > calls

    return spike, say


# ============================ the routing ============================

@pytest.mark.parametrize(("said", "tool"), [
    ("when will room three zero five be available", "room_free_from"),
    ("when is room three zero five free again", "room_free_from"),
    ("how long is room three zero five booked for", "room_free_from"),
    # "check out" alone is the hotel's general policy; with a room number it is that booking.
    ("when does the guest in three zero five check out", "room_free_from"),
    # And without a room number it stays the general policy, which it always was.
    ("what time is check out", "check_in_out"),
    # "how many rooms" is in the hotel-info table; "how many rooms are FREE" is a different question
    # and was answered "we have five floors and fifty rooms".
    ("how many rooms are free", "room_availability"),
    ("how many rooms do you have", "hotel_info"),
    ("what is my booking reference", "my_booking"),
    ("when is my table booked for", "my_booking"),
    ("what did I book", "my_booking"),
    ("cancel my reservation", "cancel_my_booking"),
])
def test_the_question_reaches_the_tool_that_can_answer_it(said, tool):
    decision = route(said)
    assert decision is not None, f"{said!r} reached the model"
    assert decision.tool == tool, said


def test_asking_when_a_room_frees_up_never_books_it():
    """A lookup silently becoming a write, and it really happened -- in Hindi.

    "कमरा एक शून्य दो कब तक बुक है" means *until when is room one zero two booked*. It contains
    बुक, which is in `_BOOK_VERBS`, so it routed to `reserve_room`. That room was occupied so the
    booking was refused; on a free room AETHER would have booked it for a caller who asked nothing
    of the sort. `_BOOK_VERBS` is exact-form English to avoid exactly this, and Hindi has no
    separate word for the noun to key off, so a question word plus a room number wins instead.
    """
    for said in ("कमरा एक शून्य दो कब तक बुक है",
                 "कमरा दो शून्य एक कब खाली होगा",
                 "hasta cuando esta reservada la habitacion uno cero dos",
                 "when is room two zero one booked until"):
        decision = route(said)
        assert decision is not None, f"{said!r} reached the model"
        assert decision.tool != "reserve_room", (
            f"{said!r} is a question and it took a booking"
        )


# ============================ the whole call ============================

def test_a_table_booking_changes_the_answer_to_the_next_question(monkeypatch, tmp_path):
    from aether.hotel import say_number

    spike, say = _one_call(monkeypatch, tmp_path)
    # Read from THIS call's store, before anything is booked. The module-level `STORE` shares the
    # same scratch database, so asking it afterwards returns the post-booking number and the test
    # would be comparing the answer against itself.
    free_before = spike.menu.bookings.tables_free(sitting="20:00")

    before, _ = say("do you have a table free at eight")
    booked, used_model = say("book a table for four at eight")
    after, _ = say("do you have a table free at eight")

    assert not used_model
    assert "Done" in booked
    assert before != after, "the hotel said the same thing before and after a booking"
    assert spike.menu.bookings.tables_free(sitting="20:00") == free_before - 1
    # `f"{word} tables"`, not the bare number word: the sentence also says "at eight in the
    # evening", so looking for "eight" anywhere in it finds the TIME. The first version of this
    # test did exactly that and read the count as eight both times.
    assert f"{say_number(free_before)} tables" in before
    assert f"{say_number(free_before - 1)} tables" in after, (
        f"the table count did not move: {after!r}"
    )


def test_the_caller_can_ask_what_they_just_booked(monkeypatch, tmp_path):
    _spike, say = _one_call(monkeypatch, tmp_path)

    booked, _ = say("book a table for four at eight")
    reference = booked.rsplit("is ", 1)[-1].rstrip(".")

    recalled, used_model = say("what is my booking reference")
    assert not used_model, "the model cannot know a reference this call just created"
    assert reference in recalled, f"{recalled!r} does not carry {reference!r}"
    assert "four" in recalled and "eight" in recalled, "the party and the sitting were forgotten"


def test_before_booking_anything_the_answer_is_honest_not_invented(monkeypatch, tmp_path):
    _spike, say = _one_call(monkeypatch, tmp_path)
    said, used_model = say("what is my booking reference")
    assert not used_model
    assert "not made a booking" in said.lower()


def test_a_caller_can_cancel_the_booking_they_just_made_without_a_reference(
        monkeypatch, tmp_path):
    """A reference is normally required -- cancelling the wrong booking is not recoverable. "My"
    is the one safe exception: it is this call's booking, and there is exactly one of it."""
    _spike, say = _one_call(monkeypatch, tmp_path)

    before, _ = say("do you have a table free at eight")
    say("book a table for four at eight")
    cancelled, used_model = say("cancel my reservation")
    after, _ = say("do you have a table free at eight")

    assert not used_model
    assert "cancelled" in cancelled.lower()
    assert after == before, "the table did not come back"


def test_cancelling_with_no_booking_asks_for_a_reference_rather_than_guessing(
        monkeypatch, tmp_path):
    _spike, say = _one_call(monkeypatch, tmp_path)
    said, _ = say("cancel my reservation")
    assert "reference" in said.lower()


def test_a_room_booking_says_when_the_room_frees_up_afterwards(monkeypatch, tmp_path):
    """The manager's question end to end: book it, then ask when it is free again."""
    from aether.hotel import say_date

    spike, say = _one_call(monkeypatch, tmp_path)

    booked, _ = say("book a deluxe king for two nights")
    assert "Done" in booked
    reservation = spike.menu.bookings.last_booking
    number = reservation.room_number
    spoken_number = " ".join({"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
                              "5": "five", "6": "six", "7": "seven", "8": "eight",
                              "9": "nine"}[c] for c in number)

    free_now, _ = say(f"is room {spoken_number} free")
    assert "free" not in free_now.replace("is not free", ""), free_now

    when, used_model = say(f"when will room {spoken_number} be available")
    assert not used_model
    assert say_date(reservation.check_out) in when, (
        f"{when!r} does not say when the room frees up ({reservation.check_out})"
    )


# A room is unavailable for one of two quite different reasons, and the difference is the whole
# point of this pair of tests. `occupied` and `reserved` mean a GUEST -- somebody booked it and
# leaves on a date. `housekeeping` and `maintenance` mean the room is out of service, nobody has
# booked it, and there is no date to give.
_HAS_A_GUEST = ("occupied", "reserved")


def test_every_room_with_a_guest_in_it_can_say_when_it_frees_up():
    """The data claim behind the answer above.

    Five rooms held a guest and only three had a reservation, so two answered "I do not have a date
    for when it frees up" -- a hotel that cannot say when its own rooms free up.
    `scripts/fix_occupied_rooms.py` gave each the booking that must exist behind it.
    """
    from aether.hotel.db import UnknownRecord

    taken = [r for r in STORE.rooms() if r.status in _HAS_A_GUEST]
    assert taken, "the fixture hotel has no rooms with a guest in them"
    for room in taken:
        try:
            STORE.reservation_for_room(room.number)
        except UnknownRecord:
            pytest.fail(
                f"room {room.number} is '{room.status}' with no booking behind it, so AETHER "
                f"cannot say when it frees up -- run scripts/fix_occupied_rooms.py"
            )


def test_a_room_out_of_service_has_no_guest_and_says_why():
    """The other half, and it is a correction rather than a new rule.

    The first version of `fix_occupied_rooms.py` gave a reservation to every room that was not
    available -- including the four being cleaned or repaired. That put an invented guest in a room
    nobody had booked, and "when will room five zero eight be available?" answered "booked until the
    twenty-fifth of September" about a room that is out for maintenance. Wrong, and confidently so.
    """
    from aether.hotel.db import UnknownRecord
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.tools import ToolRunner
    from aether.trace import Trace

    out_of_service = [r for r in STORE.rooms()
                      if r.status not in _HAS_A_GUEST and r.status != "available"]
    assert out_of_service, "the fixture hotel has no rooms out of service"

    runner = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS)
    for room in out_of_service:
        with pytest.raises(UnknownRecord):
            STORE.reservation_for_room(room.number)
            pytest.fail(f"room {room.number} is '{room.status}' and has a guest booked into it")

        said = render(runner.run("room_free_from", gen="g", turn_id=1, is_valid=lambda: True,
                                 room=room.number))
        assert "booked until" not in said, f"invented a checkout date: {said!r}"
        # The raw column value interpolated into the sentence, which is what the first version did:
        # "Room five zero eight is maintenance at the moment". Not the word itself -- "with
        # housekeeping just now" is the correct phrasing and contains it -- but that exact shape.
        assert f"is {room.status} at the moment" not in said, (
            f"the raw column value reached the caller: {said!r}"
        )
        assert "do not have a date" in said, said
        # And it says WHY, which is the useful half: a room being repaired and a room whose guest
        # has not left are both "not available" and are not the same answer.
        assert any(why in said for why in ("maintenance", "housekeeping")), (
            f"{said!r} does not say why room {room.number} is unavailable"
        )


def test_a_room_with_no_booking_behind_it_says_so_rather_than_inventing_a_date(monkeypatch):
    """The honest fallback still has to exist and still has to be right.

    A checkout date the hotel has no record of, spoken to someone planning around it, is worse
    than an admission -- so this stages the state the data fix removed rather than relying on it.
    """
    from aether.hotel.tools import HOTEL_TOOLS
    from aether.hotel.db import UnknownRecord

    fn, _mutating = HOTEL_TOOLS["room_free_from"]
    taken = next(r for r in STORE.rooms() if r.status != "available")

    class _NoReservations:
        def __getattr__(self, name):
            return getattr(STORE, name)

        def reservation_for_room(self, number):
            raise UnknownRecord(f"no reservation for room {number}")

    _records, summary = fn(_NoReservations(), room=taken.number)
    assert summary["free_now"] is False
    assert summary["until"] is None, "a date was invented for a room with no booking"


def test_a_booking_the_caller_abandoned_is_not_remembered_either(monkeypatch, tmp_path):
    """The golden invariant, extended to session memory.

    A fenced booking writes no row -- `test_bookings.py` owns that. This owns the other half: it
    must not be REMEMBERED either, or the next turn would tell the caller they have a table that
    was never booked. Worse than forgetting, because the caller would stop worrying about it.
    """
    from aether.events import EventType
    from aether.hotel.tools import HOTEL_TOOLS
    from aether.tools import ToolRunner

    spike, say = _one_call(monkeypatch, tmp_path)

    def caller_interrupts(_seconds):
        spike.barge.on_speech_onset()
        spike.barge.on_voiced_progress(400.0)

    spike.tools = ToolRunner(spike.trace, spike.menu, tools=HOTEL_TOOLS,
                             delay_ms=500.0, sleep=caller_interrupts)
    spoken, _ = say("book a table for four at eight")
    assert spoken == "", "a fenced booking was spoken"
    assert spike.menu.bookings.last_booking is None, "a fenced booking was remembered"
    assert spike.trace.all(EventType.RESULT_LEAKED) == []

    spike.tools = ToolRunner(spike.trace, spike.menu, tools=HOTEL_TOOLS)
    said, _ = say("what is my booking reference")
    assert "not made a booking" in said.lower(), (
        f"the caller was told about a booking that never happened: {said!r}"
    )


# ============================ a reference the caller gives ============================
#
# REPORTED FROM A REAL CALL. A room was booked on one call and read back as reference 1009. On the
# NEXT call, "can you check my room booking status with the reference ID 1009" was answered "you
# have not made a booking on this call yet" -- while "is room one zero three free" correctly
# answered that it was reserved. The hotel knew the booking and denied it.
#
# `my_booking` answers about THIS call, which is right for a caller who has just booked and has
# nothing else to be looked up by. A caller who GIVES the number is asking a different question.

def test_a_reference_the_caller_gives_is_looked_up_not_denied():
    for said in ("can you check my room booking status with the reference ID 1009",
                 "what is the status of booking one zero zero nine",
                 "is booking 1009 confirmed",
                 "check booking one zero zero nine"):
        decision = route(said)
        assert decision is not None, f"{said!r} reached the model"
        assert decision.tool == "booking_status", f"{said!r} went to {decision.tool}"
        assert decision.params["reference"] == "1009", decision.params


def test_a_four_digit_reference_is_not_read_as_a_three_digit_room():
    """"Booking one zero zero nine" contains "one zero zero", and the room finder took it -- so a
    question about booking 1009 was answered about ROOM 100: a different room, a different guest."""
    from aether.hotel.router import _find_room_number, normalise

    assert _find_room_number(normalise("booking one zero zero nine")) is None
    assert _find_room_number(normalise("room one zero three")) == "103"


def test_naming_a_room_is_still_about_the_room():
    """The guard on the rule above: three digits after the word "room" are a room, not a
    reference."""
    decision = route("is there a booking on room three zero five")
    assert decision.tool == "reservation_for_room"
    assert decision.params["room"] == "305"


def test_a_booking_made_on_an_earlier_call_can_be_read_back(tmp_path):
    """End to end: book on one session, look it up by reference on a completely separate one."""
    import shutil

    from aether.hotel.bookings import Bookings
    from aether.hotel.db import default_db_path
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.tools import ToolRunner
    from aether.trace import Trace

    copy = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), copy)

    first_call = Bookings(copy)
    try:
        booked = first_call.reserve_room(room_type="Deluxe King", nights=2)
    finally:
        first_call.close()

    later = HotelStore.__class__ and __import__("aether.hotel", fromlist=["HotelStore"])
    store = later.HotelStore(path=copy)
    try:
        assert store.bookings.last_booking is None, "a new call must remember nothing"
        result = ToolRunner(Trace(), store, tools=HOTEL_TOOLS).run(
            "booking_status", gen="g", turn_id=1, is_valid=lambda: True,
            reference=booked.reference)
        said = render(result)
        assert "no booking" not in said.lower(), said
        assert booked.room_number.lstrip("0")[0] in said or "room" in said.lower()
        assert "confirmed" in said or "checked in" in said, said
    finally:
        store.bookings.close()


def test_an_unknown_reference_says_so_rather_than_guessing():
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.tools import ToolRunner
    from aether.trace import Trace

    result = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS).run(
        "booking_status", gen="g", turn_id=1, is_valid=lambda: True, reference="8888")
    said = render(result)
    assert "no booking" in said.lower() and "eight eight eight eight" in said, said


# ============================ asking who ============================
#
# REPORTED: "there were some reservation questions it was not answering when we gave room number
# and reference id ... it should at least respond, like sorry I can't give personal info."
#
# They were being answered -- as something else. "Who is staying in room two zero one" went to
# `room_status` and came back "room two zero one is already reserved": true, about availability,
# and not the question. A caller who asks who is in a room and hears about the room has been
# answered by something that was not listening.
#
# The withholding itself was never in doubt -- `_reservation_for_room` has always refused to speak
# a guest's name, because a hotel line answers to whoever dials it. What was missing was saying so.

@pytest.mark.parametrize("said", [
    "who is staying in room two zero one",
    "what is the name of the guest in room two zero one",
    "can you give me the guest details for room two zero one",
    "what is the phone number of the guest in room two zero one",
    "who booked room two zero one",
    "whose booking is one zero zero eight",
    "tell me the guest name for booking one zero zero eight",
])
def test_asking_who_is_declined_rather_than_answered_as_something_else(said):
    decision = route(said)
    assert decision is not None, f"{said!r} reached the model"
    assert decision.tool == "guest_privacy", (
        f"{said!r} asks about a person and went to {decision.tool}"
    )


@pytest.mark.parametrize("said", [
    "what is your phone number",          # the HOTEL's number, which is public
    "who do I call for room service",     # an extension, not a guest
    "where are you located",
    "is room two zero one free",
    "when will room two zero one be available",
    "is there a booking on room two zero one",
    "what is my booking reference",
    "what is the status of booking one zero zero eight",
])
def test_ordinary_questions_are_not_refused_as_personal(said):
    """The expensive direction. A hotel that will not say whether a room is free is useless, and
    the hotel's own telephone number is not a guest's."""
    decision = route(said)
    assert decision is not None, f"{said!r} reached the model"
    assert decision.tool != "guest_privacy", f"{said!r} was wrongly refused"


def test_the_refusal_says_what_it_can_do_instead(tmp_path):
    """A bare refusal on a telephone sounds like a fault. It has to hand the call back."""
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.tools import ToolRunner
    from aether.trace import Trace

    said = render(ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS).run(
        "guest_privacy", gen="g", turn_id=1, is_valid=lambda: True))
    assert "cannot give out" in said, said
    assert "free" in said, f"the refusal offers nothing in return: {said}"


def test_the_privacy_answer_reads_no_guest_record_at_all():
    """Structural. The answer does not depend on the booking, so it must not look at one -- a tool
    that reads the record to decide it cannot speak it is one edit away from speaking it."""
    import inspect

    from aether.hotel.tools import HOTEL_TOOLS

    fn, mutating = HOTEL_TOOLS["guest_privacy"]
    assert mutating is False
    body = inspect.getsource(fn)
    for reach in ("store.bookings", "reservation", "guest_name", "store.rooms", ".find("):
        assert reach not in body.split('"""')[-1], (
            f"guest_privacy consults {reach!r} -- it has no reason to"
        )


@pytest.mark.parametrize("code", ["eng", "hin", "spa"])
def test_the_refusal_exists_in_every_language(code):
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.lang import by_code
    from aether.tools import ToolRunner
    from aether.trace import Trace

    said = render(ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS).run(
        "guest_privacy", gen="g", turn_id=1, is_valid=lambda: True), by_code(code))
    assert said and len(said.split()) > 6, f"[{code}] {said!r}"


def test_a_reference_on_its_own_is_enough_to_look_a_booking_up():
    """"Reference one zero zero eight" reached the model, and "room two zero one, reference one
    zero zero eight" was answered as though only the room had been said -- because `reference` was
    not one of the words that makes a sentence about a booking."""
    assert route("reference one zero zero eight").tool == "booking_status"
    assert route("booking number one zero zero eight").tool == "booking_status"
    assert route("my confirmation is one zero zero eight").tool == "booking_status"


def test_a_room_with_no_booking_says_so_rather_than_asking_you_to_repeat(tmp_path):
    """Seen on a real call: "can you give me details on a room I booked on another call, 103?"
    came back "I am sorry, I could not find that. Could you say it again?"

    That is the sentence for a request that was MISHEARD. The caller was heard perfectly -- room
    103 simply had nobody booked into it (the working copy had been reset since). Being asked to
    repeat a clear question suggests the fault was theirs.
    """
    import shutil

    from aether.hotel.db import default_db_path
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.tools import ToolRunner
    from aether.trace import Trace

    copy = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), copy)
    store = HotelStore(path=copy)
    free = next(r for r in store.rooms() if r.status == "available")

    said = render(ToolRunner(Trace(), store, tools=HOTEL_TOOLS).run(
        "reservation_for_room", gen="g", turn_id=1, is_valid=lambda: True, room=free.number))

    assert "could not find that" not in said, said
    assert "no booking" in said.lower(), said
    assert "free" in said, f"it says there is no booking but not that the room is free: {said}"


def test_a_room_the_hotel_does_not_have_is_still_told_apart(tmp_path):
    """The guard on the answer above: "no booking on room nine nine nine" would be a confident
    statement about a room that does not exist."""
    import shutil

    from aether.hotel.db import default_db_path
    from aether.hotel.tools import HOTEL_TOOLS, render
    from aether.tools import ToolRunner
    from aether.trace import Trace

    copy = tmp_path / "hotel.db"
    shutil.copy(default_db_path(), copy)
    said = render(ToolRunner(Trace(), HotelStore(path=copy), tools=HOTEL_TOOLS).run(
        "reservation_for_room", gen="g", turn_id=1, is_valid=lambda: True, room="999"))
    assert "no booking" not in said.lower(), f"invented a room: {said}"
