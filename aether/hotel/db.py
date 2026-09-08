"""The hotel's data, read from SQLite. THE SOURCE OF TRUTH for everything AETHER says about it.

`data/aether_hotel.db` replaces the hand-written fixture that lived in this package. A caller
asking "how much is the chicken kebab" gets the hotel's actual price, and a caller asking "is room
four one two free" gets the hotel's actual room status -- neither is a number a language model
produced, and neither is a constant somebody has to remember to update in two places.

**Read-only, and enforced rather than promised.** The connection is opened with SQLite's
`mode=ro` URI, so a write does not merely fail review -- it raises. Reservation creation, order
placement and room changes are deliberately out of scope, and this module could not perform them if
asked.

Structure:

    HotelDB       one connection, one query per question, dataclasses out
    HotelStore    what `ToolRunner` is handed; caches the read-mostly tables

Nothing here formats speech, decides routing, or knows what a generation is. It answers questions
about the hotel and stops, so the tools above it stay the only place that turns data into words and
`ToolRunner` stays the only place that decides whether those words may be spoken.

**What the database does NOT hold**, recorded because the fixture did and something had to give:

* **Spice level.** There is no column. Menu items carry a prose `description` that sometimes
  mentions spices, so "is the paneer tikka spicy" is answered by reading that description back
  rather than by inventing a heat level. `find_by_spice` is gone: filtering the whole menu by a
  field that does not exist is not something to fake.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from ..errors import ToolLookupError

# Repository-relative, so a worker started from anywhere finds it.
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "aether_hotel.db"


class HotelDataUnavailable(RuntimeError):
    """The database is missing or unreadable. Raised loudly rather than answered around."""


class UnknownRecord(ToolLookupError):
    """A room, dish, service or reservation the hotel does not have.

    Never guessed at and never approximated to a near match: quoting the wrong room's status or
    the wrong dish's allergens is worse than admitting the question was not understood.
    """


# --- what a row becomes ---------------------------------------------------------------------
#
# Frozen, so a record handed to a tool result stays a faithful snapshot of the moment it was read
# and cannot be edited by anything downstream.

@dataclass(frozen=True)
class MenuItem:
    item_id: int
    name: str
    category: str
    description: str
    price: float
    vegetarian: bool
    vegan: bool
    contains_egg: bool
    allergens: tuple[str, ...]
    available: bool


@dataclass(frozen=True)
class RoomType:
    room_type_id: int
    name: str
    description: str
    rate: float
    max_guests: int
    amenities: tuple[str, ...]


@dataclass(frozen=True)
class Room:
    room_id: int
    number: str
    room_type: str
    floor: int
    status: str
    view: str
    rate: float


@dataclass(frozen=True)
class Service:
    service_id: int
    name: str
    description: str
    availability: str
    extension: str


@dataclass(frozen=True)
class Reservation:
    reservation_id: int
    guest_name: str
    room_number: str
    room_type: str
    check_in: str
    check_out: str
    status: str


@dataclass(frozen=True)
class HotelInfo:
    name: str
    description: str
    phone: str
    address: str
    check_in_time: str
    check_out_time: str
    currency: str


def _allergens(raw: str | None) -> tuple[str, ...]:
    """"Dairy, Nuts" -> ("dairy", "nuts"). Lowercased so the router and the tools agree."""
    if not raw or raw.strip() in ("", "-"):
        return ()
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class HotelDB:
    """One read-only connection to the hotel database.

    Threading: the turn loop runs on its own thread while the console reads state on another, so
    the connection is created with `check_same_thread=False` and every query holds a lock. SQLite
    serialises readers anyway; the lock is there so the cursor is not shared mid-fetch.
    """

    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = Path(path)
        if not self.path.exists():
            raise HotelDataUnavailable(
                f"hotel database not found at {self.path}. "
                "It is the source of truth for menu, rooms, services and reservations."
            )
        try:
            # mode=ro is the guarantee. A write raises rather than being caught in review.
            self._db = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro", uri=True, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise HotelDataUnavailable(f"cannot open {self.path}: {exc}") from exc
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # --- the hotel itself -------------------------------------------------------------

    def hotel(self) -> HotelInfo:
        r = self._one("SELECT * FROM hotel LIMIT 1")
        if r is None:
            raise HotelDataUnavailable("the hotel table is empty")
        return HotelInfo(
            name=r["name"], description=r["description"], phone=r["phone"],
            address=r["address"], check_in_time=r["check_in_time"],
            check_out_time=r["check_out_time"], currency=r["currency"],
        )

    # --- menu -------------------------------------------------------------------------

    _MENU_SQL = """
        SELECT m.item_id, m.name, c.name AS category, m.description, m.price_inr,
               m.vegetarian, m.vegan, m.contains_egg, m.allergens, m.available,
               c.display_order
        FROM menu_items m JOIN menu_categories c USING(category_id)
    """

    def _menu_row(self, r: sqlite3.Row) -> MenuItem:
        return MenuItem(
            item_id=r["item_id"], name=r["name"], category=r["category"].lower(),
            description=r["description"] or "", price=float(r["price_inr"]),
            vegetarian=bool(r["vegetarian"]), vegan=bool(r["vegan"]),
            contains_egg=bool(r["contains_egg"]), allergens=_allergens(r["allergens"]),
            available=bool(r["available"]),
        )

    def menu(self) -> list[MenuItem]:
        """Every item, sold out included. Callers filter; the store does not hide rows."""
        return [self._menu_row(r) for r in
                self._all(self._MENU_SQL + " ORDER BY c.display_order, m.name")]

    def categories(self) -> list[str]:
        return [r["name"].lower() for r in
                self._all("SELECT name FROM menu_categories ORDER BY display_order")]

    # --- rooms ------------------------------------------------------------------------

    _ROOM_SQL = """
        SELECT r.room_id, r.room_number, t.name AS room_type, r.floor, r.status, r.view,
               t.base_rate_inr
        FROM rooms r JOIN room_types t USING(room_type_id)
    """

    def _room_row(self, r: sqlite3.Row) -> Room:
        return Room(room_id=r["room_id"], number=str(r["room_number"]),
                    room_type=r["room_type"], floor=r["floor"], status=r["status"],
                    view=r["view"] or "", rate=float(r["base_rate_inr"]))

    def rooms(self) -> list[Room]:
        return [self._room_row(r) for r in self._all(self._ROOM_SQL + " ORDER BY r.room_number")]

    def room(self, number: str) -> Room:
        """One room by number. Raises rather than guessing at a near match."""
        wanted = "".join(ch for ch in str(number) if ch.isdigit())
        if not wanted:
            raise UnknownRecord(f"no room number given: {number!r}")
        r = self._one(self._ROOM_SQL + " WHERE r.room_number = ?", (wanted,))
        if r is None:
            raise UnknownRecord(f"no such room: {wanted}")
        return self._room_row(r)

    def room_types(self) -> list[RoomType]:
        return [
            RoomType(room_type_id=r["room_type_id"], name=r["name"],
                     description=r["description"] or "", rate=float(r["base_rate_inr"]),
                     max_guests=r["max_guests"], amenities=_csv(r["amenities"]))
            for r in self._all("SELECT * FROM room_types ORDER BY base_rate_inr")
        ]

    def room_type(self, name: str) -> RoomType:
        wanted = " ".join(str(name).lower().split())
        if not wanted:
            raise UnknownRecord("no room type given")
        for rt in self.room_types():
            if rt.name.lower() == wanted:
                return rt
        matches = [rt for rt in self.room_types() if wanted in rt.name.lower()]
        if len(matches) == 1:
            return matches[0]
        raise UnknownRecord(f"no such room type: {name}")

    # --- services and reservations ----------------------------------------------------

    def services(self) -> list[Service]:
        return [
            Service(service_id=r["service_id"], name=r["name"],
                    description=r["description"] or "", availability=r["availability"] or "",
                    extension=str(r["phone_extension"] or ""))
            for r in self._all("SELECT * FROM hotel_services ORDER BY name")
        ]

    def service(self, name: str) -> Service:
        wanted = " ".join(str(name).lower().split())
        for s in self.services():
            if s.name.lower() == wanted:
                return s
        matches = [s for s in self.services() if wanted and wanted in s.name.lower()]
        if len(matches) == 1:
            return matches[0]
        raise UnknownRecord(f"no such service: {name}")

    def reservations(self) -> list[Reservation]:
        return [
            Reservation(reservation_id=r["reservation_id"], guest_name=r["guest_name"],
                        room_number=str(r["room_number"]), room_type=r["room_type"],
                        check_in=r["check_in"], check_out=r["check_out"], status=r["status"])
            for r in self._all("SELECT * FROM current_reservations ORDER BY room_number")
        ]

    def reservation_for_room(self, number: str) -> Reservation:
        wanted = "".join(ch for ch in str(number) if ch.isdigit())
        for res in self.reservations():
            if res.room_number == wanted:
                return res
        raise UnknownRecord(f"no reservation for room {wanted or number}")


class HotelStore:
    """What `ToolRunner` is handed. One per call, wrapping one read-only database.

    Mirrors the shape the runner already expects -- a `state_version` stamped onto every result so
    a late answer is recognisable as describing a hotel that has since changed. It stays at zero
    while the database is read-only, and it is still stamped, because the field is the runner's
    contract and not this store's opinion.

    The read-mostly tables (menu, rooms, types, services) are cached on first use: they are read
    several times per turn by the router, the tools and the prompt, and re-querying SQLite for each
    is work with no benefit while nothing can write.
    """

    def __init__(self, db: HotelDB | None = None, path: Path | str = DEFAULT_DB_PATH):
        self.db = db if db is not None else HotelDB(path)
        self.state_version: int = 0
        self._menu: list[MenuItem] | None = None
        self._rooms: list[Room] | None = None
        self._types: list[RoomType] | None = None
        self._services: list[Service] | None = None
        self._hotel: HotelInfo | None = None

    # --- menu ---------------------------------------------------------------------------

    def menu(self) -> list[MenuItem]:
        if self._menu is None:
            self._menu = self.db.menu()
        return list(self._menu)

    def categories(self) -> list[str]:
        seen: list[str] = []
        for item in self.menu():
            if item.category not in seen:
                seen.append(item.category)
        return seen

    def in_category(self, category: str, *, available_only: bool = True) -> list[MenuItem]:
        wanted = " ".join(str(category).lower().split())
        rows = [i for i in self.menu() if i.category == wanted]
        return [i for i in rows if i.available] if available_only else rows

    def find_item(self, name: str) -> MenuItem:
        """Exact match first, then containment. Ambiguous or absent raises.

        STT will not reliably produce "chicken kebab" -- it may give "the chicken kebab" or
        "chicken kebabs". Containment handles that without a fuzzy matcher whose failures would be
        hard to explain, and an ambiguous match raises rather than guessing, because quoting the
        wrong price is worse than admitting we did not catch it.
        """
        spoken = " ".join(str(name).lower().split())
        if not spoken:
            raise UnknownRecord("no dish name given")
        for item in self.menu():
            if item.name.lower() == spoken:
                return item
        matches = [i for i in self.menu() if i.name.lower() in spoken or spoken in i.name.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise UnknownRecord(f"no dish matching: {name}")
        raise UnknownRecord(f"more than one dish matches: {name}")

    def by_diet(self, diet: str, *, category: str | None = None) -> list[MenuItem]:
        """Vegan counts as vegetarian for a caller who asks for vegetarian options.

        Somebody asking "do you have vegetarian mains" wants everything they can eat, not a
        taxonomy lesson -- excluding the vegan dishes would be a technically-correct wrong answer.
        """
        wanted = str(diet).lower()
        rows = [i for i in self.menu() if i.available]
        if wanted == "vegan":
            rows = [i for i in rows if i.vegan]
        elif wanted in ("vegetarian", "veg"):
            rows = [i for i in rows if i.vegetarian or i.vegan]
        else:
            rows = [i for i in rows if not i.vegetarian and not i.vegan]
        if category:
            wanted_cat = " ".join(str(category).lower().split())
            rows = [i for i in rows if i.category == wanted_cat]
        return rows

    # --- rooms, services, reservations, hotel -------------------------------------------

    def rooms(self) -> list[Room]:
        if self._rooms is None:
            self._rooms = self.db.rooms()
        return list(self._rooms)

    def room(self, number: str):
        return self.db.room(number)

    def room_types(self) -> list[RoomType]:
        if self._types is None:
            self._types = self.db.room_types()
        return list(self._types)

    def room_type(self, name: str):
        return self.db.room_type(name)

    def available_rooms(self, room_type: str | None = None) -> list[Room]:
        rows = [r for r in self.rooms() if r.status == "available"]
        if room_type:
            wanted = " ".join(str(room_type).lower().split())
            rows = [r for r in rows if r.room_type.lower() == wanted]
        return rows

    def services(self) -> list[Service]:
        if self._services is None:
            self._services = self.db.services()
        return list(self._services)

    def service(self, name: str):
        return self.db.service(name)

    def reservations(self):
        return self.db.reservations()

    def reservation_for_room(self, number: str):
        return self.db.reservation_for_room(number)

    def hotel(self) -> HotelInfo:
        if self._hotel is None:
            self._hotel = self.db.hotel()
        return self._hotel
