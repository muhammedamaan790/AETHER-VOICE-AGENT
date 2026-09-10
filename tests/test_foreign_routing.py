"""A Hindi or Spanish caller must reach the database, not the model.

THE DEFECT THIS PINS. AETHER answered in three languages long before it understood three. The
language tests in `test_language.py` all feed ENGLISH questions and assert the ANSWER comes back in
the target language -- which is a real property, and is not this one. A caller actually speaking
Hindi produced Devanagari from Whisper, matched nothing in the router, and fell through to Gemini.
The reply was still in Hindi, which is exactly why it went unnoticed, but it came from a language
model rather than from `data/aether_hotel.db`. For every non-English caller the project's central
claim was false.

Two things had to change and both are pinned below: `normalise()` was deleting every Devanagari
vowel mark, so no Hindi keyword could ever have matched whatever table it was in; and the router had
no vocabulary but English.

These tests assert ROUTING, deliberately. `test_language.py` already owns the rendering side, and
the point here is the half that was missing: that the question reaches a tool at all.
"""

from __future__ import annotations

import unicodedata

import pytest

from aether.hotel import HotelStore
from aether.hotel._foreign import to_router_language
from aether.hotel.router import normalise, route

STORE = HotelStore()

# What a caller actually says, and the tool it has to reach. Hindi in Devanagari (what Whisper
# returns), Hinglish (what an English session returns for the same caller), and Spanish.
HINDI = [
    ("मेन्यू में क्या है", "menu_overview"),
    ("स्टार्टर में क्या है", "list_category"),
    ("मिठाई में क्या है", "list_category"),
    ("पीने में क्या है", "list_category"),
    ("चिकन कबाब कितने का है", "price_of"),
    ("पनीर टिक्का कितने का है", "price_of"),
    ("क्या फिश करी उपलब्ध है", "check_availability"),
    ("क्या शाकाहारी खाना है", "find_by_diet"),
    ("एक्जीक्यूटिव सुइट कितने का है", "room_price"),
    ("क्या कमरे खाली हैं", "room_availability"),
    ("चेक इन का समय क्या है", "check_in_out"),
    ("क्या आपके पास स्विमिंग पूल है", "hotel_policy"),
    ("क्या जिम है", "hotel_policy"),
]

HINGLISH = [
    ("menu mein kya hai", "menu_overview"),
    ("chicken kebab kitne ka hai", "price_of"),
    ("swimming pool hai kya", "hotel_policy"),
    ("check in ka time kya hai", "check_in_out"),
]

SPANISH = [
    ("que hay en el menu", "menu_overview"),
    ("que tienen de entrantes", "list_category"),
    ("que postres tienen", "list_category"),
    ("cuanto cuesta la suite ejecutiva", "room_price"),
    ("tienen opciones vegetarianas", "find_by_diet"),
    ("a que hora es la entrada", "check_in_out"),
    ("tienen piscina", "hotel_policy"),
    ("hay gimnasio", "hotel_policy"),
]

ALL = ([(s, t, "hin") for s, t in HINDI]
       + [(s, t, "hin-latin") for s, t in HINGLISH]
       + [(s, t, "spa") for s, t in SPANISH])


# --- the bug underneath everything ------------------------------------------------------------

def test_normalise_keeps_devanagari_vowel_marks() -> None:
    """The bug that made Hindi routing impossible rather than merely unimplemented.

    `[^\\w\\s-]` looks script-agnostic and is not: Python's `\\w` is `str.isalnum()` plus
    underscore, and a matra is category Mn/Mc, for which `isalnum()` is False. So
    "मेन्यू में क्या है" became "म न य म क य ह" before any table was consulted.
    """
    said = "मेन्यू में क्या है"
    assert normalise(said) == said

    marks = [ch for ch in said if unicodedata.category(ch).startswith("M")]
    assert marks, "this fixture no longer contains combining marks, so it tests nothing"
    for mark in marks:
        assert mark in normalise(said), f"{mark!r} was stripped"


def test_normalise_still_strips_the_punctuation_it_always_did() -> None:
    """The fix widened what is kept. It must not have widened it to everything."""
    assert normalise("  What STARTERS, do you have?? ") == "what starters do you have"
    assert normalise("¿Qué hay en el menú?") == "qué hay en el menú"


@pytest.mark.parametrize("said", [
    "what starters do you have",
    "how much is the chicken kebab",
    "do you have a swimming pool",
])
def test_english_is_untouched_by_the_translation_layer(said: str) -> None:
    """The guardrail. Every English routing rule in this project is tested against English input,
    and a substitution table that rewrote English would invalidate all of it at once."""
    assert to_router_language(said) == said


# --- the caller reaches the database ------------------------------------------------------------

_IDS = [f"{script}-{said}" for said, _tool, script in ALL]


@pytest.mark.parametrize(("said", "tool", "script"), ALL, ids=_IDS)
def test_the_caller_reaches_a_tool_rather_than_the_model(said: str, tool: str, script: str) -> None:
    decision = route(said)
    assert decision is not None, f"{script}: {said!r} fell through to the model"
    assert decision.tool == tool, f"{script}: {said!r} reached {decision.tool}, wanted {tool}"


@pytest.mark.parametrize(("said", "_tool", "_script"), ALL, ids=_IDS)
def test_no_hotel_question_in_any_language_reaches_the_model(said, _tool, _script) -> None:
    """The claim, stated as the suite understands it: a fact the database holds never reaches an
    LLM -- in any language, not only in the one the router happens to be written in."""
    assert route(said) is not None


def test_the_same_question_reaches_the_same_tool_in_all_three_languages() -> None:
    """Three phrasings of one question must not be answered by three different tools."""
    for english, hindi, spanish in [
        ("what is on the menu", "मेन्यू में क्या है", "que hay en el menu"),
        ("what time is check in", "चेक इन का समय क्या है", "a que hora es la entrada"),
        ("do you have a swimming pool", "क्या आपके पास स्विमिंग पूल है", "tienen piscina"),
    ]:
        tools = {lang: route(said).tool for lang, said in
                 (("eng", english), ("hin", hindi), ("spa", spanish)) if route(said)}
        assert len(tools) == 3, f"a language fell through: {tools}"
        assert len(set(tools.values())) == 1, f"same question, different tools: {tools}"


def test_a_price_asked_in_hindi_is_the_price_in_the_database() -> None:
    """Routing to the right tool is not enough; it has to carry the right slot."""
    decision = route("चिकन कबाब कितने का है")
    assert decision is not None and decision.tool == "price_of"
    item = STORE.find_item(decision.params["dish"])
    assert item is not None and item.name == "Chicken Kebab"


# --- degrading safely ---------------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "नमस्ते आप कैसे हैं",                 # a greeting: no fact behind it
    "क्या यहाँ आसपास कोई मंदिर है",        # a question the hotel holds no row for
    "hola como estas",
    "que se puede visitar cerca de aqui",
])
def test_a_question_the_database_cannot_answer_still_reaches_the_model(said: str) -> None:
    """The layer rewrites known keywords and leaves everything else alone, so an unknown question
    degrades to "no match" -- and no match is correct: R8b.3 says the model answers those."""
    assert route(said) is None, f"{said!r} was matched to a tool it has no fact for"


def test_the_translation_table_has_no_duplicate_sources() -> None:
    """A duplicated source is a silent contradiction: one of the two mappings can never fire."""
    from aether.hotel._foreign import _PAIRS

    seen: dict[str, str] = {}
    clashes = []
    for src, dst in _PAIRS:
        if src in seen and seen[src] != dst:
            clashes.append(f"{src!r} -> {seen[src]!r} and {dst!r}")
        seen[src] = dst
    assert not clashes, "contradictory entries: " + "; ".join(clashes)
