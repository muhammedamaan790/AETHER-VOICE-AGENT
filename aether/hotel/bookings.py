"""The only thing in AETHER that may change the hotel, and the fence around it.

UNTIL NOW NOTHING COULD WRITE. `HotelStore` opens `data/aether_hotel.db` with `mode=ro`, so a write
was refused by SQLite rather than by convention -- and the documentation said so, loudly, because a
read-only agent cannot ruin anyone's day. Taking a booking means giving that up, and the honest
thing is to give up as little as possible rather than to open the file and rely on care.

WHAT IS STILL UNWRITABLE, and enforced the same way the old guarantee was. This connection installs
a `sqlite3` authorizer that DENIES any write outside six places:

    reservations              a room booking
    table_bookings            a restaurant booking
    guests                    the caller, so a booking has someone to belong to
    restaurant_orders         an order being taken, and then sent to the kitchen
    restaurant_order_items    the dishes on it, at the price they were ordered at
    rooms.status              the one column a booking changes, so "is room three zero five free"
                              keeps telling the truth afterwards

It was four until 2026-09-18, when AETHER learned to take an order. Widening a guarantee this
project states out loud is worth stating: two tables were NAMED rather than the authorizer relaxed,
and the facts protected are exactly the facts protected before.

Every price, every allergen, every policy, every room rate, every menu item is refused **by the
driver**, mid-statement, whatever the code asks for. A tool with a bug cannot reprice the menu; nor
can a model, which never reaches this class at all. That is the same kind of guarantee `mode=ro`
gave, narrowed rather than abandoned, and `tests/test_bookings.py` proves it by trying.

WHY WRITE `rooms.status` AT ALL. Because the alternative is worse. A booking that leaves the room
table untouched means "reserve room three zero five" is followed by "is room three zero five free?"
answering *yes* -- and a judge will try exactly that. Availability is derived from `rooms.status`
everywhere in this codebase, so a booking has to move it or the hotel starts contradicting itself
one turn after a booking.

FENCING. Booking tools are declared mutating in `HOTEL_TOOLS`, and `ToolRunner` checks validity
*after* the delay and *before* the tool body -- so a caller who changes their mind while the booking
is in flight leaves no row behind. That is not a new mechanism; it is the one the warehouse tools
have always used, and it is the strongest form of the project's central claim: not merely that a
stale answer is never spoken, but that a stale intention never lands.

STILL THE ONLY CONNECTION THAT MAY CHANGE THE HOTEL. `learned.py` also writes, but to a database of
its own -- deliberately, so that what the model made up about the hotel is not in the same file as
what the hotel actually charges.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .db import UnknownRecord, default_db_path

# Exactly what may be written, and nothing else. Tables here are writable in full; `rooms` is
# writable only in the `status` column, which the authorizer checks separately.
#
# `restaurant_orders` and `restaurant_order_items` were added on 2026-09-18, when AETHER learned to
# take an order. That is a deliberate widening of a guarantee this project states out loud, so it is
# stated here too: taking an order means writing an order, and the honest way to do that is to name
# the two tables rather than to relax the authorizer. Every price, allergen, policy, rate and room
# number is refused exactly as before -- `unit_price_inr` is COPIED from `menu_items` at the moment
# of ordering and the menu row itself is never touched.
_WRITABLE_TABLES = frozenset({"reservations", "table_bookings", "guests",
                              "restaurant_orders", "restaurant_order_items"})
_WRITABLE_COLUMNS = frozenset({("rooms", "status")})

# A caller on the telephone has not given a name. Inventing one would put a fabricated person in a
# real table; using a single shared row would make two callers the same guest. A new row per
# booking, named for what it is, is the honest option -- and nothing ever speaks a guest name
# (`tests/test_language.py` enforces that), so it is never heard.
_TELEPHONE_GUEST = "Telephone booking"

# A caller cannot order thirty of one dish over the telephone by accident, and if they mean to, a
# human should hear about it. Same shape as `_MAX_PARTY` on table bookings: a refusal that names the
# limit, rather than a silent acceptance nobody can fulfil.
_MAX_PER_DISH = 20

# THE KITCHEN'S OWN VOCABULARY, not a new one. `restaurant_orders.status` is a CHECK constraint over
# exactly these four, so an order being built is `pending` -- not yet with anyone -- and becomes
# `preparing` when the caller says to send it. Inventing a fifth word ("open") was refused by SQLite
# on the first order ever taken, which is the schema doing its job.
_ORDER_OPEN = "pending"
_ORDER_PLACED = "preparing"
_ORDER_CANCELLED = "cancelled"


class BookingRefused(RuntimeError):
    """A booking that cannot be made, for a reason the caller should be told.

    Carries a CODE and structured detail, never a sentence. An early version raised
    `f"room {number} is not free"` and the renderer spoke it verbatim -- putting a bare "305" into
    Rime, which says it as a quantity, and leaving Hindi and Spanish with an English sentence to
    read out. Same rule as `hotel_policies`: the store holds facts, the renderers hold language.
    """

    def __init__(self, code: str, **detail):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RoomBooking:
    reference: str
    room_number: str
    room_type: str
    rate: float
    nights: int
    check_in: str
    check_out: str


@dataclass(frozen=True)
class TableBooking:
    reference: str
    party_size: int
    sitting: str
    booked_for: str


@dataclass(frozen=True)
class OrderLine:
    """One dish on an order, at the price it was ordered at."""

    name: str
    quantity: int
    unit_price: float

    @property
    def line_total(self) -> float:
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class Order:
    """An order as it stands: what is on it, and what it comes to.

    `placed` is what separates "reading the order back" from "it is with the kitchen". A caller can
    keep adding until they say so, and `repeat_order` is answerable at any point in between -- which
    is the whole reason this exists rather than a list held in the model's context.
    """

    reference: str
    lines: tuple[OrderLine, ...]
    placed: bool

    @property
    def total(self) -> float:
        return sum(line.line_total for line in self.lines)

    @property
    def items(self) -> int:
        return sum(line.quantity for line in self.lines)


def _authorizer(action: int, arg1: str | None, arg2: str | None,
                _db: str | None, _trigger: str | None) -> int:
    """Refuse every write outside the booking tables, at the driver.

    `arg1` is the table for the write actions and `arg2` the column where one applies. Reads are
    allowed everywhere -- a booking has to look at rates and room types to be made at all.
    """
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_OK if arg1 in _WRITABLE_TABLES else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_UPDATE:
        if arg1 in _WRITABLE_TABLES:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if (arg1, arg2) in _WRITABLE_COLUMNS else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_DROP_TABLE or action == sqlite3.SQLITE_ALTER_TABLE:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class Bookings:
    """Takes bookings and orders. Holds the only connection that may change the hotel."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_db_path()
        # WHAT THIS CALL HAS DONE. Both are per-call, because every call builds its own store and so
        # its own `Bookings` -- two callers cannot see each other's order or reference.
        #
        # They are the difference between an agent that takes a booking and one a caller can then
        # TALK to about it. "What was my reference?" and "repeat my order" are the questions people
        # actually ask next, and without this they reached the model, which knows neither.
        self._order_id: int | None = None
        self.last_booking: RoomBooking | TableBooking | None = None
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self.path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")
        # Installed AFTER the pragma: the authorizer would otherwise have to reason about pragmas
        # too, and this is the only one used.
        self._con.set_authorizer(_authorizer)

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass

    # --- rooms -----------------------------------------------------------------------------

    def reserve_room(self, *, room_type: str | None = None, room: str | None = None,
                     nights: int = 1, today: date | None = None) -> RoomBooking:
        """Hold a room. Names the room it actually booked, never "a room".

        `room` names one specific room; `room_type` takes the lowest-numbered free room of that
        type; neither takes the lowest-numbered free room in the hotel. A room that is not free is
        refused rather than double-booked -- SQLite has no opinion about that, so this does.
        """
        if nights < 1:
            raise BookingRefused("min_one_night")
        # NEITHER NAMED: ASK, DO NOT CHOOSE. This used to fall through to the lowest-numbered free
        # room in the hotel, and on 2026-09-19 a caller who asked for "a deluxe room" -- a phrasing
        # the router did not yet resolve -- was told "Done. I have reserved the Standard King" at a
        # rate two thousand rupees below the room they asked for. A dropped word became a confident
        # wrong booking, which is worse than any refusal: the caller has no way to know.
        #
        # A booking is the one thing here that cannot be taken back by saying something else, so an
        # unnamed room type is a question rather than a default.
        if room is None and room_type is None:
            raise BookingRefused("room_type_not_given")
        start = today or datetime.now(timezone.utc).date()

        with self._lock:
            # ONE TRANSACTION, TAKEN WITH THE WRITE LOCK. `self._lock` serialises this connection
            # only -- and every call builds its own store, so two callers are two connections. Both
            # used to read the last Deluxe King as free and both be told it was theirs.
            # `BEGIN IMMEDIATE` takes SQLite's database-wide write lock BEFORE the room is chosen,
            # so simultaneous bookings are served one after the other.
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._pick_room(room=room, room_type=room_type)
                # And the claim is conditional, belt and braces: it only succeeds if the room is
                # still free at the instant of writing, whatever was read a moment earlier.
                claimed = self._con.execute(
                    "UPDATE rooms SET status = 'reserved'"
                    " WHERE room_id = ? AND status = 'available'",
                    (row["room_id"],)).rowcount
                if claimed != 1:
                    raise BookingRefused("room_not_free", room=str(row["room_number"]))
                check_in = start.isoformat()
                check_out = (start + timedelta(days=nights)).isoformat()
                guest_id = self._new_guest()
                cur = self._con.execute(
                    "INSERT INTO reservations (guest_id, room_id, check_in, check_out, adults,"
                    " children, status, special_requests, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (guest_id, row["room_id"], check_in, check_out, 1, 0, "confirmed", None,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                self._con.commit()
            except BaseException:
                self._con.rollback()
                raise
            made = RoomBooking(
                reference=str(cur.lastrowid),
                room_number=str(row["room_number"]),
                room_type=str(row["type_name"]),
                rate=float(row["base_rate"]),
                nights=nights,
                check_in=check_in,
                check_out=check_out,
            )
            self.last_booking = made
            return made

    def _pick_room(self, *, room: str | None, room_type: str | None) -> sqlite3.Row:
        sql = ("SELECT r.room_id, r.room_number, r.status, t.name AS type_name, t.base_rate_inr AS base_rate"
               " FROM rooms r JOIN room_types t ON t.room_type_id = r.room_type_id")
        if room:
            wanted = "".join(ch for ch in str(room) if ch.isdigit())
            found = self._con.execute(sql + " WHERE r.room_number = ?", (wanted,)).fetchone()
            if found is None:
                raise UnknownRecord(f"no such room: {room}")
            if found["status"] != "available":
                raise BookingRefused("room_not_free", room=str(found["room_number"]))
            return found

        if room_type:
            found = self._con.execute(
                sql + " WHERE r.status = 'available' AND lower(t.name) = lower(?)"
                      " ORDER BY r.room_number LIMIT 1", (room_type,)).fetchone()
            if found is None:
                raise BookingRefused("none_of_that_type_free", room_type=str(room_type))
            return found

        found = self._con.execute(
            sql + " WHERE r.status = 'available' ORDER BY t.base_rate_inr, r.room_number LIMIT 1"
        ).fetchone()
        if found is None:
            raise BookingRefused("hotel_full")
        return found

    # --- tables ----------------------------------------------------------------------------

    def reserve_table(self, *, party_size: int, sitting: str = "20:00",
                      today: date | None = None) -> TableBooking:
        """Hold a restaurant table.

        The restaurant's hours are a row in `hotel_policies`, so a sitting outside them is refused
        with the real hours rather than accepted and quietly disappointed.
        """
        if party_size < 1:
            raise BookingRefused("party_too_small")
        if party_size > _MAX_PARTY:
            raise BookingRefused("party_too_large", most=_MAX_PARTY)

        opens, closes = self._restaurant_hours()
        if not (opens <= sitting <= closes):
            raise BookingRefused("outside_hours", opens=opens, closes=closes)

        booked_for = (today or datetime.now(timezone.utc).date()).isoformat()
        with self._lock:
            guest_id = self._new_guest()
            cur = self._con.execute(
                "INSERT INTO table_bookings (guest_id, party_size, sitting, booked_for, status,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (guest_id, party_size, sitting, booked_for, "confirmed",
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self._con.commit()
            made = TableBooking(reference=str(cur.lastrowid), party_size=party_size,
                                sitting=sitting, booked_for=booked_for)
            self.last_booking = made
            return made

    def _restaurant_hours(self) -> tuple[str, str]:
        row = self._con.execute(
            "SELECT hours FROM hotel_policies WHERE topic = 'restaurant'").fetchone()
        if row is None or not row["hours"]:
            return "07:00", "23:00"
        opens, _, closes = str(row["hours"]).partition("-")
        return opens, closes

    def tables_free(self, *, sitting: str = "20:00", today: date | None = None) -> int:
        """How many of the restaurant's tables are still unbooked for a sitting."""
        booked_for = (today or datetime.now(timezone.utc).date()).isoformat()
        row = self._con.execute(
            "SELECT COUNT(*) AS n FROM table_bookings WHERE booked_for = ? AND sitting = ?"
            " AND status = 'confirmed'", (booked_for, sitting)).fetchone()
        return max(0, _TABLES - int(row["n"]))

    # --- taking an order -----------------------------------------------------------------------
    #
    # AN ORDER IS BUILT UP ACROSS TURNS, which makes it different from every other tool here. A
    # booking is one sentence and one row; an order is "the chicken kebab", then "and a mango
    # lassi", then "that's all" -- three turns that have to add up to one thing.
    #
    # It lives in the DATABASE, not in the model's context, and that is the point. A list carried in
    # the conversation is re-read by the model every turn and can come back one dish longer; a row
    # cannot. "Repeat my order" is then a lookup with `llm_ms = 0`, and it is the same answer every
    # time it is asked, which is what a caller reading back an order actually needs.
    #
    # `self._order_id` ties the open order to THIS call. Every call builds its own store and so its
    # own `Bookings`, so two callers cannot add to each other's order even though both are writing
    # to one database.

    def _open_order(self, *, create: bool = False) -> int | None:
        """The order this call is building, if there is one."""
        if self._order_id is not None:
            return self._order_id
        if not create:
            return None
        with self._lock:
            guest_id = self._new_guest()
            # `restaurant`, not `room_service`, and the schema settles it: `order_source` is a
            # CHECK constraint over exactly those two, and the existing room-service rows all carry
            # a `room_id`. A caller on the telephone has not told us their room, so claiming room
            # service would put an order in the kitchen's queue with nowhere to take it.
            cur = self._con.execute(
                "INSERT INTO restaurant_orders (guest_id, room_id, order_source, status,"
                " created_at) VALUES (?,?,?,?,?)",
                (guest_id, None, "restaurant", _ORDER_OPEN,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self._con.commit()
            self._order_id = int(cur.lastrowid)
        return self._order_id

    def add_to_order(self, *, dish: str, quantity: int = 1) -> Order:
        """Put a dish on the order. Returns the WHOLE order, not just the line added.

        Returning everything is deliberate: the renderer can then confirm the new item and say what
        the order now comes to in one breath, which is what stops a caller having to ask.

        The price is read from `menu_items` and COPIED onto the order line. That is how a price
        stays correct: if the kitchen reprices a dish overnight, an order taken today still totals
        what the caller was told, and nothing here can write back to the menu.
        """
        if quantity < 1:
            raise BookingRefused("min_one_item")
        if quantity > _MAX_PER_DISH:
            raise BookingRefused("too_many_of_one_dish", most=_MAX_PER_DISH)

        row = self._con.execute(
            "SELECT item_id, name, price_inr, available FROM menu_items"
            " WHERE lower(name) = lower(?)", (str(dish).strip(),)).fetchone()
        if row is None:
            raise UnknownRecord(f"no such dish: {dish!r}")
        # A dish the kitchen has run out of is refused rather than ordered and disappointed later.
        # `check_availability` already answers this question; an order must not quietly disagree.
        if not row["available"]:
            raise BookingRefused("dish_unavailable", dish=row["name"])

        order_id = self._open_order(create=True)
        with self._lock:
            # One line per dish, so "two kebabs" and "a kebab, and another kebab" read back the
            # same. A caller who says the second thing does not expect to hear two lines.
            existing = self._con.execute(
                "SELECT order_item_id, quantity FROM restaurant_order_items"
                " WHERE order_id = ? AND item_id = ?", (order_id, row["item_id"])).fetchone()
            if existing is None:
                self._con.execute(
                    "INSERT INTO restaurant_order_items (order_id, item_id, quantity,"
                    " unit_price_inr) VALUES (?,?,?,?)",
                    (order_id, row["item_id"], quantity, float(row["price_inr"])))
            else:
                total = int(existing["quantity"]) + quantity
                if total > _MAX_PER_DISH:
                    raise BookingRefused("too_many_of_one_dish", most=_MAX_PER_DISH)
                self._con.execute(
                    "UPDATE restaurant_order_items SET quantity = ? WHERE order_item_id = ?",
                    (total, existing["order_item_id"]))
            self._con.commit()
        return self.current_order()

    def current_order(self) -> Order | None:
        """What is on the order right now, or None if nothing has been ordered.

        None is a real answer and the renderer speaks it as one -- "you have not ordered anything
        yet" is correct, and is what "repeat my order" used to answer with the entire menu.
        """
        order_id = self._open_order()
        if order_id is None:
            return None
        rows = self._con.execute(
            "SELECT m.name AS name, i.quantity AS quantity, i.unit_price_inr AS unit_price"
            " FROM restaurant_order_items i JOIN menu_items m USING(item_id)"
            " WHERE i.order_id = ? ORDER BY i.order_item_id", (order_id,)).fetchall()
        if not rows:
            return None
        status = self._con.execute(
            "SELECT status FROM restaurant_orders WHERE order_id = ?", (order_id,)).fetchone()
        return Order(
            reference=str(order_id),
            lines=tuple(OrderLine(r["name"], int(r["quantity"]), float(r["unit_price"]))
                        for r in rows),
            placed=bool(status and status["status"] != _ORDER_OPEN),
        )

    def place_order(self) -> Order:
        """Send it to the kitchen. After this the caller is told it is being prepared."""
        order = self.current_order()
        if order is None:
            raise BookingRefused("nothing_ordered")
        if order.placed:
            raise BookingRefused("already_placed", reference=order.reference)
        with self._lock:
            self._con.execute("UPDATE restaurant_orders SET status = ? WHERE order_id = ?",
                              (_ORDER_PLACED, int(order.reference)))
            self._con.commit()
        # The call may go on to order again; a placed order is finished, so the next dish starts a
        # new one rather than reopening this.
        self._order_id = None
        return Order(reference=order.reference, lines=order.lines, placed=True)

    def remove_from_order(self, *, dish: str, quantity: int | None = None) -> tuple[Order | None,
                                                                                   str, int]:
        """Take a dish off the order. Returns (the order after, the dish's real name, how many went).

        WHY THIS EXISTS, and it is not a nice-to-have. Until 2026-09-18 there was no way to remove
        anything, and the router had no rule for the word -- so "remove paneer butter masala" was
        read as an ORDER for paneer butter masala. A real caller said it twice, each time with a
        count, and watched their order go from one to two to four of the dish they were trying to
        get rid of. **A removal that adds is worse than no removal at all**, because the caller is
        actively trying to correct it and every attempt makes it worse.

        `quantity=None` means the whole line, which is what "remove the paneer butter masala"
        means. A number removes that many and leaves the rest.
        """
        order_id = self._open_order()
        if order_id is None:
            raise BookingRefused("nothing_ordered")

        row = self._con.execute(
            "SELECT i.order_item_id, i.quantity, m.name FROM restaurant_order_items i"
            " JOIN menu_items m USING(item_id)"
            " WHERE i.order_id = ? AND lower(m.name) = lower(?)",
            (order_id, str(dish).strip())).fetchone()
        if row is None:
            raise BookingRefused("not_on_the_order", dish=str(dish).strip())

        on_it = int(row["quantity"])
        going = on_it if quantity is None else min(int(quantity), on_it)
        with self._lock:
            if going >= on_it:
                self._con.execute("DELETE FROM restaurant_order_items WHERE order_item_id = ?",
                                  (row["order_item_id"],))
            else:
                self._con.execute(
                    "UPDATE restaurant_order_items SET quantity = ? WHERE order_item_id = ?",
                    (on_it - going, row["order_item_id"]))
            self._con.commit()
        return self.current_order(), str(row["name"]), going

    def cancel_order(self) -> Order:
        """Drop the order the caller was building.

        Marked `cancelled` rather than deleted. The kitchen's own vocabulary has a word for this
        and an order that was taken and then dropped is something a hotel keeps a record of; the
        caller cannot read it back either way, because `_order_id` is cleared and a cancelled row
        is never reopened.
        """
        order = self.current_order()
        if order is None:
            raise BookingRefused("nothing_ordered")
        if order.placed:
            raise BookingRefused("already_placed", reference=order.reference)
        with self._lock:
            self._con.execute("UPDATE restaurant_orders SET status = ? WHERE order_id = ?",
                              (_ORDER_CANCELLED, int(order.reference)))
            self._con.commit()
        self._order_id = None
        return order

    def find(self, reference: str) -> dict:
        """Look a booking up by the number AETHER read out when it made it.

        WHY THIS EXISTS. `my_booking` remembers what THIS call booked, which is the right answer to
        "what was my reference?" -- a caller who has just booked has nothing else to be looked up
        by. But a caller who rings back the next day and *gives* the number was told "you have not
        made a booking on this call yet", while the room they had booked was still correctly
        reserved. The hotel knew the booking and denied it, which is the worst of both.

        Rooms and tables share one numbering space in the caller's mind -- it is "your reference" in
        both cases -- so both tables are searched and the row says which it was.
        """
        digits = "".join(ch for ch in str(reference) if ch.isdigit())
        if not digits:
            raise UnknownRecord(f"not a booking reference: {reference!r}")

        row = self._con.execute(
            "SELECT rs.reservation_id, rs.check_in, rs.check_out, rs.status, rs.adults,"
            " rm.room_number, t.name AS room_type"
            " FROM reservations rs JOIN rooms rm USING(room_id)"
            " JOIN room_types t ON t.room_type_id = rm.room_type_id"
            " WHERE rs.reservation_id = ?", (digits,)).fetchone()
        if row is not None:
            return {"kind": "room", "reference": str(row["reservation_id"]),
                    "room_number": str(row["room_number"]), "room_type": str(row["room_type"]),
                    "check_in": row["check_in"], "check_out": row["check_out"],
                    "status": row["status"], "guests": row["adults"]}

        row = self._con.execute(
            "SELECT booking_id, party_size, sitting, booked_for, status"
            " FROM table_bookings WHERE booking_id = ?", (digits,)).fetchone()
        if row is not None:
            return {"kind": "table", "reference": str(row["booking_id"]),
                    "party_size": row["party_size"], "sitting": row["sitting"],
                    "booked_for": row["booked_for"], "status": row["status"]}

        raise UnknownRecord(f"no booking with reference {digits}")

    # --- cancelling --------------------------------------------------------------------------

    def cancel(self, reference: str) -> str:
        """Cancel a booking by its reference. Returns what was cancelled: "room" or "table"."""
        digits = "".join(ch for ch in str(reference) if ch.isdigit())
        if not digits:
            raise UnknownRecord(f"not a booking reference: {reference!r}")

        with self._lock:
            row = self._con.execute(
                "SELECT room_id FROM reservations WHERE reservation_id = ? AND status != 'cancelled'",
                (digits,)).fetchone()
            if row is not None:
                self._con.execute(
                    "UPDATE reservations SET status = 'cancelled' WHERE reservation_id = ?",
                    (digits,))
                self._con.execute("UPDATE rooms SET status = 'available' WHERE room_id = ?",
                                  (row["room_id"],))
                self._con.commit()
                return "room"

            row = self._con.execute(
                "SELECT booking_id FROM table_bookings WHERE booking_id = ? AND status != 'cancelled'",
                (digits,)).fetchone()
            if row is not None:
                self._con.execute(
                    "UPDATE table_bookings SET status = 'cancelled' WHERE booking_id = ?", (digits,))
                self._con.commit()
                return "table"

        raise UnknownRecord(f"no live booking with reference {digits}")

    # --- helpers -----------------------------------------------------------------------------

    def _new_guest(self) -> int:
        cur = self._con.execute(
            "INSERT INTO guests (full_name, phone, email, language) VALUES (?,?,?,?)",
            (_TELEPHONE_GUEST, None, None, "English"))
        return int(cur.lastrowid)


# The restaurant's size. Not in the database because the database describes the MENU, not the room
# it is served in -- and inventing a `restaurant_tables` table to hold one integer would be worse
# than naming it here, where the reason is visible.
_TABLES = 12
_MAX_PARTY = 12
