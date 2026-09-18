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
from functools import lru_cache
from dataclasses import dataclass

from ._foreign import _INSIDE_A_WORD, to_router_language
from ._text import is_kept as _is_kept_char
from ._text import normalise as _normalise
from .context import Subject, resolve
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
    "allerg*", "intoleran*", "avoid*", "cannot eat", "can not eat",
    "cant eat", "can t eat", "free", "without", "no", "react to", "safe",
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
# Matched as WHOLE WORDS through `_says_word`, unlike most tables here, because "night" is a
# substring of "tonight" -- and "is the chicken kebab available tonight" was therefore answered with
# "we have forty-one rooms free". A confident answer about the wrong table is the exact failure this
# router exists to avoid, and it had been sitting behind an `in` test.
#
# The inflections substring matching used to catch for free are listed explicitly now, or "we are
# staying three nights" would stop being a room question.
# Nouns that make a sentence a room question ON THEIR OWN. Say "room" or "suite" and you are
# asking about a room, whatever else is in the sentence.
_ROOM_NOUNS = ("room", "rooms", "suite", "suites", "book a room")

# Words that SUPPORT a room reading without establishing one. They are about time and duration, and
# they sit just as naturally in a menu question -- "what starters do you have tonight" is not a
# question about rooms. So these only claim a sentence that mentions no food at all.
#
# Getting this wrong is how "is the chicken kebab available tonight" came to be answered with "we
# have forty-one rooms free": "night" was matched as a substring of "tonight", and a time word was
# treated as though it were a room noun. Both halves are fixed, and both halves are needed.
_ROOM_TIME_WORDS = ("stay", "staying", "stays", "night", "nights", "tonight")

# The union, for anything that just wants to know whether rooms were mentioned at all.
_ROOM_WORDS = _ROOM_NOUNS + _ROOM_TIME_WORDS
_SERVICE_WORDS_GENERIC = ("service", "services", "facilities", "amenities")
_CHECKIN_WORDS = ("check in", "check-in", "checkin", "check out", "check-out", "checkout",
                  "checking in", "checking out", "arrival time", "departure time")
_RESERVATION_WORDS = ("reservation", "reserved", "booking", "booked", "reservations")

# The reservation NOUN, in three languages. Its own table rather than an addition to
# `_RESERVATION_WORDS`, because this one is used to decide that a sentence is a QUESTION about an
# existing booking rather than a request for a new one -- and "reserved"/"booked" are participles
# that appear in both.
_RESERVATION_NOUNS = ("reservation", "reservations", "booking", "bookings",
                      "बुकिंग", "आरक्षण", "reserva", "reservas")

# Asking AETHER to DO something rather than to tell you something. Deliberately separate from
# `_RESERVATION_WORDS`, which is about looking an existing booking up: "is there a booking on room
# two zero two" must stay a question, and "book me room two zero two" must not.
#
# EXACT VERB FORMS, not a stem, and this is the whole difficulty of the rule. `book*` matched
# "booking", so "is there a booking on room two zero two" -- a question about an existing
# reservation -- took a NEW booking on room 202. A lookup silently becoming a write is the worst
# failure available to a mutating tool, and a stem is exactly how it happened.
#
# "booking" and "reservation" are nouns and live below: they can only mean "do it" when an explicit
# intent phrase says so ("can I make a booking"), never on their own.
_BOOK_VERBS = ("book", "books", "reserve", "reserves", "reserving",
               "reservar", "reserva", "reservo", "बुक", "आरक्षित")
_BOOK_NOUNS = ("booking", "reservation", "बुकिंग", "reserva")

# The intent has to be about DOING it, not describing it. "What is your cancellation policy" and
# "can I book a room" are different sentences, and only one of them should take a booking.
# Romanised Hindi sits alongside Devanagari throughout, because BOTH reach the router: Whisper
# returns Devanagari for a Hindi utterance, but the same caller in an English-language session is
# transcribed as "mujhe do raat ke liye room book krna hai" -- which is what a real Indian hotel
# line sounds like, and which matched nothing at all until 2026-09-10.
_BOOK_INTENT = ("i want", "i would like", "i need", "can i", "could i", "please", "make",
                "for me", "get me", "set up", "arrange",
                "मुझे", "चाहिए", "कर दीजिए", "कीजिए", "करना है", "करनी है", "कर दो",
                "mujhe", "mujhko", "chahiye", "karna hai", "karni hai", "krna hai", "krni hai",
                "kar dijiye", "kar do", "karo", "kar dena", "chahiye tha",
                "quiero", "quisiera", "necesito", "puede", "me gustaria", "me gustaría")

_TABLE_WORDS = ("table", "tables", "मेज", "टेबल", "mesa", "mesas")

# "When?" in three languages. Its own table because two rules need it and because a question word
# is what separates "until when is room one zero two booked" from "book room one zero two".
_WHEN_WORDS = ("when", "till when", "until when", "how long",
               "कब", "कब तक", "कितने बजे",
               "cuando", "cuándo", "hasta cuando", "hasta cuándo", "hasta que")

# TAKING AN ORDER. "I'll have the chicken kebab" is a request to DO something; "how much is the
# chicken kebab" is a question. The two sentences share a dish and nothing else, so the verb is the
# whole distinction and these are exact forms rather than stems.
#
# `have` is deliberately absent on its own: "do you have the chicken kebab" is an availability
# question and must stay one, so only the first-person frames ("can i have", "i'll have") count.
# `normalise` turns an apostrophe into a SPACE, so "I'll have" reaches here as "i ll have". Both
# spellings are listed because both are real: the recogniser returns the apostrophe and the router
# sees it stripped, and an entry that only matched one silently matched neither in practice.
_ORDER_VERBS = ("order", "orders", "ordering", "i ll have", "ill have", "i will have",
                "i ll take", "ill take", "i will take", "can i have", "can i get",
                "could i have", "could i get", "i d like", "id like", "i would like",
                "i want", "give me", "send up", "bring me", "put me down for", "add",
                # Hindi and Spanish. `order` is what people actually say in both, so it carries.
                "मंगवा", "ऑर्डर", "भेज दीजिए", "le quiero", "quiero pedir", "pedir", "traiga")
_ORDER_NOUNS = ("order", "orders", "ऑर्डर", "pedido", "pedidos")

# TAKING SOMETHING OFF. Its own table, tested BEFORE the add rules, because every one of these
# sentences also names a dish -- and with no rule for the word, "remove paneer butter masala" was
# read as an order FOR paneer butter masala. A caller hit this for real: they said it twice, each
# time with a count, and their order went one, two, four of the dish they were removing.
#
# `no` and `without` are absent on purpose: "no onions", "without ice" are preparation notes, not
# removals, and reading them as removals would delete a dish the caller still wants.
# SPLIT PHRASAL VERBS. English puts the object in the middle -- "take the paneer butter masala
# OFF" -- so "take off" as one keyword matches "take off the paneer" and misses the commoner
# phrasing entirely. `off my order` and friends are listed whole because a bare "off" is not a
# removal: "is the fish curry off today" asks whether it is available.
_REMOVE_WORDS = ("off my order", "off the order", "off my bill", "off that order",
                 "remove", "removes", "remove the", "take off", "take out", "take away",
                 "delete", "drop the", "drop that", "get rid of", "scratch the", "scratch that",
                 "cancel the", "leave out", "leave off", "forget the", "not the", "instead of",
                 "हटा*", "निकाल*", "रद्द कर*",
                 "quit*", "quite*", "elimin*", "saca*", "sin el", "sin la", "borra*")

# "Say it back to me." A read, and it must never be confused with placing one.
# `दोहरा*` and `repit*` are STEMS, and they have to be. Hindi inflects the verb -- दोहराइए,
# दोहराओ, दोहरा दीजिए -- and a whole-word entry matched none of them, so "मेरा ऑर्डर दोहराइए"
# reached the model. Spanish has the same shape: repetir, repita, repítame.
_REPEAT_WORDS = ("repeat", "read back", "read it back", "read me back", "say it back",
                 "what did i", "did i order", "what have i", "what is on", "what s on",
                 "whats on",
                 "so far", "go over", "run through", "run over", "check my", "confirm my",
                 "दोहरा*", "बता*", "repit*", "repít*", "repás*", "repas*")
# "Send it." A write, and the end of the order.
_PLACE_WORDS = ("place", "send it", "send that", "send them", "submit", "go ahead",
                "that is all", "that s all", "thats all", "that is everything",
                "that s everything", "thats everything", "put it through", "send it through",
                "confirm the order", "confirm that order",
                "भेज दीजिए", "envíe", "envie", "mande")

# "What did I book?" -- about THIS call, not about a room number the caller never gave.
_MY_BOOKING_WORDS = ("my booking", "my bookings", "my reservation", "my reservations",
                     "my table", "my room", "my reference", "booking reference",
                     "reference number", "what did i book", "what have i booked",
                     "मेरी बुकिंग", "मेरा कमरा", "मेरी टेबल", "मेरी बुकिंग का नंबर",
                     "बुकिंग नंबर", "बुकिंग का नंबर",
                     "mi reserva", "mi mesa", "mi habitación", "mi habitacion",
                     # "cual es mi numero de reserva" -- the possessive and the noun are two words
                     # apart in Spanish, so "mi reserva" never matched the commonest phrasing.
                     "numero de reserva", "número de reserva", "mi número", "mi numero")

_CANCEL_WORDS = ("cancel*", "रद्द", "cancelar*", "anular*")
_STATUS_WORDS = ("free", "available", "vacant", "occupied", "empty", "taken", "ready",
                 # Spanish and Hindi say the STATE with their own words, and `_foreign.py`
                 # translates nouns rather than adjectives -- so "hasta cuando está reservada la
                 # habitación uno cero dos" matched no status word and was answered "está ocupada",
                 # which is true and is not when it frees up.
                 "reservada", "reservado", "ocupada", "ocupado", "libre", "disponible",
                 "खाली", "बुक है", "बुक हैं", "भरा", "उपलब्ध")
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
# `off today` and `off the menu` are here, not in `_REMOVE_WORDS`: "is the fish curry off today?"
# asks whether a dish is available. Mid-order, with no availability cue, a bare dish name is read
# as another item -- so without this the question would have ORDERED the dish it was asking about.
_AVAILABLE_WORDS = ("available", "do you still have", "in stock", "sold out", "on today",
                    "off today", "off the menu", "run out", "ran out")
# `allerg*` and `contain*` are STEMS: allergy/allergic/allergen, contains/containing. Everything
# else is a whole word, so "nuts" cannot fire on "doughnuts".
_ALLERGEN_WORDS = ("allerg*", "contain*", "nuts", "dairy", "gluten", "shellfish", "eggs", "lactose")
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


# Text preparation lives in `_text` so `clarify` can use exactly the same rule. Re-exported here
# because the whole suite imports `normalise` from the router, and because this is where a reader
# looking for "what happens to the transcript first" will come.
normalise = _normalise
_is_kept = _is_kept_char


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


def _find_dishes(spoken: str) -> list[str]:
    """EVERY menu name in the sentence, in the order the caller said them.

    `_find_dish` returns one, which is right for "how much is the chicken kebab" and wrong for an
    order: a caller asked for "biryani naan and paneer butter masala" and had one dish added. They
    were not told the other two were missed -- the order was simply shorter than what they said.

    Longest-first and then blanked out, so "paneer butter masala" is matched whole and cannot then
    also match "paneer tikka" through the word they share. Anything not on the menu -- "naan" -- is
    invisible here by design; the renderer reads the whole order back, which is where a caller
    notices something they asked for is missing.
    """
    left, found = spoken, []
    for name in _DISH_NAMES:                      # already sorted longest-first
        at = left.find(name)
        if at != -1:
            found.append((at, name))
            left = left[:at] + (" " * len(name)) + left[at + len(name):]
    for word, name in _DISH_SHORTHAND:
        match = re.search(rf"\b{re.escape(word)}\b", left)
        if match and name.lower() not in [n for _pos, n in found]:
            found.append((match.start(), name.lower()))
            left = left[:match.start()] + (" " * len(word)) + left[match.end():]
    return [name for _position, name in sorted(found)]


def _cue_at(spoken: str, entries: tuple[str, ...]) -> list[int]:
    """Where each of these keywords appears, in order."""
    found = []
    for entry in entries:
        for match in _compile(entry).finditer(spoken):
            found.append(match.start())
    return sorted(found)


def _split_remove_and_add(spoken: str, dishes: list[str]) -> tuple[str | None, list[str]]:
    """"Remove X and add Y" -- which dish goes off, and which come on.

    A SWAP is one sentence and two intentions, and it is how people actually correct an order:
    "can you remove the paneer butter masala and add a butter chicken". Handling only the removal
    leaves the caller to ask again for something they already asked for.

    Each dish belongs to whichever cue -- a removal word or an order verb -- sits closest BEFORE it.
    Position, not keyword order, because "add a butter chicken instead of the paneer" reverses them.
    """
    removes = _cue_at(spoken, _REMOVE_WORDS)
    adds = _cue_at(spoken, _ORDER_VERBS)
    going, coming = None, []
    for name in dishes:
        at = spoken.find(name)
        if at == -1:
            continue
        last_remove = max([p for p in removes if p < at], default=None)
        last_add = max([p for p in adds if p < at], default=None)
        if last_remove is not None and (last_add is None or last_remove > last_add):
            if going is None:
                going = name
        elif last_add is not None:
            coming.append(name)
    return going, coming


def _find_category(spoken: str) -> Category | None:
    for word, category in _CATEGORY_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", spoken):
            return category
    return None


@lru_cache(maxsize=None)
def _compile(entry: str) -> re.Pattern[str]:
    """One keyword-table entry as a pattern that says what it means.

    THE BUG THIS CLOSES. Every table here used to be matched with plain `in`, which is substring
    matching, and substring matching inside a sentence is a hazard that only shows up when the
    right word finally arrives. It did: "night" is inside "tonight", so "is the chicken kebab
    available tonight" was answered with "we have forty-one rooms free". The same trap was sitting
    unexploded in several other tables -- "any" is inside "company", "no " is inside "casino ",
    "like" is inside "unlikely".

    Fixing it by making everything a whole word would have broken the tables that MEAN a prefix:
    "allerg" is there to catch allergy, allergic, allergen and allergies at once. So intent is now
    written down per entry instead of assumed table-wide:

        "nuts"       whole word   -- matches "nuts", not "doughnuts"
        "allerg*"    stem         -- matches allergy/allergic/allergen, but only at a word start
        "how much"   phrase       -- whole words at both ends

    A trailing `*` is the only special character, and no keyword in this file legitimately contains
    one.

    THE BOUNDARY KNOWS ABOUT DEVANAGARI, and `\\w` alone does not -- a matra is a combining mark for
    which `str.isalnum()` is False, so `(?!\\w)` reports a word boundary in the middle of a Hindi
    word. `बुक` ("book") therefore matched inside `बुकिंग` ("booking"), and "कमरा तीन शून्य पाँच पर
    कोई बुकिंग है क्या" -- *is there a booking on room 305* -- was read as an instruction to book it.
    The exact-form/stem distinction above exists precisely to keep a noun from firing a verb, and in
    Hindi it was not holding. `_foreign.py` carries the same fix for the same reason.
    """
    if entry.endswith("*"):
        return re.compile(rf"(?<!{_INSIDE_A_WORD}){re.escape(entry[:-1])}{_INSIDE_A_WORD}*")
    return re.compile(
        rf"(?<!{_INSIDE_A_WORD}){re.escape(entry.strip())}(?!{_INSIDE_A_WORD})")


def _says(spoken: str, entries: tuple[str, ...]) -> bool:
    """Whether the sentence contains any of these keywords, each matched as it declares."""
    return any(_compile(entry).search(spoken) for entry in entries)


# Kept as a distinct name because `_ROOM_WORDS` is the table whose substring match caused a real
# wrong answer, and the call site reads better for saying so.
_says_word = _says


def _find_pair(spoken: str, table: tuple[tuple[str, str], ...]) -> str | None:
    for word, value in table:
        if re.search(rf"\b{re.escape(word)}\b", spoken):
            return value
    return None


def _find_policy(spoken: str) -> str | None:
    """The policy this sentence asks about -- or None when it names more than one.

    `_find_pair` returns the LONGEST matching keyword, which is the right rule for overlapping
    phrases ("late check out" must beat "check out") and the wrong rule when a sentence genuinely
    names two different topics. "Is the gym open to children?" matched both `gym` and `children`,
    `children` won on length alone, and the caller was told that children stay free -- a confident
    answer to a question nobody asked.

    Naming two topics is exactly the ambiguity this router refuses to guess at elsewhere (a dish
    shorthand claimed by two dishes is left alone for the same reason). So it declines, and the
    model answers -- which it can now do from the hotel's own facts, and does better: asked about
    the gym and children it replies "children are welcome in the gym when accompanied by an adult",
    addressing both instead of one.

    Several keywords for the SAME topic are not ambiguity: "wifi", "wi-fi" and "internet" all mean
    `wifi`, and that sentence is answered.
    """
    found: list[str] = []
    for word, topic in _POLICY_WORDS:
        if topic not in found and re.search(rf"\b{re.escape(word)}\b", spoken):
            found.append(topic)
            if len(found) > 1:
                return None
    return found[0] if found else None


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


# Words for small numbers, so "a table for four" carries a party size. Only up to twelve, which is
# the largest table the restaurant has -- a number beyond that is not a party size and should not
# be read as one.
_SMALL_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5, "chhe": 6, "saat": 7, "aath": 8,
    # `पांच` is FIVE. It read 6 until 2026-09-18 -- a copy-paste from the छह line beside it -- so
    # "पांच लोगों के लिए टेबल" booked a table for six. A wrong party size is not a wrong answer that
    # gets corrected next turn; it is a table laid for the wrong number of people.
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पाँच": 5, "पांच": 5, "छह": 6, "सात": 7, "आठ": 8,
    "नौ": 9, "दस": 10,
    "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10,
}


def _find_party_size(spoken: str) -> int | None:
    r""""a table for four", "table for 4", "चार लोगों के लिए" -> 4.

    `\S+` rather than `\w+` for the number token. `\w` is `str.isalnum()` plus underscore, and a
    Devanagari matra is a combining mark for which that is False -- so `\w+` matched only "द" of
    "दो" and every Hindi count silently failed. The same fact that broke `normalise`, twice.

    Requires the "for" frame (or its equivalent) rather than picking up any number in the sentence:
    "book me room three zero five for two nights" contains "two", and reading that as a party size
    would turn a room booking into a table for two.
    """
    # THE EXPLICIT FRAME FIRST, because it is the specific one. Hindi puts the number BEFORE the
    # phrase -- "चार लोगों के लिए", "char logon ke liye" -- and running the "for" rule ahead of it
    # read the wrong number out of the same sentence: "चार लोगों के लिए आठ बजे टेबल" matched
    # "के लिए" followed by "आठ" and booked a table for EIGHT people at eight o'clock. A wrong party
    # size is not corrected on the next turn; it is a table laid for the wrong number of people.
    match = re.search(
        r"(\S+)\s+(?:लोगों|लोग|जनों|logon|log|logo|jano|jane|admi|aadmi|people|persons|person)",
        spoken)
    if match and match.group(1) in _SMALL_NUMBERS:
        return _SMALL_NUMBERS[match.group(1)]
    # The number must not be a DURATION. "book me a deluxe king for two nights" matched the "for"
    # frame and turned a two-night room booking into a table for two -- the caller asked to sleep
    # somewhere and was offered dinner. Nor a TIME: "के लिए आठ बजे" is the hour, not the party.
    match = re.search(
        r"(?:for|के लिए|ke liye|para)\s+(\d{1,2}|\S+)(?!\s*(?:night|nights|day|days|week|weeks"
        r"|रात|रातों|दिन|noche|noches|dia|dias|día|días"
        r"|बजे|baje|o'clock|oclock))",
        spoken)
    if match:
        token = match.group(1)
        if token.isdigit() and 1 <= int(token) <= 12:
            return int(token)
        if token in _SMALL_NUMBERS:
            return _SMALL_NUMBERS[token]
    return None


def _find_quantity(spoken: str, dish: str | None) -> int:
    """"two chicken kebabs" -> 2. Defaults to one, which is what a caller who says no number means.

    The number must sit immediately BEFORE the dish. Reading any number in the sentence would turn
    "the chicken kebab, and send it to room two zero five" into an order for 205 kebabs -- and a
    quantity is the one parameter here where being wrong is expensive rather than merely unhelpful.
    """
    if not dish:
        return 1
    match = re.search(rf"(\d{{1,2}}|\S+)\s+(?:{re.escape(dish)})", spoken)
    if not match:
        return 1
    token = match.group(1)
    if token.isdigit():
        return int(token) if 1 <= int(token) <= 20 else 1
    return _SMALL_NUMBERS.get(token, 1)


def _find_nights(spoken: str) -> int | None:
    """"for two nights", "three nights", "2 raat" -> the number of nights.

    The mirror image of `_find_party_size`, and it has to be: the same "for two" can mean a party
    or a stay, and the noun after it is the only thing that says which. One of the two rules has to
    read that noun, so both do.
    """
    match = re.search(
        r"(\d{1,2}|\S+)\s*(?:night|nights|रात|रातों|raat|raaton|noche|noches)", spoken)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit() and 1 <= int(token) <= 30:
        return int(token)
    return _SMALL_NUMBERS.get(token)


# Digits as they are actually said down a telephone, in all three languages. ONE table, because a
# reference and a room number are both read out digit by digit and they were drifting apart: the
# Hindi and Spanish digits existed for neither until 2026-09-18, and the consequences were not
# symmetrical. A room number that failed to parse gave a wrong answer; a REFERENCE that failed to
# parse turned "बुकिंग एक शून्य शून्य चार रद्द कीजिए" -- cancel booking one zero zero four -- into a
# new room booking, because the cancel rule needs a reference and the booking rule does not.
_SPOKEN_DIGITS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "शून्य": "0", "सुन्य": "0", "एक": "1", "दो": "2", "तीन": "3", "चार": "4",
    "पाँच": "5", "पांच": "5", "छह": "6", "छः": "6", "सात": "7", "आठ": "8", "नौ": "9",
    "cero": "0", "uno": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4",
    "cinco": "5", "seis": "6", "siete": "7", "ocho": "8", "nueve": "9",
}


def _find_reference(spoken: str) -> str | None:
    """A booking reference: four digits, spoken or written."""
    digits = re.search(r"(?<!\w)(\d{3,5})(?!\w)", spoken)
    if digits:
        return digits.group(1)
    words = spoken.split()
    run: list[str] = []
    for word in words:
        value = _SPOKEN_DIGITS.get(word)
        if value is None:
            if len(run) >= 3:
                break
            run = []
        else:
            run.append(value)
    return "".join(run) if len(run) >= 3 else None


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
    # "three oh five", "three zero five" -- how a number is actually said on an English phone line,
    # and digit by digit is how it is said on a Hindi or Spanish one too.
    #
    # THE HINDI AND SPANISH DIGITS WERE MISSING, and the failure was not a polite one. "hasta cuando
    # esta reservada la habitacion uno cero dos" found no room number, fell past every room rule,
    # and the conversational-subject memory resolved the sentence against the last dish mentioned --
    # so a caller asking when a room frees up was told the price of a Masala Chai. A confident
    # answer to a question nobody asked, in the caller's own language.
    digits = _SPOKEN_DIGITS
    words = spoken.split()
    for i in range(len(words) - 2):
        trio = [digits.get(w) for w in words[i:i + 3]]
        if not all(trio):
            continue
        # A trio that is part of a LONGER run of digits is not a room number. "Booking one zero
        # zero nine" is four digits; reading the first three of them gave room "100" -- a different
        # room, a different guest, and a confident answer about neither.
        before = digits.get(words[i - 1]) if i else None
        after = digits.get(words[i + 3]) if i + 3 < len(words) else None
        if before is not None or after is not None:
            continue
        return "".join(trio)
    # And as a CARDINAL, which is how Hindi and Spanish say a room number: "एक सौ एक",
    # "ciento uno". Longest first, so "ciento uno" is not matched as "ciento" inside a longer form.
    for spoken_form, value in _CARDINAL_ROOM_NUMBERS:
        if spoken_form in spoken:
            return value
    return None


def names_something(spoken: str) -> bool:
    """Whether the sentence already says what it is about.

    The gate on conversational memory: if the caller named a dish, a room, a category, a room type,
    a service or a policy, that is the subject and nothing remembered may override it. Memory fills
    a gap; it never argues with the sentence in front of it.
    """
    return any((
        _find_dish(spoken) is not None,
        _find_category(spoken) is not None,
        _find_room_number(spoken) is not None,
        _find_pair(spoken, _ROOM_TYPE_WORDS) is not None,
        _find_pair(spoken, _SERVICE_WORDS) is not None,
        _find_pair(spoken, _POLICY_WORDS) is not None,
    ))


def subject_of(decision: "Route | None", spoken: str) -> tuple[str | None, str | None]:
    """What this turn was about, as (phrase, kind), for the next turn to refer back to.

    Read from the DECISION rather than from the sentence, so the remembered subject is the thing
    AETHER actually looked up -- not something the caller mentioned in passing and was not answered
    about.
    """
    if decision is None:
        return None, None
    params = decision.params
    if "dish" in params:
        return params["dish"], "dish"
    if "room" in params:
        return f"room {params['room']}", "room"
    if params.get("room_type"):
        return str(params["room_type"]).lower(), "room_type"
    if "category" in params:
        return params["category"], "category"
    if "topic" in params:
        return str(params["topic"]).replace("_", " "), "policy"
    return None, None


def route(text: str, subject: Subject | None = None, *, ordering: bool = False) -> Route | None:
    """Pick a menu tool for this sentence, or None to let the LLM handle it.

    Order is by specificity, not by frequency: a sentence naming a dish AND asking about allergens
    must go to the allergen tool, not to the price tool, so the narrower rules are tested first.

    `ordering` says whether this caller has an order open, and it changes exactly one thing: a bare
    dish name. "Chicken kebab" means *how much is it* to someone browsing and *add it* to someone
    mid-order, and nothing in the sentence can tell the two apart -- the caller who has just
    ordered a kebab and says "and two masala chai" is not asking the price. Passed in rather than
    read here, because routing stays a pure function of what was said plus what the session knows.
    """
    # The caller's own words first, then the ASR repairs, then one tested set of English rules.
    # A Hindi or Spanish caller used to match nothing here and fall through to the model -- which
    # answered in the right language, which is why it went unnoticed, and which meant hotel facts
    # reached a language model for every non-English caller. See `_foreign.py`.
    spoken = _repair_asr(to_router_language(normalise(text)))
    if not spoken:
        return None

    # Conversational memory, applied ONLY into a gap: a sentence that names its own subject is
    # never overridden, and a sentence with no referring word is left alone to fall through to the
    # model. See `context.py` for why this is the smallest version that fixes the real complaint.
    if subject is not None and not names_something(spoken):
        spoken = _repair_asr(resolve(spoken, subject))

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
    policy = _find_policy(spoken)
    if policy is not None:
        return Route("hotel_policy", {"topic": policy}, "policy")

    # A0. TAKING A BOOKING. Before every read rule, because "book me a deluxe king" also contains
    #     a room type and a price word and would otherwise be answered with the rate -- which is a
    #     particularly annoying way to fail, since the caller has to ask twice.
    #
    #     The bar is deliberately high: a booking word AND either an explicit intent phrase or a
    #     party size. "What is your cancellation policy" and "is there a booking on room two zero
    #     two" both survive it, and both must.
    # Whether the sentence is about food. Computed here because BOTH the booking rules and the room
    # rules need it: "what type of dishes are available on the table" contains "table" and would
    # otherwise be answered with restaurant availability, and "is the chicken kebab available
    # tonight" contains a time word and would otherwise be answered with rooms.
    mentions_food = (dish is not None or category is not None
                     or _find_pair(spoken, _MENU_NOUNS) is not None)

    party = _find_party_size(spoken)
    nights = _find_nights(spoken)
    if _says(spoken, _CANCEL_WORDS) and (reference := _find_reference(spoken)):
        return Route("cancel_booking", {"reference": reference}, "cancel booking")

    # A0a. THE ORDER, before the booking rules and before every menu rule.
    #
    #      Before the menu rules because "I'll have the chicken kebab" names a dish and would
    #      otherwise be answered with its price -- a caller who has just ordered being quoted a
    #      number instead. Before the booking rules because "I want to order a table for four"
    #      is still a table, and `_ORDER_VERBS` shares "i want" with `_BOOK_INTENT`, so the order
    #      rules below all require an order NOUN or a dish and none of them fire on a table.
    #
    #      Reading the order back is first within the block: "repeat my order" contains "order",
    #      and an order rule that tested for the noun alone would place it.
    says_order = _says(spoken, _ORDER_NOUNS)
    says_remove = _says(spoken, _REMOVE_WORDS)
    dishes = _find_dishes(spoken)

    #  TAKING SOMETHING OFF COMES FIRST, before every add rule and before cancelling the lot.
    #
    #  Reported by a real caller. "Can you remove paneer butter masala and add chicken butter?"
    #  named a dish, matched no removal rule because there was none, and was read as an ORDER for
    #  paneer butter masala. They said it twice more, each time with a count, and watched the order
    #  go one, two, four of the dish they were trying to get rid of. A removal that adds is worse
    #  than no removal at all: the caller is actively correcting it and every attempt makes it
    #  worse.
    if says_remove and dish is not None and not _says(spoken, _PRICE_WORDS):
        going, coming = _split_remove_and_add(spoken, dishes)
        going = going or dish
        #  "Remove the TWO paneer butter masala" -- the count says how many to take off, and it was
        #  being read as how many to add. That is what turned each correction into a doubling.
        count = _find_quantity(spoken, going)
        params = {"dish": going, "quantity": count if count > 1 else None}
        #  A SWAP is one sentence and two intentions. Doing only the removal leaves the caller to
        #  ask again for the dish they just asked for.
        if coming:
            params["then_add"] = [{"dish": name, "quantity": _find_quantity(spoken, name)}
                                  for name in coming]
        return Route("remove_from_order", params,
                     "swap on order" if coming else "remove from order")

    #  `not dishes`: "cancel the chicken kebab from my order" wiped the WHOLE order. Naming a dish
    #  makes it a removal, which the rule above already took.
    if says_order and _says(spoken, _CANCEL_WORDS) and not _find_reference(spoken) and not dishes:
        return Route("cancel_order", {}, "cancel order")
    if says_order and _says(spoken, _REPEAT_WORDS):
        return Route("repeat_order", {}, "repeat order")
    if says_order and _says(spoken, _PLACE_WORDS):
        return Route("place_order", {}, "place order")
    # "that's all" and "send it through" carry no noun at all, and are how people actually finish.
    if _says(spoken, _PLACE_WORDS) and not mentions_food and not _says(spoken, _ROOM_NOUNS):
        return Route("place_order", {}, "place order")
    if dish is not None and not _says(spoken, _PRICE_WORDS) and not _says(spoken, _ALLERGEN_WORDS):
        # Mid-order, a dish name on its own is another item. "And two masala chai" carries no verb
        # at all, and answering it with a price is how a waiter who is not listening behaves.
        # Off-order it still needs an explicit order verb, so browsing the menu is unaffected.
        #
        # EVERY dish the caller named, not just the longest match. "I need biryani naan and paneer
        # butter masala" added the paneer and silently dropped the biryani; the caller asked for
        # three things and was read back one.
        adding = None
        if _says(spoken, _ORDER_VERBS):
            adding = "add to order"
        elif ordering and not _says(spoken, _DESCRIBE_WORDS + _AVAILABLE_WORDS + _LIST_WORDS):
            adding = "add to open order"
        if adding and len(dishes) > 1:
            return Route("add_to_order",
                         {"dishes": [{"dish": name, "quantity": _find_quantity(spoken, name)}
                                     for name in dishes]}, adding)
        if adding:
            return Route("add_to_order",
                         {"dish": dish, "quantity": _find_quantity(spoken, dish)}, adding)

    # A REFERENCE THE CALLER ACTUALLY GAVE beats anything the session remembers, and beats reading
    # its first three digits as a room number.
    #
    # Reported: "can you check my room booking status with the reference ID one zero zero nine" was
    # answered "you have not made a booking on this call yet" -- while the room it had booked was
    # still correctly reserved. And "what is the status of booking one zero zero nine" found "100"
    # inside the reference and answered about ROOM one zero zero, which is a different room, a
    # different guest, and a confident answer to a question nobody asked.
    #
    # Placed before the room rules so the digits are read as what the caller called them: a
    # reference. `_find_reference` wants three to five digits, so a bare room number cannot be
    # mistaken for one -- and a reservation word has to be present, so "room one zero zero nine"
    # is still a room.
    # `not room_number`: "is there a booking on room three zero five" names a ROOM, and its three
    # digits are a room number that `_find_reference` would happily read as a reference. A sentence
    # that says "room" is asking about that room.
    if (_says(spoken, _RESERVATION_NOUNS + _RESERVATION_WORDS) and not room_number
            and not _says(spoken, _CANCEL_WORDS) and not _says(spoken, _BOOK_VERBS)):
        if reference := _find_reference(spoken):
            return Route("booking_status", {"reference": reference}, "booking by reference")

    # A0a. ASKING WHETHER A NAMED ROOM IS BOOKED, before the booking rules can take it as an order.
    #
    #      English separates the verb from the noun -- `_BOOK_VERBS` holds exact forms so that
    #      "booking" cannot fire "book" -- and neither Hindi nor Spanish gives that for free.
    #      `reserva` IS the Spanish booking verb, and `बुक` matches inside `बुकिंग` because a
    #      Devanagari matra is not a `\w` character, so the whole-word guard does not bite. Both
    #      "कमरा तीन शून्य पाँच पर कोई बुकिंग है क्या" and "hay alguna reserva en la habitación tres
    #      cero cinco" -- *is there a booking on room 305* -- went to `reserve_room` and tried to
    #      take one.
    #
    #      A reservation NOUN plus a room number is a question about an existing booking, unless the
    #      caller also said to do something ("बुकिंग कीजिए"), which is what `_BOOK_INTENT` is for.
    #      This is English rule B moved earlier and made multilingual; the English behaviour is
    #      unchanged, because an English booking request contains no reservation noun.
    if (room_number and _says(spoken, _RESERVATION_NOUNS)
            and not _says(spoken, _BOOK_INTENT)):
        return Route("reservation_for_room", {"room": room_number}, "room+reservation")

    # A0b. ASKING ABOUT A BOOKING IS NOT MAKING ONE, and this rule exists because the Hindi path
    #      got that wrong in the worst possible direction. "कमरा एक शून्य दो कब तक बुक है" -- "until
    #      when is room one zero two booked" -- contains बुक, which is in `_BOOK_VERBS`, so it was
    #      read as *book room 102* and went to `reserve_room`. It happened to be refused because
    #      that room is occupied; on a free room it would have taken a booking the caller never
    #      asked for. A lookup silently becoming a write is the exact trap `_BOOK_VERBS` was made
    #      exact-form to avoid, and Hindi has no separate word for the noun to key off.
    #
    #      A question word plus a room number is a question. Placed before the booking rules so it
    #      wins, and narrow enough that "book me room two zero one" is untouched -- that sentence
    #      asks nothing.
    # `_RESERVATION_WORDS` as well as the status words: "how long is room three zero five booked
    # for" asks when, names a room, and uses neither "free" nor "check out".
    asking_when = _says(spoken, _WHEN_WORDS)
    if asking_when and room_number and _says(
            spoken, _STATUS_WORDS + _CHECKIN_WORDS + _RESERVATION_WORDS):
        return Route("room_free_from", {"room": room_number}, "room free from")

    # A0c. WHAT THIS CALL HAS BOOKED. Before check-in/out and before the table rules: "when is my
    #      table booked for" contains a table word, and "when do I check out" after a booking is
    #      about that booking rather than about the hotel's general times.
    if _says(spoken, _MY_BOOKING_WORDS) and not room_number:
        # "Cancel my reservation" carries no reference, and a reference is normally required --
        # cancelling the wrong booking is not recoverable by saying sorry. It is safe here for one
        # reason only: "my" means the booking made on THIS call, and there is exactly one of it.
        # The tool asks for a reference as before when this call has booked nothing.
        if _says(spoken, _CANCEL_WORDS):
            return Route("cancel_my_booking", {}, "cancel my booking")
        return Route("my_booking", {}, "my booking")

    wants_to_book = _says(spoken, _BOOK_VERBS) or (
        _says(spoken, _BOOK_NOUNS) and _says(spoken, _BOOK_INTENT)
    ) or (
        # Hindi asks for a room without any word for "book": "kamra chahiye do raat ke liye" is
        # a booking, and "मुझे कमरा चाहिए" is how it is actually said. Allowed ONLY with a stay or
        # a party size attached, because "room ka rate chahiye" is a price question and wanting
        # something is not, on its own, asking to have it reserved. A mutating tool gets the most
        # conservative rule in the file.
        _says(spoken, _BOOK_INTENT) and (nights or party) and (room_type or room_number
                                                               or _says(spoken, _ROOM_NOUNS)
                                                               or _says(spoken, _TABLE_WORDS))
    )
    # A table word counts as intent on its own: "reserve a table for twenty" carries no "can I"
    # and no recognisable party size, and falling through to the model there means the caller is
    # never told the largest table seats twelve.
    if wants_to_book and (_says(spoken, _BOOK_INTENT) or party or room_type or room_number
                          or _says(spoken, _TABLE_WORDS)):
        # A party size only means a TABLE when no room has been named. "a room for four" is a
        # room; "a table for four" is a table; "for four" alone is a table, which is what people
        # mean when they say it.
        if _says(spoken, _TABLE_WORDS) or (party and not room_type and not room_number):
            return Route("reserve_table", {"party_size": party or 2}, "book a table")
        stay = {"nights": nights} if nights else {}
        if room_number:
            return Route("reserve_room", {"room": room_number, **stay}, "book a named room")
        if room_type:
            return Route("reserve_room", {"room_type": room_type, **stay}, "book a room type")
        return Route("reserve_room", dict(stay), "book any room")

    # A0b. Asking whether a TABLE is free, which the rooms rules would otherwise answer about beds.
    #      `not mentions_food`: "what type of dishes are available on the table" is a menu question
    #      that happens to contain the word "table", and answering it with restaurant availability
    #      is a confident answer to a question nobody asked.
    if (_says(spoken, _TABLE_WORDS) and not mentions_food
            and _says(spoken, _STATUS_WORDS + _LIST_WORDS)):
        return Route("table_availability", {}, "table availability")

    # A1. The hotel itself: where it is, how big it is, how to reach it.
    #
    #     `not _says(_STATUS_WORDS)` because "how many rooms" is in this table and "how many rooms
    #     are free" is not a question about the building. It was answered "we have five floors and
    #     fifty rooms" -- a fact, confidently given, to a caller who asked a different one.
    if _says(spoken, _HOTEL_INFO_WORDS) and not _says(spoken, _STATUS_WORDS):
        return Route("hotel_info", {}, "hotel info")

    # A2. WHEN a named room frees up, before the check-in/out rule and before room status.
    #
    #     Before check-in/out because "when does the guest in three zero five check out" contains
    #     "check out" and was being answered with the hotel's general checkout time -- a confident
    #     answer about the wrong thing, to someone asking about one specific room.
    #
    #     Before `room_status` because "when will room two zero one be available" was answered
    #     "room two zero one is already reserved": true, and not the question. A caller asking
    #     *when* wants a date, and the reservation holds one.
    if room_number and asking_when and _says(
            spoken, _STATUS_WORDS + _CHECKIN_WORDS + _RESERVATION_WORDS):
        return Route("room_free_from", {"room": room_number}, "room free from")

    # A. Check-in and check-out times. Read from the hotel row, so the model never guesses them.
    if _says(spoken, _CHECKIN_WORDS):
        return Route("check_in_out", {}, "check-in/out")

    # B. A reservation on a named room.
    if room_number and _says(spoken, _RESERVATION_WORDS):
        return Route("reservation_for_room", {"room": room_number}, "room+reservation")

    # C. The status of a named room: "is room three oh five free".
    if room_number:
        return Route("room_status", {"room": room_number}, "room number")

    # D. A named service: "when is room service available".
    if service:
        return Route("service_hours", {"service": service}, "service")

    # E. What a room type costs, or what comes with it.
    if room_type:
        if _says(spoken, ("amenities", "come with", "comes with", "include",
                          "included", "facilities", "what is in")):
            return Route("room_amenities", {"room_type": room_type}, "room type+amenities")
        if _says(spoken, _PRICE_WORDS) or _says(spoken, ("rate", "rates")):
            return Route("room_price", {"room_type": room_type}, "room type+price")
        if _says(spoken, _STATUS_WORDS):
            return Route("room_availability", {"room_type": room_type}, "room type+availability")
        return Route("room_price", {"room_type": room_type}, "room type only")

    # F. Rooms in general: "do you have anything free tonight", "what rooms do you have".
    #
    #    "room for" is excluded because there "room" is uncountable and means SPACE. On a real call
    #    "do you have room service" was heard as "Do you have room for this?", and this rule
    #    answered it with the list of room types -- a confident answer to a question nobody asked.
    #    Falling through costs a slower answer from a model that has the whole hotel in its prompt;
    #    answering the wrong question costs the claim.
    # A room noun claims the sentence outright. A time word only claims one that mentions no food:
    # "do you have anything free tonight" is a room question, "what starters do you have tonight"
    # is not, and neither is "is the chicken kebab available tonight".
    asks_about_rooms = _says(spoken, _ROOM_NOUNS) or (
        _says(spoken, _ROOM_TIME_WORDS) and not mentions_food
    )
    if asks_about_rooms and "room for" not in spoken:
        if _says(spoken, _STATUS_WORDS):
            return Route("room_availability", {}, "rooms+availability")
        if _says(spoken, _LIST_WORDS) or _says(spoken, _PRICE_WORDS):
            return Route("list_room_types", {}, "room types")

    # G. Services in general: "what services do you offer".
    if (_says(spoken, _SERVICE_WORDS_GENERIC)
            and _says(spoken, _LIST_WORDS)):
        return Route("list_services", {}, "services")

    # ---- the menu rules ---------------------------------------------------------------------

    # 1. Allergens about a named dish. First because it is the answer that matters most to get
    #    right, and because "does X contain nuts" also contains price-ish and list-ish words.
    if dish and _says(spoken, _ALLERGEN_WORDS):
        return Route("check_allergens", {"dish": dish}, "dish+allergen")

    # 2. Availability of a named dish.
    if dish and _says(spoken, _AVAILABLE_WORDS):
        return Route("check_availability", {"dish": dish}, "dish+availability")

    # 3. Price of a named dish. BEFORE the spice rule, because an explicit price question is the
    #    more specific signal: "how much is the hot chicken kebab" asks for a number, and the
    #    stray "hot" must not turn it into an answer about heat. Getting this order wrong was a
    #    real regression -- every price question containing a spice word answered the wrong
    #    question.
    if dish and _says(spoken, _PRICE_WORDS):
        return Route("price_of", {"dish": dish}, "dish+price")

    # 4. "Is the chicken kebab spicy?" / "tell me about the paneer tikka".
    #
    #    THE DATABASE RECORDS NO SPICE LEVEL -- only a prose description. So this reads the hotel's
    #    own description back rather than inventing a heat rating. Still before the bare-dish
    #    fallback, because a question about what a dish IS must not be answered with its price.
    if dish and (spice is not None or _says(spoken, _DESCRIBE_WORDS)):
        return Route("describe_item", {"dish": dish}, "dish+describe")

    # 5. A dish named with no other signal -- treat as "tell me about it", which is its price.
    if dish:
        return Route("price_of", {"dish": dish}, "dish only")

    # 6. An allergy with no dish named: "I have a nut allergy, what can I eat?". Requires BOTH an
    #    allergen and an avoidance cue, so "do you have any fish" stays a menu browse rather than
    #    becoming a medical question.
    if allergen and _says(spoken, _AVOIDANCE_WORDS):
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
    if category is not None and _says(spoken, _LIST_WORDS):
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
        and _says(spoken, _LIST_WORDS)
        and not _says(spoken, _ALLERGEN_WORDS)
    ):
        return Route("menu_overview", {}, "general menu")

    # A BARE NOUN, said on its own. "Menu." / "The menu, please." A caller who says only the thing
    # they want is asking for it -- and on a real call on 2026-09-10 the recogniser received exactly
    # one word, "menu", and AETHER asked the caller to repeat themselves. Only when the WHOLE
    # utterance is the noun (give or take "the" and "please"), so this can never capture a sentence
    # the rules above deliberately let through.
    bare = [w for w in spoken.split() if w not in ("the", "please", "a", "your", "our")]
    if bare in (["menu"], ["menus"]):
        return Route("menu_overview", {}, "bare menu")
    if bare in (["rooms"], ["room", "types"]):
        return Route("list_room_types", {}, "bare rooms")

    # Nothing confident. The LLM takes it -- a slower answer beats a wrong tool.
    return None
