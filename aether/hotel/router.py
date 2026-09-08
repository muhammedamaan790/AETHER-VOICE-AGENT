"""Deterministic routing: which menu tool answers this sentence, if any.

Keyword-and-slot matching, not a model. Three reasons, in order of importance:

1. **Latency.** A menu fact via Gemini measured ~1.8 s of LLM time on top of STT and TTS. A tool
   lookup is under 5 ms. On a phone call that is the difference between a conversation and a wait.
2. **Correctness.** Prices and allergens are facts. A model asked to phrase `{"price": 380}` can
   still say the wrong number, and an allergen it invents could genuinely hurt somebody.
3. **Determinism.** The demo must answer the same way every rehearsal.

The router is deliberately conservative: it answers only when it is confident, and returns `None`
otherwise so the LLM handles the sentence. A wrong tool call is worse than a slower answer, so
anything ambiguous falls through rather than guessing.

It does **not** run the tool. It returns a name and arguments; `ToolRunner` executes them with the
usual fence check, delay and identity stamping. Routing and execution stay separate so the router
can never bypass fencing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .db import HotelStore

# The store the router reads its vocabulary from. Dish names, categories and room types all come
# from the database, so adding a dish or a room type needs no code change here. Built once at
# import: the router is a pure function of the sentence and the hotel's shape, and the shape does
# not change while the process runs (the database is read-only).
_STORE = HotelStore()

# Longest-first, so "main course" wins over "main" and "vegetarian mains" over "mains".
_CATEGORY_WORDS: tuple[tuple[str, str], ...] = tuple(sorted(
    [(c, c) for c in _STORE.categories()] + [
        ("starter", "starters"), ("appetisers", "starters"), ("appetizers", "starters"),
        ("appetiser", "starters"), ("appetizer", "starters"),
        ("main course", "mains"), ("main courses", "mains"), ("main", "mains"),
        ("entree", "mains"),
        ("vegetarian main", "vegetarian mains"), ("veg mains", "vegetarian mains"),
        ("dessert", "desserts"),
        # STT spellings, not English. `base.en` transcribed a caller's "dessert" as "Desert" on a
        # real call; the router missed, the model answered, and it invented three dishes and three
        # prices that do not exist. A missing letter must not be able to do that.
        ("deserts", "desserts"), ("desert", "desserts"),
        ("sweets", "desserts"), ("sweet", "desserts"),
        ("pudding", "desserts"), ("puddings", "desserts"),
        ("drink", "drinks"), ("beverages", "drinks"), ("beverage", "drinks"),
    ],
    key=lambda pair: -len(pair[0]),
))

# Room types, from the database, longest-first so "standard twin" beats "standard".
_ROOM_TYPE_WORDS: tuple[tuple[str, str], ...] = tuple(sorted(
    [(t.name.lower(), t.name) for t in _STORE.room_types()] + [
        ("suite", "Executive Suite"), ("suites", "Executive Suite"),
        ("family room", "Family Suite"), ("twin", "Standard Twin"),
    ],
    key=lambda pair: -len(pair[0]),
))

# Hotel services, from the database.
_SERVICE_WORDS: tuple[tuple[str, str], ...] = tuple(sorted(
    [(s.name.lower(), s.name) for s in _STORE.services()] + [
        ("wake up call", "Wake-up Call"), ("wake up", "Wake-up Call"),
        ("reception", "Front Desk"), ("luggage", "Luggage Assistance"),
        ("bags", "Luggage Assistance"), ("cleaning", "Housekeeping"),
    ],
    key=lambda pair: -len(pair[0]),
))

_DIET_WORDS: tuple[tuple[str, str], ...] = (
    ("non vegetarian", "non-vegetarian"), ("non-vegetarian", "non-vegetarian"),
    ("nonveg", "non-vegetarian"), ("non veg", "non-vegetarian"),
    ("vegan", "vegan"), ("plant based", "vegan"),
    ("vegetarian", "vegetarian"), ("veggie", "vegetarian"), ("veg", "vegetarian"),
)

_SPICE_WORDS: tuple[tuple[str, str], ...] = (
    ("not spicy", "none"), ("no spice", "none"), ("mild", "mild"),
    ("very spicy", "hot"), ("extra spicy", "hot"), ("spicy", "hot"), ("hot", "hot"),
    ("medium", "medium"),
)

# Allergen names a caller says, mapped to the fixture's canonical labels. Longest-first so
# "tree nut" beats "nut". Kept in step with `tools._ALLERGEN_ALIASES` -- the router names the
# allergen, the tool validates it, and a word only this table knows would raise there.
_ALLERGEN_NAMES: tuple[tuple[str, str], ...] = tuple(sorted(
    (
        ("tree nut", "nuts"), ("tree nuts", "nuts"), ("peanuts", "nuts"), ("peanut", "nuts"),
        ("nuts", "nuts"), ("nut", "nuts"),
        ("lactose", "dairy"), ("dairy", "dairy"), ("milk", "dairy"), ("cheese", "dairy"),
        ("gluten", "gluten"), ("wheat", "gluten"),
        ("shellfish", "shellfish"), ("prawns", "shellfish"), ("prawn", "shellfish"),
        ("shrimp", "shellfish"), ("crab", "shellfish"),
        ("seafood", "fish"), ("fish", "fish"),
        ("eggs", "eggs"), ("egg", "eggs"),
    ),
    key=lambda pair: -len(pair[0]),
))

# An allergen word alone is not an allergy question -- "do you have fish" is a menu browse. One of
# these cues must be present too, so the narrower `safe_for` tool only fires when the caller has
# actually said they are avoiding something.
_AVOIDANCE_WORDS = (
    "allerg", "intolerant", "intolerance", "avoid", "avoiding", "cannot eat", "can not eat",
    "cant eat", "can t eat", "free", "without", "no ", "react to", "safe",
)

# Words that make a sentence a question about FOOD IN GENERAL rather than about anything specific.
# Reused through `_find_pair`, so each is matched as a whole word: "dish" must not fire on
# "dishwasher", and "eat" must not fire on "theatre".
#
# Deliberately nouns only, and deliberately paired with a list cue below. On its own each of these
# appears in plenty of sentences that are not menu questions -- "is the food good", "where is the
# food court", "can I order a taxi" -- and none of those should be answered from the menu.
_MENU_NOUNS: tuple[tuple[str, str], ...] = (
    ("menu", "menu"), ("menus", "menu"),
    ("dishes", "dish"), ("dish", "dish"),
    ("food", "food"), ("foods", "food"),
    ("eat", "eat"), ("cuisine", "cuisine"), ("cuisines", "cuisine"),
    ("meal", "meal"), ("meals", "meal"),
    ("dining", "dining"), ("serve", "serve"), ("serving", "serve"),
    ("order", "order"),
)

# Rooms, services and timings. Each needs its own noun, so a sentence about the menu can never be
# answered with a room rate.
_ROOM_WORDS = ("room", "rooms", "suite", "suites", "stay", "night", "nights", "book a room")
_SERVICE_WORDS_GENERIC = ("service", "services", "facilities", "amenities")
_CHECKIN_WORDS = ("check in", "check-in", "checkin", "check out", "check-out", "checkout",
                  "checking in", "checking out", "arrival time", "departure time")
_RESERVATION_WORDS = ("reservation", "reserved", "booking", "booked", "reservations")
_STATUS_WORDS = ("free", "available", "vacant", "occupied", "empty", "taken", "ready")
_DESCRIBE_WORDS = ("tell me about", "what is the", "what is in", "describe",
                   "what comes with", "like")

_PRICE_WORDS = ("how much", "price of", "price for", "cost of", "what does", "how expensive")
_AVAILABLE_WORDS = ("available", "do you still have", "in stock", "sold out", "on today")
_ALLERGEN_WORDS = ("allerg", "contain", "nuts", "dairy", "gluten", "shellfish", "eggs", "lactose")
# Ways a caller asks to be told what there is. Broadened after a real call: "tell me all the
# possible things available in dessert" matched NONE of the original six, so a plain menu question
# reached the model. Each entry is a phrase people actually used on the calls, not a guess.
_LIST_WORDS = (
    "what", "which", "list", "any", "do you have", "have you got", "got any",
    "tell me", "show me", "give me", "read me", "talk me through",
    "options", "available", "everything", "all the", "kind of", "type of", "types of",
)

# Dish names longest-first, so "paneer butter masala" is matched before "paneer tikka" could
# ambiguously grab "paneer".
_DISH_NAMES: tuple[str, ...] = tuple(sorted((i.name.lower() for i in _STORE.menu()),
                                            key=len, reverse=True))


@dataclass(frozen=True)
class Route:
    """A tool to run and the arguments to run it with. Never the result."""

    tool: str
    params: dict[str, object]
    reason: str          # which rule matched, recorded in the trace for auditability


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. STT output is not tidy."""
    return " ".join(re.sub(r"[^\w\s-]", " ", text.lower()).split())


def _find_dish(spoken: str) -> str | None:
    """The longest menu name contained in the sentence, or None.

    Longest-first matters: "paneer butter masala" must not be answered as "paneer tikka" because
    both contain "paneer".
    """
    for name in _DISH_NAMES:
        if name in spoken:
            return name
    return None


def _find_category(spoken: str) -> Category | None:
    for word, category in _CATEGORY_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", spoken):
            return category
    return None


def _find_pair(spoken: str, table: tuple[tuple[str, str], ...]) -> str | None:
    for word, value in table:
        if re.search(rf"\b{re.escape(word)}\b", spoken):
            return value
    return None


def _find_allergen(spoken: str) -> str | None:
    for word, canonical in _ALLERGEN_NAMES:
        if re.search(rf"\b{re.escape(word)}\b", spoken):
            return canonical
    return None


def _find_room_number(spoken: str) -> str | None:
    """Any room number the caller said, spoken or spelled. Existence is NOT checked here.

    Deliberately so. A number the hotel does not have must still reach `room_status`, where the
    tool raises `UnknownRecord` and AETHER says it cannot find that room. Filtering unknown numbers
    out here instead sent "is room four one two free" -- a room this hotel does not have -- down to
    the general availability rule, which cheerfully answered "forty-one rooms are free". Answering
    a question about a room that does not exist is worse than admitting it does not.
    """
    for token in re.findall(r"\b\d{3}\b", spoken):
        return token
    # "three oh five", "three zero five" -- how a number is actually said on a phone.
    digits = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
              "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
    words = spoken.split()
    for i in range(len(words) - 2):
        trio = [digits.get(w) for w in words[i:i + 3]]
        if all(trio):
            return "".join(trio)
    return None


def route(text: str) -> Route | None:
    """Pick a menu tool for this sentence, or None to let the LLM handle it.

    Order is by specificity, not by frequency: a sentence naming a dish AND asking about allergens
    must go to the allergen tool, not to the price tool, so the narrower rules are tested first.
    """
    spoken = normalise(text)
    if not spoken:
        return None

    dish = _find_dish(spoken)
    category = _find_category(spoken)
    diet = _find_pair(spoken, _DIET_WORDS)
    spice = _find_pair(spoken, _SPICE_WORDS)
    allergen = _find_allergen(spoken)

    room_number = _find_room_number(spoken)
    room_type = _find_pair(spoken, _ROOM_TYPE_WORDS)
    service = _find_pair(spoken, _SERVICE_WORDS)

    # ---- rooms, services and timings, before the menu rules --------------------------------
    #
    # First because their nouns are unambiguous. "How much is an executive suite" contains a price
    # word and would otherwise be looked up as a dish; a sentence naming a room type is asking
    # about a room.

    # A. Check-in and check-out times. Read from the hotel row, so the model never guesses them.
    if any(word in spoken for word in _CHECKIN_WORDS):
        return Route("check_in_out", {}, "check-in/out")

    # B. A reservation on a named room.
    if room_number and any(word in spoken for word in _RESERVATION_WORDS):
        return Route("reservation_for_room", {"room": room_number}, "room+reservation")

    # C. The status of a named room: "is room three oh five free".
    if room_number:
        return Route("room_status", {"room": room_number}, "room number")

    # D. A named service: "when is room service available".
    if service:
        return Route("service_hours", {"service": service}, "service")

    # E. What a room type costs, or what comes with it.
    if room_type:
        if any(word in spoken for word in ("amenities", "come with", "comes with", "include",
                                           "included", "facilities", "what is in")):
            return Route("room_amenities", {"room_type": room_type}, "room type+amenities")
        if any(word in spoken for word in _PRICE_WORDS) or "rate" in spoken:
            return Route("room_price", {"room_type": room_type}, "room type+price")
        if any(word in spoken for word in _STATUS_WORDS):
            return Route("room_availability", {"room_type": room_type}, "room type+availability")
        return Route("room_price", {"room_type": room_type}, "room type only")

    # F. Rooms in general: "do you have anything free tonight", "what rooms do you have".
    if any(word in spoken for word in _ROOM_WORDS):
        if any(word in spoken for word in _STATUS_WORDS):
            return Route("room_availability", {}, "rooms+availability")
        if any(word in spoken for word in _LIST_WORDS) or any(
                word in spoken for word in _PRICE_WORDS):
            return Route("list_room_types", {}, "room types")

    # G. Services in general: "what services do you offer".
    if (any(word in spoken for word in _SERVICE_WORDS_GENERIC)
            and any(word in spoken for word in _LIST_WORDS)):
        return Route("list_services", {}, "services")

    # ---- the menu rules ---------------------------------------------------------------------

    # 1. Allergens about a named dish. First because it is the answer that matters most to get
    #    right, and because "does X contain nuts" also contains price-ish and list-ish words.
    if dish and any(word in spoken for word in _ALLERGEN_WORDS):
        return Route("check_allergens", {"dish": dish}, "dish+allergen")

    # 2. Availability of a named dish.
    if dish and any(word in spoken for word in _AVAILABLE_WORDS):
        return Route("check_availability", {"dish": dish}, "dish+availability")

    # 3. Price of a named dish. BEFORE the spice rule, because an explicit price question is the
    #    more specific signal: "how much is the hot chicken kebab" asks for a number, and the
    #    stray "hot" must not turn it into an answer about heat. Getting this order wrong was a
    #    real regression -- every price question containing a spice word answered the wrong
    #    question.
    if dish and any(word in spoken for word in _PRICE_WORDS):
        return Route("price_of", {"dish": dish}, "dish+price")

    # 4. "Is the chicken kebab spicy?" / "tell me about the paneer tikka".
    #
    #    THE DATABASE RECORDS NO SPICE LEVEL -- only a prose description. So this reads the hotel's
    #    own description back rather than inventing a heat rating. Still before the bare-dish
    #    fallback, because a question about what a dish IS must not be answered with its price.
    if dish and (spice is not None or any(w in spoken for w in _DESCRIBE_WORDS)):
        return Route("describe_item", {"dish": dish}, "dish+describe")

    # 5. A dish named with no other signal -- treat as "tell me about it", which is its price.
    if dish:
        return Route("price_of", {"dish": dish}, "dish only")

    # 6. An allergy with no dish named: "I have a nut allergy, what can I eat?". Requires BOTH an
    #    allergen and an avoidance cue, so "do you have any fish" stays a menu browse rather than
    #    becoming a medical question.
    if allergen and any(word in spoken for word in _AVOIDANCE_WORDS):
        params: dict[str, object] = {"allergen": allergen}
        if category is not None:
            params["category"] = category
        return Route("safe_for", params, "allergen avoidance")

    # 7. Dietary request, optionally narrowed to a category.
    if diet:
        params = {"diet": diet}
        if category is not None:
            params["category"] = category
        return Route("find_by_diet", params, "diet")

    # (There is no "find everything mild" rule. The database records no spice level, and filtering
    #  a menu by a column that does not exist is not something to fake -- such a question falls
    #  through to the model, which is told the menu and told never to invent.)

    # 9. A whole category.
    if category is not None and any(word in spoken for word in _LIST_WORDS):
        return Route("list_category", {"category": category}, "category")

    # 10. The broadest menu question, and usually the FIRST one a caller asks: "what's on the
    #     menu", "what type of dishes are available", "what can I order". Answered from the
    #     fixture's shape -- which courses exist, which diets are catered for.
    #
    #     LAST, so every narrower rule above still wins: "what starters do you have" is a category
    #     question, not a general one.
    #
    #     Conservative by construction: it needs BOTH a food noun and a list cue. "is the food
    #     good", "where is the food court" and "can I order a taxi" all carry a food noun with no
    #     list cue and correctly fall through to the model, and "can I book a table for eight"
    #     carries neither.
    #
    #     Left to the model on purpose: anything mentioning an allergy. A vague allergy question
    #     needs the caller to name the allergen, and answering it with a list of courses would be
    #     a confident non-answer to the one question where that is dangerous.
    if (
        _find_pair(spoken, _MENU_NOUNS)
        and any(word in spoken for word in _LIST_WORDS)
        and not any(word in spoken for word in _ALLERGEN_WORDS)
    ):
        return Route("menu_overview", {}, "general menu")

    # Nothing confident. The LLM takes it -- a slower answer beats a wrong tool.
    return None
