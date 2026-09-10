"""Hotel tools, and the templates that turn their results into speech.

These plug into the EXISTING `aether.tools.ToolRunner` via its injectable registry. Nothing about
fencing, delay injection or result identity is reimplemented here -- the fence check placed after
the delay and before the tool body, the task/generation/state stamping and the canonical
`TaskStarted`/`ResultReceived`/`ResultDiscarded` events all come from that runner unchanged. A
second tool layer would mean a second place for a stale result to slip through.

Every tool body follows one contract: take the store plus validated parameters, return
`(records, summary)`, emit nothing, sleep for nothing, check nothing.

**Every fact comes from `data/aether_hotel.db`.** Not one price, room number, allergen, opening time
or service extension is written in this file. A menu answer is a fact read aloud; routing
`{"name": "Chicken Kebab", "price": 420}` through Gemini to be re-phrased costs a measured ~1.8 s
and introduces a way to quote a price the hotel does not charge -- which it has already done once,
inventing three desserts that do not exist.

Every template is written to be SPOKEN: numbers as words, no symbols, no markdown, one or two short
sentences. Rime receives what a person would say.
"""

from __future__ import annotations

from collections.abc import Callable

from . import (
    say_a,
    say_date,
    say_list,
    say_number,
    say_price,
    say_room_number,
    say_time,
)
from .db import HotelStore, UnknownRecord

# NOTE: the dish parameter is `dish`, not `name`. `ToolRunner.run(name, ...)` already takes the
# TOOL's name positionally, so a tool parameter called `name` collides with it. Worth stating
# because `name` is the obvious thing to call it and the failure is a confusing TypeError.

# A telephone is not a printed menu. Reading a dozen names aloud is a wall of speech the caller
# cannot hold in their head, so long lists are capped and the remainder is COUNTED rather than
# dropped -- the caller is told there is more, and can ask.
_SPOKEN_LIST_MAX = 6


def _say_names(names: list[str]) -> str:
    if len(names) <= _SPOKEN_LIST_MAX:
        return say_list(names)
    rest = len(names) - _SPOKEN_LIST_MAX
    more = "one more" if rest == 1 else f"{say_number(rest)} more"
    return f"{say_list(names[:_SPOKEN_LIST_MAX])}, plus {more}"


def _item(row) -> dict:
    """One menu item flattened into the plain dict a tool result carries.

    Done here rather than in each tool so every item is described identically, and so what reaches
    a caller is data rather than a live database row.
    """
    return {
        "item_id": row.item_id, "name": row.name, "category": row.category,
        "description": row.description, "price": row.price,
        "vegetarian": row.vegetarian, "vegan": row.vegan, "contains_egg": row.contains_egg,
        "allergens": list(row.allergens), "available": row.available,
    }


def _room(row) -> dict:
    return {"room_id": row.room_id, "number": row.number, "room_type": row.room_type,
            "floor": row.floor, "status": row.status, "view": row.view, "rate": row.rate}


def _room_type(row) -> dict:
    return {"room_type_id": row.room_type_id, "name": row.name, "description": row.description,
            "rate": row.rate, "max_guests": row.max_guests, "amenities": list(row.amenities)}


def _service(row) -> dict:
    return {"service_id": row.service_id, "name": row.name, "description": row.description,
            "availability": row.availability, "extension": row.extension}


def _reservation(row) -> dict:
    return {"reservation_id": row.reservation_id, "guest_name": row.guest_name,
            "room_number": row.room_number, "room_type": row.room_type,
            "check_in": row.check_in, "check_out": row.check_out, "status": row.status}


def _parse_category(store: HotelStore, value: str) -> str:
    """Map spoken words to one of the database's own categories. Raises rather than guessing.

    The categories come from `menu_categories`, so adding one to the database adds it here with no
    code change. The synonyms are the words callers actually use for them.
    """
    spoken = " ".join(str(value).lower().split())
    known = store.categories()
    if spoken in known:
        return spoken
    synonyms = {
        "starter": "starters", "appetiser": "starters", "appetisers": "starters",
        "appetizer": "starters", "appetizers": "starters",
        "main": "mains", "main course": "mains", "entree": "mains",
        "vegetarian main": "vegetarian mains", "veg mains": "vegetarian mains",
        "dessert": "desserts", "sweet": "desserts", "sweets": "desserts",
        "pudding": "desserts", "puddings": "desserts",
        "drink": "drinks", "beverage": "drinks", "beverages": "drinks",
    }
    resolved = synonyms.get(spoken)
    if resolved in known:
        return resolved
    raise UnknownRecord(f"no such menu category: {value}")


def _parse_diet(value: str) -> str:
    spoken = str(value).lower().strip()
    if spoken in ("veg", "vegetarian"):
        return "vegetarian"
    if spoken in ("vegan", "plant based", "plant-based"):
        return "vegan"
    if spoken in ("non veg", "non-veg", "nonveg", "non vegetarian", "non-vegetarian", "meat"):
        return "non-vegetarian"
    raise UnknownRecord(f"no such dietary preference: {value}")


def _known_allergens(store: HotelStore) -> set[str]:
    """Every allergen the database actually records, so an unknown one raises rather than
    returning a confident "nothing contains it"."""
    found: set[str] = set()
    for item in store.menu():
        found.update(item.allergens)
    return found


_ALLERGEN_ALIASES = {
    "nut": "nuts", "nuts": "nuts", "peanut": "nuts", "peanuts": "nuts", "tree nut": "nuts",
    "dairy": "dairy", "milk": "dairy", "lactose": "dairy", "cream": "dairy", "cheese": "dairy",
    "gluten": "gluten", "wheat": "gluten",
    "shellfish": "shellfish", "prawn": "shellfish", "prawns": "shellfish", "shrimp": "shellfish",
    "crab": "shellfish", "fish": "fish", "seafood": "fish",
    "egg": "egg", "eggs": "egg",
}


def _parse_allergen(store: HotelStore, value: str) -> str:
    """Resolve a spoken allergen to one the database records.

    A closed set on purpose: an allergen this menu does not track must raise rather than quietly
    return "nothing contains it", which is the one wrong answer here that could put somebody in
    hospital.
    """
    spoken = " ".join(str(value).lower().split())
    canonical = _ALLERGEN_ALIASES.get(spoken, spoken)
    if canonical not in _known_allergens(store):
        raise UnknownRecord(f"the menu does not record an allergen called: {value}")
    return canonical


# --- menu tools ----------------------------------------------------------------------------

def _list_category(store: HotelStore, *, category: str) -> tuple[list, dict]:
    """"What starters do you have?" -- the demo's opening question."""
    parsed = _parse_category(store, category)
    rows = store.in_category(parsed)
    sold_out = [i.name for i in store.in_category(parsed, available_only=False) if not i.available]
    return [_item(i) for i in rows], {"category": parsed, "sold_out": sold_out}


def _price_of(store: HotelStore, *, dish: str) -> tuple[list, dict]:
    """"How much is the chicken kebab?" """
    found = store.find_item(dish)
    return [_item(found)], {"name": found.name, "price": found.price,
                            "available": found.available}


def _find_by_diet(store: HotelStore, *, diet: str,
                  category: str | None = None) -> tuple[list, dict]:
    """"Do you have any vegetarian main courses?" """
    parsed_diet = _parse_diet(diet)
    parsed_category = _parse_category(store, category) if category else None
    rows = store.by_diet(parsed_diet, category=parsed_category)
    return [_item(i) for i in rows], {"diet": parsed_diet, "category": parsed_category}


def _check_availability(store: HotelStore, *, dish: str) -> tuple[list, dict]:
    """"Do you still have the fish curry?" """
    found = store.find_item(dish)
    return [_item(found)], {"name": found.name, "available": found.available}


def _check_allergens(store: HotelStore, *, dish: str) -> tuple[list, dict]:
    """"Does the paneer butter masala have nuts?" -- the question a caller asks about themselves.

    Answered from the database, never from the model. Getting an allergen wrong is the one menu
    error that could actually hurt somebody, so it must never be inferred.
    """
    found = store.find_item(dish)
    return [_item(found)], {"name": found.name, "allergens": list(found.allergens),
                            "contains_egg": found.contains_egg}


def _safe_for(store: HotelStore, *, allergen: str,
              category: str | None = None) -> tuple[list, dict]:
    """"I have a nut allergy, what can I eat?" -- the inverse of `check_allergens`.

    `check_allergens` answers about a dish the caller has named. This answers when they have named
    only the allergen, which is how the question is actually asked on a phone.
    """
    wanted = _parse_allergen(store, allergen)
    parsed_category = _parse_category(store, category) if category else None
    in_scope = [i for i in store.menu()
                if i.available and (parsed_category is None or i.category == parsed_category)]
    safe = [i for i in in_scope if wanted not in i.allergens]
    avoid = [i.name for i in in_scope if wanted in i.allergens]
    return [_item(i) for i in safe], {"allergen": wanted, "category": parsed_category,
                                      "avoid": avoid}


def _describe_item(store: HotelStore, *, dish: str) -> tuple[list, dict]:
    """"Tell me about the paneer tikka" / "is it spicy?"

    The database records a prose `description`, not a spice level, so a question about heat is
    answered by reading the hotel's own description back rather than by inventing a rating. The
    old fixture had a `spice` column; the database does not, and faking one would be exactly the
    class of invention this whole path exists to prevent.
    """
    found = store.find_item(dish)
    return [_item(found)], {"name": found.name, "description": found.description,
                            "available": found.available}


def _menu_overview(store: HotelStore) -> tuple[list, dict]:
    """"What's on the menu?" -- the broadest question, and usually the first one.

    Answers the SHAPE of the menu rather than its contents: which courses exist and which diets are
    catered for. Reading every dish down a telephone is not an answer.
    """
    available = [i for i in store.menu() if i.available]
    categories = [c for c in store.categories() if any(i.category == c for i in available)]
    diets = []
    if any(i.vegetarian and not i.vegan for i in available):
        diets.append("vegetarian")
    if any(i.vegan for i in available):
        diets.append("vegan")
    if any(not i.vegetarian and not i.vegan for i in available):
        diets.append("non-vegetarian")
    return [_item(i) for i in available], {"categories": categories, "diets": diets,
                                           "dish_count": len(available)}


# --- room tools ----------------------------------------------------------------------------

def _room_status(store: HotelStore, *, room: str) -> tuple[list, dict]:
    """"Is room three oh five free?" -- one room, by number."""
    found = store.room(room)
    return [_room(found)], {"number": found.number, "status": found.status,
                            "room_type": found.room_type, "rate": found.rate}


def _room_availability(store: HotelStore, *, room_type: str | None = None) -> tuple[list, dict]:
    """"Do you have any rooms free tonight?" / "any suites available?" """
    parsed = store.room_type(room_type).name if room_type else None
    rows = store.available_rooms(parsed)
    return [_room(r) for r in rows], {"room_type": parsed, "count": len(rows),
                                      "total": len(store.rooms())}


def _list_room_types(store: HotelStore) -> tuple[list, dict]:
    """"What kinds of room do you have?" """
    rows = store.room_types()
    return [_room_type(t) for t in rows], {"count": len(rows)}


def _room_price(store: HotelStore, *, room_type: str) -> tuple[list, dict]:
    """"How much is an executive suite?" """
    found = store.room_type(room_type)
    return [_room_type(found)], {"name": found.name, "rate": found.rate,
                                 "max_guests": found.max_guests}


def _room_amenities(store: HotelStore, *, room_type: str) -> tuple[list, dict]:
    """"What comes with a deluxe king?" """
    found = store.room_type(room_type)
    return [_room_type(found)], {"name": found.name, "amenities": list(found.amenities)}


# --- service, timing and reservation tools --------------------------------------------------

def _list_services(store: HotelStore) -> tuple[list, dict]:
    """"What services do you offer?" """
    rows = store.services()
    return [_service(s) for s in rows], {"count": len(rows)}


def _service_hours(store: HotelStore, *, service: str) -> tuple[list, dict]:
    """"When is room service available?" """
    found = store.service(service)
    return [_service(found)], {"name": found.name, "availability": found.availability,
                               "extension": found.extension}


def _check_in_out(store: HotelStore) -> tuple[list, dict]:
    """"What time is check-in?" -- from the hotel row, not from the model's idea of hotels."""
    info = store.hotel()
    return [{"check_in_time": info.check_in_time, "check_out_time": info.check_out_time,
             "hotel": info.name}], {"check_in": info.check_in_time,
                                    "check_out": info.check_out_time}


def _reservation_for_room(store: HotelStore, *, room: str) -> tuple[list, dict]:
    """"Is there a booking on room three oh two?"

    Returns the reservation's DATES AND STATUS and deliberately not the guest's name. A hotel line
    answers to whoever dials it, and reading a guest's name out to an unauthenticated caller is a
    disclosure the database makes easy and the product should not make casual. The name is in the
    record and stays there; the template below never speaks it.
    """
    found = store.reservation_for_room(room)
    return [_reservation(found)], {"room_number": found.room_number, "status": found.status,
                                   "check_in": found.check_in, "check_out": found.check_out,
                                   "room_type": found.room_type}


def _hotel_policy(store: HotelStore, *, topic: str) -> tuple[list, dict]:
    """"Do you have parking?", "Can I bring my dog?", "Is breakfast included?"

    One tool for every policy, because they all answer the same shape of question -- is it
    offered, does it cost, when is it available -- and one tool means one renderer per language
    rather than fifteen. `available=False` is a real answer and is returned as one; an unknown
    topic raises, so a policy the hotel has no record of becomes "I do not have that" rather than
    a plausible invention.
    """
    found = store.policy(topic)
    return [{
        "topic": found.topic, "available": found.available, "fee": found.fee,
        "hours": found.hours, "limit_hours": found.limit_hours,
        "options": list(found.options),
    }], {"topic": found.topic, "available": found.available}


def _hotel_info(store: HotelStore) -> tuple[list, dict]:
    """"Where are you?", "What is your number?", "How many floors?"

    `floors` is COUNTED from the rooms rather than stored, so it cannot contradict them.
    """
    info = store.hotel()
    return [{
        "name": info.name, "address": info.address, "phone": info.phone,
        "currency": info.currency, "floors": store.floors(),
        "rooms": len(store.rooms()),
    }], {"name": info.name, "floors": store.floors()}


HOTEL_TOOLS: dict[str, tuple[Callable[..., tuple[list, dict]], bool]] = {
    # Menu. None of these mutate: a caller asking about the menu must never be able to change it.
    "menu_overview": (_menu_overview, False),
    "list_category": (_list_category, False),
    "price_of": (_price_of, False),
    "find_by_diet": (_find_by_diet, False),
    "check_availability": (_check_availability, False),
    "check_allergens": (_check_allergens, False),
    "safe_for": (_safe_for, False),
    "describe_item": (_describe_item, False),
    # Rooms.
    "room_status": (_room_status, False),
    "room_availability": (_room_availability, False),
    "list_room_types": (_list_room_types, False),
    "room_price": (_room_price, False),
    "room_amenities": (_room_amenities, False),
    # Services, timings and reservations.
    "list_services": (_list_services, False),
    "service_hours": (_service_hours, False),
    "check_in_out": (_check_in_out, False),
    "reservation_for_room": (_reservation_for_room, False),
    # Policies and the hotel itself. Added after an audit found that the commonest questions a
    # hotel line receives -- parking, wi-fi, pets, breakfast, late checkout -- had no fact behind
    # them at all, so they reached the model with nothing to answer from.
    "hotel_policy": (_hotel_policy, False),
    "hotel_info": (_hotel_info, False),
}


# --- spoken templates -------------------------------------------------------------------------

def _speak_menu_overview(result) -> str:
    categories = result.summary.get("categories") or []
    diets = result.summary.get("diets") or []
    if not categories:
        return "I am sorry, we are not serving anything today."
    lead = f"We have {say_list(categories)}"
    return f"{lead}, with {say_list(diets)} options." if diets else f"{lead}."


def _speak_list_category(result) -> str:
    rows, category = result.records, result.summary.get("category", "menu")
    sold_out = result.summary.get("sold_out") or []
    if not rows:
        return f"I am sorry, we have nothing on the {category} menu right now."
    names = _say_names([r["name"] for r in rows])
    cheapest = min(rows, key=lambda r: r["price"])
    lead = f"For {category} we have {names}."
    price = (f" It is {say_price(rows[0]['price'])}." if len(rows) == 1
             else f" They start at {say_price(cheapest['price'])}.")
    tail = ""
    if sold_out:
        tail = f" The {say_list(sold_out)} is off today." if len(sold_out) == 1 \
               else f" The {say_list(sold_out)} are off today."
    return lead + price + tail


def _speak_price_of(result) -> str:
    dish = result.records[0]
    price = say_price(dish["price"])
    if not dish["available"]:
        return f"The {dish['name']} is {price}, but I am afraid it is not available today."
    return f"The {dish['name']} is {price}."


def _speak_find_by_diet(result) -> str:
    rows = result.records
    diet, category = result.summary.get("diet", ""), result.summary.get("category")
    # "vegetarian vegetarian mains" -- the database makes "vegetarian mains" a category of its own,
    # so naming the diet again stutters. Say the narrower one.
    if category and diet.split("-")[-1] in category:
        scope = category
    else:
        scope = f"{diet} {category}" if category else diet
    if not rows:
        return f"I am sorry, we have no {scope} options available today."
    return f"Yes. For {scope} we have {_say_names([r['name'] for r in rows])}."


def _speak_check_availability(result) -> str:
    dish = result.records[0]
    if dish["available"]:
        return f"Yes, the {dish['name']} is available today, at {say_price(dish['price'])}."
    return f"I am sorry, the {dish['name']} is not available today."


def _speak_check_allergens(result) -> str:
    dish = result.records[0]
    allergens = dish["allergens"]
    if not allergens:
        return f"The {dish['name']} has no listed allergens."
    return f"The {dish['name']} contains {say_list(allergens)}."


def _speak_safe_for(result) -> str:
    """Suggest, then warn. Never read ten dish names down a telephone.

    One suggestion per course plus the size of what to avoid is what a duty manager would say.
    Deterministic: the picks are menu order, not a choice.
    """
    allergen = result.summary.get("allergen", "")
    rows = result.records
    if not rows:
        return f"I am sorry, everything we have on today contains {allergen}."
    picks, seen = [], set()
    for row in rows:
        if row["category"] not in seen:
            seen.add(row["category"])
            picks.append(row["name"])
        if len(picks) == 3:
            break
    lead = f"If you are avoiding {allergen}, I would suggest {say_list(picks)}."
    avoid = result.summary.get("avoid") or []
    if not avoid:
        return f"{lead} Nothing on our menu today contains {allergen}."
    count, word = say_number(len(avoid)), "dish" if len(avoid) == 1 else "dishes"
    verb = "contains" if len(avoid) == 1 else "contain"
    return (f"{lead} {count.capitalize()} other {word} on the menu {verb} {allergen}, "
            f"so do check with me before you order.")


def _speak_describe_item(result) -> str:
    dish = result.records[0]
    description = (result.summary.get("description") or "").strip()
    if not description:
        return f"I do not have a description for the {dish['name']}, I am afraid."
    tail = "" if dish["available"] else " It is not available today, though."
    return f"{description}{tail}"


def _speak_room_status(result) -> str:
    room = result.records[0]
    number = say_room_number(room["number"])
    spoken = {
        "available": f"Room {number} is free. It is {say_a(room['room_type'])} at "
                     f"{say_price(room['rate'])} a night.",
        "occupied": f"Room {number} is occupied at the moment.",
        "reserved": f"Room {number} is already reserved.",
        "housekeeping": f"Room {number} is with housekeeping just now.",
        "maintenance": f"Room {number} is out for maintenance.",
    }
    return spoken.get(room["status"], f"Room {number} is marked {room['status']}.")


def _speak_room_availability(result) -> str:
    count = result.summary.get("count", 0)
    room_type = result.summary.get("room_type")
    if not count:
        return (f"I am sorry, we have no {room_type} free at the moment."
                if room_type else "I am sorry, we have nothing free at the moment.")
    what = f"{room_type} rooms" if room_type else "rooms"
    if count == 1:
        only = result.records[0]
        return (f"We have one {room_type or only['room_type']} free, room "
                f"{say_room_number(only['number'])}, at {say_price(only['rate'])} a night.")
    cheapest = min(result.records, key=lambda r: r["rate"])
    return (f"We have {say_number(count)} {what} free, starting at "
            f"{say_price(cheapest['rate'])} a night.")


def _speak_list_room_types(result) -> str:
    rows = result.records
    if not rows:
        return "I am sorry, I do not have our room types to hand."
    cheapest = min(rows, key=lambda r: r["rate"])
    return (f"We have {_say_names([r['name'] for r in rows])}, "
            f"starting at {say_price(cheapest['rate'])} a night.")


def _speak_room_price(result) -> str:
    row = result.records[0]
    guests = say_number(row["max_guests"])
    return (f"The {row['name']} is {say_price(row['rate'])} a night, "
            f"and sleeps up to {guests}.")


def _speak_room_amenities(result) -> str:
    row = result.records[0]
    amenities = row["amenities"]
    if not amenities:
        return f"I do not have the amenity list for the {row['name']}, I am afraid."
    return f"The {row['name']} has {say_list([a.lower() for a in amenities])}."


def _speak_list_services(result) -> str:
    rows = result.records
    if not rows:
        return "I am sorry, I do not have our service list to hand."
    return f"We offer {_say_names([r['name'].lower() for r in rows])}."


def _speak_service_hours(result) -> str:
    row = result.records[0]
    hours = str(row["availability"])
    if "24" in hours:
        when = "around the clock"
    else:
        opens, _, closes = hours.partition("-")
        when = f"from {say_time(opens)} until {say_time(closes)}"
    # The extension is read digit by digit, the way a phone number is given, not as a quantity.
    tail = (f" You can reach it on extension {say_room_number(row['extension'])}."
            if str(row["extension"]).isdigit() else "")
    return f"{row['name']} is available {when}.{tail}"


def _speak_check_in_out(result) -> str:
    return (f"Check-in is from {say_time(result.summary['check_in'])}, "
            f"and check-out is by {say_time(result.summary['check_out'])}.")


def _speak_reservation_for_room(result) -> str:
    """States that a booking exists and its dates. Deliberately never the guest's name.

    A hotel line answers to whoever dials it. The name is in the record and stays there.
    """
    s = result.summary
    number = say_room_number(s["room_number"])
    status = {"checked_in": "occupied by a guest who has checked in",
              "confirmed": "held on a confirmed booking",
              "cancelled": "no longer booked"}.get(s["status"], f"marked {s['status']}")
    return (f"Room {number} is {status}. It is {say_a(s['room_type'])}, "
            f"booked from {say_date(s['check_in'])} to {say_date(s['check_out'])}.")


# What each policy is called when spoken. Machine keys are for the database; nobody says
# "early_check_in" on a telephone.
_POLICY_NAMES = {
    "parking": "parking", "wifi": "Wi-Fi", "breakfast": "breakfast", "pets": "pets",
    "smoking": "smoking", "children": "children", "airport_transfer": "an airport transfer",
    "early_check_in": "early check-in", "late_check_out": "late check-out",
    "luggage_storage": "luggage storage", "accessibility": "step-free access",
    "cancellation": "free cancellation", "payment": "payment",
    "currency_exchange": "currency exchange", "laundry": "a laundry service",
    "swimming_pool": "a swimming pool", "gym": "a gym", "spa": "a spa",
    "extra_bed": "an extra bed", "doctor_on_call": "a doctor on call",
    "taxi_booking": "taxi booking", "conference_room": "a conference room",
    "power_backup": "power backup", "restaurant": "the restaurant", "bar": "the bar",
    "deposit": "a deposit", "id_proof": "photo identification",
}

_PAYMENT_NAMES = {"card": "card", "cash": "cash", "upi": "UPI"}

# Spoken names for the documents `id_proof` lists. Machine keys live in the database; each language
# names them itself, exactly as payment methods do.
_ID_NAMES = {"passport": "a passport", "aadhaar": "an Aadhaar card",
             "driving_licence": "a driving licence"}

# Topics that are opening hours rather than things the hotel "offers". The generic template would
# say "Yes, we offer the bar free of charge from five in the evening", which is three kinds of
# wrong in one sentence.
_OPENING_HOURS = {"restaurant": "The restaurant", "bar": "The bar"}


def _speak_hotel_policy(result) -> str:
    """One renderer for every policy, built from the structured row rather than stored prose.

    "We do not offer that" is a real answer and reads as one; the alternative -- falling through to
    the model with no fact behind it -- is how an invented policy gets spoken.
    """
    row = result.records[0]
    name = _POLICY_NAMES.get(row["topic"], row["topic"].replace("_", " "))

    if not row["available"]:
        if row["topic"] in ("pets", "smoking"):
            return f"I am afraid {name} are not allowed anywhere in the hotel."                 if row["topic"] == "pets" else "I am afraid the hotel is entirely non-smoking."
        return f"I am sorry, we do not offer {name}."

    if row["topic"] == "payment" and row["options"]:
        methods = say_list([_PAYMENT_NAMES.get(o, o) for o in row["options"]])
        return f"We accept {methods}."
    if row["topic"] == "cancellation" and row["limit_hours"]:
        return (f"You can cancel free of charge up to {say_number(row['limit_hours'])} "
                f"hours before you arrive.")
    if row["topic"] == "smoking":
        return "Smoking is allowed only in the designated areas outside."
    # Some topics are not things a hotel "offers". "Yes, we offer children" is what a template
    # applied without thinking produces, and it is the kind of sentence that tells a caller they
    # are talking to a machine.
    if row["topic"] == "children":
        return "Yes, children are very welcome, and they stay with you at no extra charge."
    if row["topic"] == "accessibility":
        return "Yes, the hotel has step-free access."
    if row["topic"] in _OPENING_HOURS and row["hours"]:
        opens, _, closes = str(row["hours"]).partition("-")
        return (f"{_OPENING_HOURS[row['topic']]} is open from {say_time(opens)} "
                f"until {say_time(closes)}.")
    if row["topic"] == "deposit" and row["fee"]:
        return (f"We take a refundable deposit of {say_price(row['fee'])} at check-in, "
                f"returned when you leave.")
    if row["topic"] == "id_proof" and row["options"]:
        # "or", not "and": these are alternatives, and `say_list` would ask for all three.
        names = [_ID_NAMES.get(o, o.replace("_", " ")) for o in row["options"]]
        documents = ", ".join(names[:-1]) + f" or {names[-1]}" if len(names) > 1 else names[0]
        return f"Every guest needs photo identification at check-in: {documents}."

    bits = [f"Yes, we offer {name}"]
    if row["fee"]:
        bits.append(f"for {say_price(row['fee'])}")
    elif row["topic"] not in ("children", "accessibility"):
        bits.append("free of charge")
    if row["hours"]:
        hours = str(row["hours"])
        if "24" in hours:
            bits.append("around the clock")
        else:
            opens, _, closes = hours.partition("-")
            bits.append(f"from {say_time(opens)} until {say_time(closes)}")
    return " ".join(bits) + "."


def _speak_hotel_info(result) -> str:
    row = result.records[0]
    return (f"{row['name']} is in {row['address']}. We have {say_number(row['floors'])} floors "
            f"and {say_number(row['rooms'])} rooms.")


SPEAK: dict[str, Callable[..., str]] = {
    "menu_overview": _speak_menu_overview,
    "list_category": _speak_list_category,
    "price_of": _speak_price_of,
    "find_by_diet": _speak_find_by_diet,
    "check_availability": _speak_check_availability,
    "check_allergens": _speak_check_allergens,
    "safe_for": _speak_safe_for,
    "describe_item": _speak_describe_item,
    "room_status": _speak_room_status,
    "room_availability": _speak_room_availability,
    "list_room_types": _speak_list_room_types,
    "room_price": _speak_room_price,
    "room_amenities": _speak_room_amenities,
    "list_services": _speak_list_services,
    "service_hours": _speak_service_hours,
    "check_in_out": _speak_check_in_out,
    "reservation_for_room": _speak_reservation_for_room,
    "hotel_policy": _speak_hotel_policy,
    "hotel_info": _speak_hotel_info,
}

# Spoken when a tool ran but could not answer -- an unknown dish, an unknown room, an unrecognised
# category. A true "I do not have that" is a useful answer and may be spoken; only a STALE result
# may not.
NOT_FOUND = "I am sorry, I could not find that. Could you say it again?"

# Tools whose empty result is a real answer rather than a failure. Listing a category with nothing
# in it is information; finding no dish by that name is not.
_EMPTY_IS_AN_ANSWER = frozenset({
    "list_category", "find_by_diet", "safe_for", "room_availability", "menu_overview",
})


def _renderer_for(language):
    """The (templates, not-found sentence) pair for a language. English when in doubt.

    Table-driven rather than a chain of `if`, so a fourth language is one entry here plus its
    module -- and an unknown or missing language degrades to English rather than raising, because
    the caller is on a telephone and silence is the worst outcome.
    """
    code = getattr(language, "code", None)
    if code == "hin":
        from . import tools_hi

        return tools_hi.SPEAK, tools_hi.NOT_FOUND
    if code == "spa":
        from . import tools_es

        return tools_es.SPEAK, tools_es.NOT_FOUND
    return SPEAK, NOT_FOUND


def render(result, language=None) -> str | None:
    """Turn a `ToolResult` into the sentence Rime will speak, or None if it must not be spoken.

    The stale check is first and is not negotiable: a result produced for a question the caller has
    already moved on from must never become speech, no matter how good the answer was.

    `language` selects the renderer and NOTHING else. The lookup already happened: the same records
    and the same summary go to whichever set of templates speaks the caller's language, so a Hindi
    answer cannot disagree with an English one about a price. Defaults to English, and an unknown
    language falls back to English rather than raising -- the caller is on a telephone, and the
    worst acceptable outcome of a misconfiguration is being answered in the wrong language, not
    silence.
    """
    if not result.may_speak:
        return None

    speakers, not_found = _renderer_for(language)

    if result.reason or (not result.records and result.tool not in _EMPTY_IS_AN_ANSWER):
        return not_found
    speaker = speakers.get(result.tool)
    return speaker(result) if speaker is not None else None
