"""What a Hindi or Spanish caller says, mapped onto the words the router already matches.

THE DEFECT THIS FIXES. AETHER *answered* in three languages from the first day it spoke them, but it
only *understood* one. `route()` matches English keywords, so a caller saying "मेन्यू में क्या है"
or "¿qué hay en el menú?" matched nothing and fell through to the model. The answer that came back
was still in the right language -- which is exactly why nobody noticed -- but it came from Gemini
rather than from `data/aether_hotel.db`, so the central claim of this project ("a hotel fact never
reaches a language model") was quietly false for every non-English caller. RULES.md R8b.6 says an
English-only hotel capability is a defect; this was one, hiding behind a correct-sounding answer.

WHY A TRANSLATION TABLE AND NOT A SECOND ROUTER. Three routers would be three sets of ordering
rules, three sets of precedence bugs, and three places to add the next tool. Every routing decision
AETHER makes -- longest-match-first, rooms before menu, policy words before categories, the
suit/sweet repair -- is tested once, in English, and is worth keeping exactly once. So this layer
only rewrites *vocabulary*: it turns the caller's words into the English words the existing tables
already key on, and then the one tested router runs.

WHAT IT IS NOT. Not translation, and not a language detector. It rewrites known keywords and leaves
everything else alone, so it degrades to "no match" rather than to a wrong match -- and a no-match
still reaches the model, which is the correct place for a question the database cannot answer. It is
applied to every utterance regardless of the session language, because a caller who has switched to
Hindi still says "Chicken Kebab" in English and a caller in English may still say "kitna".

Dish and room-type names are deliberately absent: they are proper nouns on the hotel's own menu and
are spoken the same way in all three languages (see `tools_hi.py`, which keeps them as printed).
"""

from __future__ import annotations

import re

# (what the caller might say, the English the router keys on).
#
# Ordered longest-first at import so a phrase is never eaten by one of its own words: "कितने का"
# must win over "का", and "sala de conferencias" over "sala".
#
# Romanised Hindi sits alongside Devanagari because both reach the router in practice -- Whisper
# returns Devanagari for a Hindi utterance, but an English-language session transcribes the same
# caller as "kitne ka hai", and people code-switch mid-sentence on an Indian hotel line.
_PAIRS: list[tuple[str, str]] = [
    # --- question and list cues -------------------------------------------------------------
    ("क्या क्या", "what"), ("क्या", "what"), ("कौन से", "which"), ("कौनसे", "which"),
    ("कौन सी", "which"), ("बताइए", "tell me"), ("बताओ", "tell me"), ("बता दीजिए", "tell me"),
    ("मुझे चाहिए", "i want"), ("दिखाइए", "show me"),
    ("kya kya", "what"), ("kya", "what"), ("kaun se", "which"), ("bataiye", "tell me"),
    ("batao", "tell me"),
    ("cuáles", "which"), ("cuales", "which"), ("cuál", "which"), ("cual", "which"),
    ("qué", "what"), ("que tienen", "what do you have"), ("tienen", "do you have"),
    ("tiene", "do you have"), ("hay", "do you have"), ("dime", "tell me"),
    ("me puede decir", "tell me"), ("quisiera", "i want"), ("quiero", "i want"),

    # --- price -------------------------------------------------------------------------------
    ("कितने का है", "how much"), ("कितने का", "how much"), ("कितना है", "how much"),
    ("कितने की है", "how much"), ("कितने की", "how much"), ("कीमत", "price of"),
    ("दाम", "price of"), ("रेट", "price of"),
    ("kitne ka hai", "how much"), ("kitne ka", "how much"), ("kitna hai", "how much"),
    ("kitne ki", "how much"), ("keemat", "price of"), ("daam", "price of"),
    ("cuánto cuesta", "how much"), ("cuanto cuesta", "how much"), ("cuánto vale", "how much"),
    ("cuanto vale", "how much"), ("precio de", "price of"), ("precio", "price of"),

    # --- the menu ----------------------------------------------------------------------------
    ("मेन्यू", "menu"), ("मेनू", "menu"), ("खाने में", "food"), ("खाना", "food"),
    ("व्यंजन", "dish"), ("डिश", "dish"),
    ("la carta", "menu"), ("carta", "menu"), ("menú", "menu"), ("comida", "food"),
    ("platos", "dish"), ("plato", "dish"),

    # --- categories --------------------------------------------------------------------------
    ("स्टार्टर", "starters"), ("शुरुआत में", "starters"),
    ("मेन कोर्स", "mains"), ("मुख्य", "mains"),
    ("मिठाई", "desserts"), ("डेज़र्ट", "desserts"), ("डेजर्ट", "desserts"),
    ("पीने", "drinks"), ("ड्रिंक", "drinks"), ("पेय", "drinks"),
    ("starter", "starters"), ("mithai", "desserts"),
    ("entrantes", "starters"), ("entrante", "starters"), ("aperitivos", "starters"),
    ("platos principales", "mains"), ("principales", "mains"),
    ("postres", "desserts"), ("postre", "desserts"),
    ("bebidas", "drinks"), ("bebida", "drinks"),

    # --- diet --------------------------------------------------------------------------------
    ("शाकाहारी", "vegetarian"), ("मांसाहारी", "non vegetarian"), ("वीगन", "vegan"),
    ("shakahari", "vegetarian"), ("mansahari", "non vegetarian"),
    ("vegetariano", "vegetarian"), ("vegetariana", "vegetarian"),
    ("vegetarianas", "vegetarian"), ("vegetarianos", "vegetarian"),
    ("vegano", "vegan"), ("vegana", "vegan"),

    # --- allergens. The one place a wrong match could actually hurt somebody, so the cues are
    # --- explicit rather than clever.
    ("एलर्जी", "allergy"), ("मुझे एलर्जी है", "i am allergic"),
    ("नट्स", "nuts"), ("मेवा", "nuts"), ("दूध", "dairy"), ("ग्लूटेन", "gluten"),
    ("allergy hai", "allergy"), ("alergia", "allergy"), ("alérgico", "allergic"),
    ("alergico", "allergic"), ("frutos secos", "nuts"), ("nueces", "nuts"),
    ("lácteos", "dairy"), ("lacteos", "dairy"),

    # --- rooms -------------------------------------------------------------------------------
    ("कमरे", "rooms"), ("कमरा", "room"), ("सुइट", "suite"), ("स्वीट", "suite"),
    ("रात के", "night"), ("ठहरने", "stay"),
    ("kamre", "rooms"), ("kamra", "room"),
    ("habitaciones", "rooms"), ("habitación", "room"), ("habitacion", "room"),
    ("suites", "suites"), ("noche", "night"),

    # --- availability and status ---------------------------------------------------------------
    ("उपलब्ध है", "available"), ("उपलब्ध", "available"), ("मिलेगा", "available"),
    ("खाली है", "free"), ("खाली", "free"), ("मिल सकता है", "available"),
    ("upalabdh", "available"), ("khali", "free"),
    ("disponible", "available"), ("disponibles", "available"), ("libre", "free"),
    ("libres", "free"),

    # --- check-in and check-out ----------------------------------------------------------------
    ("चेक इन", "check in"), ("चेक-इन", "check in"), ("चेक आउट", "check out"),
    ("चेक-आउट", "check out"),
    ("entrada", "check in"), ("salida", "check out"),
    ("hora de entrada", "check in"), ("hora de salida", "check out"),

    # --- services ------------------------------------------------------------------------------
    ("रूम सर्विस", "room service"), ("सेवाएं", "services"), ("सेवाएँ", "services"),
    ("सुविधाएं", "facilities"), ("सुविधाएँ", "facilities"),
    ("servicio de habitaciones", "room service"), ("servicios", "services"),
    ("instalaciones", "facilities"),

    # --- policies. Only the topics `hotel_policies` actually holds ------------------------------
    ("स्विमिंग पूल", "swimming pool"), ("तैराकी", "swimming pool"), ("पूल", "pool"),
    ("जिम", "gym"), ("स्पा", "spa"), ("पार्किंग", "parking"), ("वाई-फाई", "wifi"),
    ("वाईफाई", "wifi"), ("इंटरनेट", "internet"), ("नाश्ता", "breakfast"),
    ("लॉन्ड्री", "laundry"), ("टैक्सी", "taxi"), ("डॉक्टर", "doctor"),
    ("पालतू", "pets"), ("धूम्रपान", "smoking"), ("बच्चे", "children"),
    ("अतिरिक्त बिस्तर", "extra bed"), ("जमा राशि", "deposit"), ("पहचान पत्र", "photo id"),
    ("रेस्टोरेंट कब खुलता है", "restaurant open"), ("बार", "bar"),
    ("piscina", "swimming pool"), ("gimnasio", "gym"), ("spa", "spa"),
    ("aparcamiento", "parking"), ("estacionamiento", "parking"),
    ("desayuno", "breakfast"), ("lavandería", "laundry"), ("lavanderia", "laundry"),
    ("taxi", "taxi"), ("médico", "doctor"), ("medico", "doctor"),
    ("mascotas", "pets"), ("fumar", "smoking"), ("niños", "children"),
    ("cama supletoria", "extra bed"), ("depósito", "deposit"), ("deposito", "deposit"),
    ("documento de identidad", "photo id"),
    ("sala de conferencias", "conference room"),

    # --- the hotel's own names, as a Devanagari caller says them ---------------------------------
    #
    # The renderers keep dish and room names in Latin script because they are proper nouns on this
    # hotel's menu -- "Chicken Kebab" is what the kitchen calls it and what the sign says. But a
    # caller does not read the sign, they speak, and Whisper writes what it hears: चिकन कबाब. So the
    # names have to be recognised in Devanagari even though they are never SPOKEN in it. This is the
    # asymmetry that makes an input table necessary rather than a mirror of the output table.
    #
    # Spanish needs no equivalent: a Spanish speaker says these names in the same Latin letters
    # already in `_DISH_NAMES`.
    ("चिकन कबाब", "chicken kebab"), ("चिकन कबाब", "chicken kebab"),
    ("पनीर बटर मसाला", "paneer butter masala"), ("पनीर टिक्का", "paneer tikka"),
    ("वेजिटेबल समोसा", "vegetable samosa"), ("समोसा", "samosa"),
    ("बटर चिकन", "butter chicken"),
    ("फिश करी", "fish curry"), ("मछली करी", "fish curry"),
    ("वेजिटेबल बिरयानी", "vegetable biryani"), ("वेज बिरयानी", "vegetable biryani"),
    ("बिरयानी", "biryani"),
    ("चॉकलेट ब्राउनी", "chocolate brownie"), ("ब्राउनी", "brownie"),
    ("फ्रूट बाउल", "fresh fruit bowl"), ("फल", "fruit"),
    ("फ्रेश लाइम सोडा", "fresh lime soda"), ("नींबू सोडा", "fresh lime soda"),
    ("मसाला चाय", "masala chai"), ("चाय", "chai"),
    ("मिनरल वाटर", "mineral water"), ("पानी", "water"),

    ("स्टैंडर्ड किंग", "standard king"), ("स्टैंडर्ड ट्विन", "standard twin"),
    ("डीलक्स किंग", "deluxe king"), ("डिलक्स किंग", "deluxe king"),
    ("एक्जीक्यूटिव सुइट", "executive suite"), ("एग्जीक्यूटिव सुइट", "executive suite"),
    ("फैमिली सुइट", "family suite"),

    # --- gaps found by measurement, 2026-09-18 -------------------------------------------------
    #
    # `scripts/measure_understanding.py` routes a fixed set of sentences per language and reports
    # what reached which tool. English scored 31/31 and Hindi 23/31, and every one of the Hindi
    # misses was a missing WORD rather than a missing rule -- the sentence was translated into
    # something the router could almost read. Added here rather than as router entries, because
    # the router's tables are English and this file is where the other two languages are met.
    ("एलर्जी", "allergy"), ("मेवे", "nuts"), ("मेवा", "nuts"),
    ("खा सकता", "eat"), ("खा सकती", "eat"), ("खा सकते", "eat"),
    ("कहाँ", "where are you"), ("कहां", "where are you"),
    ("किस तरह के", "what kind of"), ("कैसे", "what kind of"), ("किस प्रकार के", "what kind of"),
    ("एग्जीक्यूटिव सूट", "executive suite"), ("एक्जीक्यूटिव सूट", "executive suite"),
    ("डीलक्स सूट", "deluxe king"),
    ("मैंने", "i"), ("किया था", "did"), ("किया", "did"),

    ("opciones", "options"), ("veganas", "vegan"), ("vegana", "vegan"),
    ("vegetarianas", "vegetarian"), ("vegetariana", "vegetarian"),
    ("ubicados", "located"), ("ubicado", "located"), ("ubicacion", "location of the hotel"),
    ("ubicación", "location of the hotel"),
    ("donde estan", "where are you"), ("donde están", "where are you"),
    ("donde esta", "where are you"), ("donde está", "where are you"),
    ("he pedido", "did i order"), ("pedi", "did i order"), ("pedí", "did i order"),

    # Phrases, not words, because both languages put "about" and "ordered" where English does not.
    # Hindi is verb-final, so "ऑर्डर किया था" is *ordered did* and no word-by-word mapping produces
    # "did i order"; Spanish wraps the noun ("hábleme DEL paneer tikka").
    ("के बारे में बताइए", "tell me about"), ("के बारे में बताओ", "tell me about"),
    ("के बारे में", "about"), ("बताइए", "tell me"), ("बताओ", "tell me"),
    ("ऑर्डर किया था", "did i order"), ("ऑर्डर किया", "did i order"),
    ("hableme del", "tell me about"), ("háblame del", "tell me about"),
    ("hableme de", "tell me about"), ("háblame de", "tell me about"),
    ("cuenteme del", "tell me about"), ("cuénteme del", "tell me about"),
]

# Longest source phrase first. Sorting here rather than trusting the order above means a new entry
# can be added anywhere in the list without quietly shadowing an existing one.
# THE BOUNDARY HAS TO KNOW ABOUT DEVANAGARI, and `\w` does not.
#
# A Devanagari matra -- the vowel sign written onto a consonant -- is a combining mark, and
# `str.isalnum()` is False for it, so `\w` does not match it and `(?!\w)` happily reports a word
# boundary in the MIDDLE of a word. "बारे" ("about") therefore matched the entry `बार` ("bar") and
# was rewritten to "barे", an English word welded to an orphan matra. The router then found "bar"
# and answered "tell me about the paneer tikka" with the bar's opening hours.
#
# This is the third time the same fact has caused a bug in this codebase -- it broke `normalise`
# twice and `_find_party_size` once -- so the boundary is spelled out rather than borrowed: any
# character in the Devanagari block, letter or mark, means we are still inside a word.
_INSIDE_A_WORD = r"[\wऀ-ॿ]"

_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<!{_INSIDE_A_WORD}){re.escape(src)}(?!{_INSIDE_A_WORD})"), dst)
    for src, dst in sorted(_PAIRS, key=lambda pair: -len(pair[0]))
)


def to_router_language(spoken: str) -> str:
    """Rewrite known Hindi and Spanish keywords into the English the router matches.

    Idempotent on English input: every replacement is already-English, so running this over an
    English sentence leaves it unchanged apart from the handful of words that are spelled the same
    in Spanish ("taxi", "spa"), which map to themselves.

    The word boundary is spelled out in `_INSIDE_A_WORD` rather than borrowed from the regex
    engine's own, and the comment there says why: neither `\\b` nor `\\w` knows that a Devanagari
    matra belongs to the word it is written on, so both report a boundary in the middle of one.
    """
    for pattern, replacement in _SUBSTITUTIONS:
        spoken = pattern.sub(replacement, spoken)
    return " ".join(spoken.split())
