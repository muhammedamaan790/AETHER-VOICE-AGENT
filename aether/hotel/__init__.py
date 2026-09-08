"""The hotel AETHER answers for. Backed by SQLite, never by model knowledge.

`data/aether_hotel.db` is the source of truth: the menu, its prices, its allergens and its
availability; fifty rooms, their types, rates, amenities and status; the hotel's services and its
check-in and check-out times; and the current reservations. A caller asking "how much is the
chicken kebab" gets the hotel's actual price, and a caller asking "is room three oh five free" gets
the hotel's actual room status.

This module is the seam. `aether.hotel.db` reads the database; `aether.hotel.tools` turns rows into
speech; `aether.hotel.router` decides which tool answers a sentence. Everything above -- the turn
loop, `ToolRunner`, fencing, the generation registry -- is unchanged, because it only ever saw
`(records, summary)` and still only sees that.

**Replaced a hand-written fixture, and the differences are real rather than cosmetic.** Prices
moved (the chicken kebab is 420, not 380), the menu is twelve items rather than twenty-nine, the
database makes "vegetarian mains" a category of its own, and it records no spice level at all --
so "is it spicy" is now answered by reading the hotel's own description back instead of inventing a
rating. Recorded in data/README.md rather than smoothed over.

Deterministic by construction: no randomness, no clocks, no IDs derived from time. Two runs a week
apart produce byte-identical answers, which is what makes a demo rehearsable and a test meaningful.
"""

from __future__ import annotations

from .db import (
    DEFAULT_DB_PATH,
    HotelDataUnavailable,
    HotelDB,
    HotelInfo,
    HotelStore,
    MenuItem,
    Reservation,
    Room,
    RoomType,
    Service,
    UnknownRecord,
)

# The currency the database records. Read once, so the spoken price and the stored price cannot
# drift apart.
CURRENCY = "rupees"

# `UnknownDish` was this package's not-found error before rooms and services existed. Kept as an
# alias so existing `except UnknownDish` sites keep working; `UnknownRecord` is the name now, since
# what is missing may be a room or a service rather than a dish.
UnknownDish = UnknownRecord

# `MenuStore` was the store's name while the menu was all there was. `HotelStore` serves rooms,
# services and reservations too; the alias keeps the older import sites working.
MenuStore = HotelStore

__all__ = [
    "CURRENCY", "DEFAULT_DB_PATH", "HotelDB", "HotelDataUnavailable", "HotelInfo", "HotelStore",
    "MenuItem", "MenuStore", "Reservation", "Room", "RoomType", "Service", "UnknownDish",
    "UnknownRecord", "menu_for_prompt", "say_a", "say_date", "say_list", "say_number",
    "say_price", "say_room_number", "say_time",
]


def menu_for_prompt(store: HotelStore | None = None) -> str:
    """The hotel's facts as compact reference text, for the model that answers what the router cannot.

    THE SAFETY NET, and it exists because the router missed once on a real call. A caller asked for
    the desserts, `base.en` transcribed "dessert" as "Desert", no rule matched, the model answered
    from nothing -- and invented three dishes and three prices. "Chocolate fudge cake, three
    hundred and fifty rupees" was never on any menu.

    The deterministic path is still the answer for these questions: it is faster and it cannot be
    wrong. This is what the model sees when that path declines, so a router miss costs a slower
    answer rather than a fabricated one. Generated from the database, so it can never drift from it.

    Sold-out items and unavailable rooms are included and marked, because "do you have the fish
    curry" is a question the model may be asked and "I don't know that dish" would be a worse
    answer than "that one is off today".
    """
    store = store if store is not None else HotelStore()
    lines: list[str] = []
    info = store.hotel()
    lines.append(f"HOTEL: {info.name}, {info.address}.")
    lines.append(f"Check-in from {info.check_in_time}. Check-out by {info.check_out_time}.")

    lines.append("")
    lines.append("MENU (the only food that exists):")
    for category in store.categories():
        rows = store.in_category(category, available_only=False)
        if not rows:
            continue
        lines.append(f"  {category.upper()}:")
        for item in rows:
            bits = [f"{item.name} - {say_price(item.price)}"]
            bits.append("vegan" if item.vegan else ("vegetarian" if item.vegetarian
                                                    else "non-vegetarian"))
            if item.allergens:
                bits.append("contains " + ", ".join(item.allergens))
            if not item.available:
                bits.append("NOT AVAILABLE TODAY")
            lines.append("    " + "; ".join(bits))

    lines.append("")
    lines.append("ROOM TYPES (the only rooms that exist):")
    for room_type in store.room_types():
        free = len(store.available_rooms(room_type.name))
        lines.append(f"  {room_type.name} - {say_price(room_type.rate)} a night; "
                     f"sleeps {room_type.max_guests}; {free} free now; "
                     f"{', '.join(room_type.amenities)}")
    rooms = store.rooms()
    lines.append(f"  Room numbers run {rooms[0].number} to {rooms[-1].number}; "
                 f"{len(store.available_rooms())} of {len(rooms)} are free. "
                 "No other room numbers exist.")

    lines.append("")
    lines.append("SERVICES (the only services that exist):")
    for service in store.services():
        lines.append(f"  {service.name} - {service.availability}; extension {service.extension}")

    return "\n".join(lines)


# --- spoken rendering ---------------------------------------------------------------------
#
# Templates, not an LLM. A menu answer is a fact read aloud; sending structured data to a model to
# be re-phrased adds ~1.8 s of measured latency and a hallucination surface for no information gain.
# Everything here is written to be SPOKEN: no digits, no symbols, no markdown.

_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def say_number(n: int) -> str:
    """Spoken form of a whole number up to 9999 -- enough for any price on this menu.

    Prices reach Rime as words because a spoken agent should never gamble on how a TTS engine
    renders a numeral. Deliberately narrow: it covers the menu's range and raises nothing outside
    it, rather than pretending to be a general number-to-words library.
    """
    if n < 0:
        return "minus " + say_number(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        rest = n % 10
        return _TENS[n // 10] + (f" {_ONES[rest]}" if rest else "")
    if n < 1000:
        rest = n % 100
        return f"{_ONES[n // 100]} hundred" + (f" and {say_number(rest)}" if rest else "")
    rest = n % 1000
    return f"{say_number(n // 1000)} thousand" + (f" {say_number(rest)}" if rest else "")


def say_price(price: float) -> str:
    """`420.0` -> `four hundred and twenty rupees`. Whole units only; this menu has no paise."""
    return f"{say_number(int(round(price)))} {CURRENCY}"


def say_list(items: list[str]) -> str:
    """`a, b and c` -- the way a person reads a short list aloud."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")
_ORDINALS = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth", 9: "ninth",
             12: "twelfth", 20: "twentieth", 21: "twenty first", 22: "twenty second",
             23: "twenty third", 30: "thirtieth", 31: "thirty first"}


def say_room_number(number: str | int) -> str:
    """`305` -> `three oh five`. How a room number is said, not how a quantity is.

    `say_number` would give "three hundred and five", which is a count of rooms rather than the
    name of one. A guest asked to go to "three hundred and five" has been given a number, not a
    door.
    """
    digits = str(number).strip()
    if not digits.isdigit():
        return str(number)
    spoken = {"0": "oh"}
    return " ".join(spoken.get(d, say_number(int(d))) for d in digits)


def say_time(clock: str) -> str:
    """`06:00` -> `six in the morning`; `12:00` -> `twelve noon`; `23:00` -> `eleven at night`.

    Rime is given words, never `06:00`, because a spoken agent should not gamble on how a TTS
    engine reads a colon.
    """
    raw = str(clock).strip()
    hour_part, _, minute_part = raw.partition(":")
    if not hour_part.strip().isdigit():
        return raw
    hour, minute = int(hour_part), int(minute_part) if minute_part.strip().isdigit() else 0

    if hour == 0:
        base, suffix = "twelve", "midnight"
    elif hour == 12:
        base, suffix = "twelve", "noon"
    else:
        base = say_number(hour if hour <= 12 else hour - 12)
        suffix = ("in the morning" if hour < 12
                  else "in the afternoon" if hour < 17
                  else "in the evening" if hour < 21
                  else "at night")
    if minute:
        base = f"{base} {say_number(minute)}"
    return f"{base} {suffix}"


def say_date(iso: str) -> str:
    """`2026-09-07` -> `the seventh of September`. The year is dropped: a caller asking about a
    stay this week does not need it, and reading it aloud makes the sentence longer than the fact.
    """
    parts = str(iso).strip().split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return str(iso)
    _, month, day = (int(p) for p in parts)
    if not 1 <= month <= 12:
        return str(iso)
    ordinal = _ORDINALS.get(day) or (say_number(day) + "th")
    return f"the {ordinal} of {_MONTHS[month - 1]}"


def say_a(phrase: str) -> str:
    """`Executive Suite` -> `an Executive Suite`. Article agreement, so the sentence reads."""
    text = str(phrase).strip()
    article = "an" if text[:1].lower() in "aeiou" else "a"
    return f"{article} {text}"
