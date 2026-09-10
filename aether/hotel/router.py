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
import unicodedata
from dataclasses import dataclass

from ._foreign import to_router_language
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

# Policy questions, keyed to the `hotel_policies` topics. These are the commonest things a hotel
# line is asked and the ones this router could not answer at all until the database grew a
# `hotel_policies` table -- "do you have parking" reached the model with no fact behind it.
#
# Longest-first, so "late check out" is not eaten by the check-in/check-out rule and "early check
# in" is not either.
_POLICY_WORDS: tuple[tuple[str, str], ...] = tuple(sorted(
    [
        ("early check in", "early_check_in"), ("early check-in", "early_check_in"),
        ("early checkin", "early_check_in"), ("check in early", "early_check_in"),
        ("late check out", "late_check_out"), ("late check-out", "late_check_out"),
        ("late checkout", "late_check_out"), ("check out late", "late_check_out"),
        ("airport transfer", "airport_transfer"), ("airport pickup", "airport_transfer"),
        ("pick me up", "airport_transfer"), ("airport", "airport_transfer"),
        ("luggage storage", "luggage_storage"), ("store my luggage", "luggage_storage"),
        ("keep my bags", "luggage_storage"), ("left luggage", "luggage_storage"),
        ("currency exchange", "currency_exchange"), ("exchange money", "currency_exchange"),
        ("wheelchair", "accessibility"), ("step free", "accessibility"),
        ("accessible", "accessibility"), ("disabled access", "accessibility"),
        ("cancellation", "cancellation"), ("cancel my booking", "cancellation"),
        ("cancel the booking", "cancellation"), ("cancellation policy", "cancellation"),
        ("payment", "payment"), ("pay by card", "payment"), ("credit card", "payment"),
        ("debit card", "payment"), ("how can i pay", "payment"), ("upi", "payment"),
        ("parking", "parking"), ("car park", "parking"), ("park my car", "parking"),
        ("wifi", "wifi"), ("wi-fi", "wifi"), ("wi fi", "wifi"), ("internet", "wifi"),
        ("breakfast", "breakfast"),
        ("pets", "pets"), ("pet", "pets"), ("my dog", "pets"), ("my cat", "pets"),
        ("smoking", "smoking"), ("smoke", "smoking"),
        ("children", "children"), ("kids", "children"), ("my child", "children"),
        ("laundry", "laundry"), ("wash my clothes", "laundry"),
        # --- added 2026-09-10 ------------------------------------------------------------
        ("swimming pool", "swimming_pool"), ("pool", "swimming_pool"),
        ("swim", "swimming_pool"), ("swimming", "swimming_pool"),
        ("gym", "gym"), ("fitness centre", "gym"), ("fitness center", "gym"),
        ("work out", "gym"), ("workout", "gym"),
        ("spa", "spa"), ("massage", "spa"),
        ("extra bed", "extra_bed"), ("additional bed", "extra_bed"), ("extra mattress",
                                                                     "extra_bed"),
        ("doctor", "doctor_on_call"), ("a doctor", "doctor_on_call"),
        ("medical help", "doctor_on_call"), ("if i fall ill", "doctor_on_call"),
        ("taxi", "taxi_booking"), ("book a cab", "taxi_booking"), ("a cab", "taxi_booking"),
        ("conference room", "conference_room"), ("meeting room", "conference_room"),
        ("banquet", "conference_room"), ("event space", "conference_room"),
        ("power backup", "power_backup"), ("power cut", "power_backup"),
        ("generator", "power_backup"),
        # DELIBERATELY NOT the bare words "restaurant" or "dinner". Policy words are matched
        # BEFORE menu routing, so a bare "restaurant" would swallow "what is on the restaurant
        # menu" and answer it with opening hours. Each of these carries its own time cue.
        ("restaurant open", "restaurant"), ("restaurant close", "restaurant"),
        ("restaurant hours", "restaurant"), ("restaurant timing", "restaurant"),
        ("restaurant timings", "restaurant"), ("is the restaurant open", "restaurant"),
        ("when does the restaurant", "restaurant"),
        ("bar", "bar"), ("cocktail", "bar"), ("the bar open", "bar"),
        ("deposit", "deposit"), ("security deposit", "deposit"),
        ("id proof", "id_proof"), ("identity proof", "id_proof"), ("photo id", "id_proof"),
        ("passport", "id_proof"), ("aadhaar", "id_proof"), ("aadhar", "id_proof"),
        ("what documents", "id_proof"), ("which documents", "id_proof"),
    ],
    key=lambda pair: -len(pair[0]),
))

# Questions about the hotel itself rather than about a room or a dish.
_HOTEL_INFO_WORDS = ("where are you", "where is the hotel", "your address", "hotel address",
                     "how many floors", "how many rooms", "what is your number",
                     "your phone number", "contact number", "how do i reach you",
                     "where are you located", "location of the hotel")

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

# Words that mean something else in a hotel, and must never be read as a menu item however
# unambiguous they look on the menu. "Is there hot water?" is a question about plumbing, and
# answering it with the price of a bottle of Mineral Water would be confidently wrong.
_NOT_SHORTHAND = frozenset({"water"})


def _unambiguous_shorthand(names: list[str]) -> dict[str, str]:
    """Trailing word -> full name, but ONLY where that word names exactly one thing.

    Nobody says "how much is the chocolate brownie" on a phone; they say "the brownie". Every one
    of the twelve dish shorthands used to miss, and "How much is the kebab?" is in a real trace
    (traces/, 2026-09-08) falling through to the model.

    The ambiguity rule is the one `HotelStore.find_item` already applies: an ambiguous match raises
    rather than guesses. So "chicken" is excluded (Butter Chicken AND Chicken Kebab), "masala" is
    excluded (Paneer Butter Masala AND Masala Chai), and "suite" is excluded (Executive AND Family).
    Guessing between two dishes is exactly the confident wrong answer this router exists to avoid.
    """
    candidates = {name.lower().split()[-1]: name for name in names}
    shorthand: dict[str, str] = {}
    for word, name in candidates.items():
        if word in _NOT_SHORTHAND:
            continue
        # Ambiguous if the word appears ANYWHERE in another name, not merely as another trailing
        # word. "chicken" trails only "Butter Chicken", but it also opens "Chicken Kebab" -- and a
        # caller saying "the chicken" has named neither one of them.
        owners = [n for n in names if word in n.lower().split()]
        if len(owners) == 1:
            shorthand[word] = name
    return shorthand


_DISH_SHORTHAND: tuple[tuple[str, str], ...] = tuple(sorted(
    _unambiguous_shorthand([i.name for i in _STORE.menu()]).items(),
    key=lambda pair: -len(pair[0]),
))

# Recogniser output, repaired before matching. EVERY entry is a mishearing observed in a real
# trace; none is invented, and the trace is named beside it. A guessed homophone table would be a
# second router with no evidence behind it.
#
# "suite" is the one that bites, because it is pronounced "sweet" -- `base.en` returns "suit" or
# "sweet" and the room-type match then misses entirely. In `run-20260908T154108Z` the caller asked
# "what comes with an executive suite?", it was heard as "executive suit", and the turn fell
# through to Gemini: 1360 ms of model time on a question the database answers for free.
#
# Deliberately anchored to a preceding room-type word. A bare "sweet" must keep meaning DESSERTS
# (`_CATEGORY_WORDS` maps it, and "what sweets do you have" is a real menu question), so only
# "executive sweet" and "family suit" are repaired -- never "sweet" on its own.
_ASR_REPAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(executive|family|deluxe|standard)\s+(?:suit|suits|sweet|sweets)\b"),
     r"\1 suite"),
)


def _repair_asr(spoken: str) -> str:
    """Undo known recogniser errors. Normalised text in, normalised text out."""
    for pattern, replacement in _ASR_REPAIRS:
        spoken = pattern.sub(replacement, spoken)
    return spoken


@dataclass(frozen=True)
class Route:
    """A tool to run and the arguments to run it with. Never the result."""

    tool: str
    params: dict[str, object]
    reason: str          # which rule matched, recorded in the trace for auditability


def _is_kept(ch: str) -> bool:
    r"""Whether `normalise` keeps this character.

    COMBINING MARKS ARE KEPT, and that is the whole reason this is a function rather than the
    one-line `[^\w\s-]` it used to be. Python's `\w` is `str.isalnum()` plus underscore, and a
    Devanagari vowel sign is category Mn/Mc, for which `isalnum()` is False. So the old expression
    deleted every matra and virama: "मेन्यू में क्या है" normalised to "म न य म क य ह", and no
    Hindi keyword could ever match. Hindi routing was impossible, not merely unimplemented.

    Tested by category rather than by codepoint range so the next script -- Tamil, Arabic, Thai --
    works without another edit here.
    """
    return (ch.isalnum() or ch.isspace() or ch in "-_"
            or unicodedata.category(ch).startswith("M"))


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. STT output is not tidy."""
    return " ".join("".join(ch if _is_kept(ch) else " " for ch in text.lower()).split())


def _find_dish(spoken: str) -> str | None:
    """The longest menu name contained in the sentence, or None.

    Longest-first matters: "paneer butter masala" must not be answered as "paneer tikka" because
    both contain "paneer".
    """
    for name in _DISH_NAMES:
        if name in spoken:
            return name
    # Then the shorthand a caller actually uses -- "the kebab", "the brownie". Whole words only,
    # so "chai" cannot be found inside another word.
    for word, name in _DISH_SHORTHAND:
        if re.search(rf"\b{re.escape(word)}\b", spoken):
            return name.lower()
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


def _build_cardinal_room_numbers() -> tuple[tuple[str, str], ...]:
    """Every three-digit number as Hindi and Spanish actually say it, longest form first.

    GENERATED FROM THE RENDERERS, not written out. `say_number` is what AETHER uses to SPEAK a
    room number in each language, so deriving the input forms from the same function means the
    router recognises exactly what the agent says back -- a caller can repeat what they just heard
    and be understood. A hand-written table would be free to drift from it.

    100-999 rather than only the fifty rooms that exist: `_find_room_number` deliberately lets an
    unknown number through so the tool can say "I could not find that room". Recognising only real
    rooms would send "क्या कमरा चार सौ बारह खाली है" to the general availability rule, which would
    answer "forty-one rooms are free" -- confidently, about a room this hotel does not have.

    English is not included. English says a room number digit by digit, and the trio scan above
    already covers it; adding "one hundred and one" here would make "we have one hundred and one
    rooms" look like a room number.
    """
    from .speech_es import say_number as say_es
    from .speech_hi import say_number as say_hi

    forms: list[tuple[str, str]] = []
    for value in range(100, 1000):
        digits = str(value)
        for say in (say_hi, say_es):
            try:
                forms.append((say(value), digits))
            except (ValueError, KeyError, IndexError):
                continue          # a language that cannot say this number simply contributes none
    return tuple(sorted(set(forms), key=lambda pair: -len(pair[0])))


_CARDINAL_ROOM_NUMBERS = _build_cardinal_room_numbers()


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
    # "three oh five", "three zero five" -- how a number is actually said on an English phone line.
    digits = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
              "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
    words = spoken.split()
    for i in range(len(words) - 2):
        trio = [digits.get(w) for w in words[i:i + 3]]
        if all(trio):
            return "".join(trio)
    # And as a CARDINAL, which is how Hindi and Spanish say a room number: "एक सौ एक",
    # "ciento uno". Longest first, so "ciento uno" is not matched as "ciento" inside a longer form.
    for spoken_form, value in _CARDINAL_ROOM_NUMBERS:
        if spoken_form in spoken:
            return value
    return None


def route(text: str) -> Route | None:
    """Pick a menu tool for this sentence, or None to let the LLM handle it.

    Order is by specificity, not by frequency: a sentence naming a dish AND asking about allergens
    must go to the allergen tool, not to the price tool, so the narrower rules are tested first.
    """
    # The caller's own words first, then the ASR repairs, then one tested set of English rules.
    # A Hindi or Spanish caller used to match nothing here and fall through to the model -- which
    # answered in the right language, which is why it went unnoticed, and which meant hotel facts
    # reached a language model for every non-English caller. See `_foreign.py`.
    spoken = _repair_asr(to_router_language(normalise(text)))
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

    # A0. A named policy. BEFORE check-in/out, because "late check out" and "early check in"
    #     contain the check-in words and are different questions with different answers -- one is a
    #     time, the other is whether it is possible and what it costs.
    policy = _find_pair(spoken, _POLICY_WORDS)
    if policy is not None:
        return Route("hotel_policy", {"topic": policy}, "policy")

    # A1. The hotel itself: where it is, how big it is, how to reach it.
    if any(phrase in spoken for phrase in _HOTEL_INFO_WORDS):
        return Route("hotel_info", {}, "hotel info")

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
    #
    #    "room for" is excluded because there "room" is uncountable and means SPACE. On a real call
    #    "do you have room service" was heard as "Do you have room for this?", and this rule
    #    answered it with the list of room types -- a confident answer to a question nobody asked.
    #    Falling through costs a slower answer from a model that has the whole hotel in its prompt;
    #    answering the wrong question costs the claim.
    if any(word in spoken for word in _ROOM_WORDS) and "room for" not in spoken:
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
