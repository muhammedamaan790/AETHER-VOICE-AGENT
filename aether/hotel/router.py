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

from . import MENU, Category

# Longest-first, so "main course" wins over "main" and "non vegetarian" over "vegetarian".
_CATEGORY_WORDS: tuple[tuple[str, Category], ...] = tuple(sorted(
    (
        ("starters", Category.STARTERS), ("starter", Category.STARTERS),
        ("appetisers", Category.STARTERS), ("appetizers", Category.STARTERS),
        ("appetiser", Category.STARTERS), ("appetizer", Category.STARTERS),
        ("main course", Category.MAINS), ("main courses", Category.MAINS),
        ("mains", Category.MAINS), ("main", Category.MAINS), ("entree", Category.MAINS),
        ("desserts", Category.DESSERTS), ("dessert", Category.DESSERTS),
        ("sweets", Category.DESSERTS), ("sweet", Category.DESSERTS),
        ("pudding", Category.DESSERTS),
        ("drinks", Category.DRINKS), ("drink", Category.DRINKS),
        ("beverages", Category.DRINKS), ("beverage", Category.DRINKS),
    ),
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

_PRICE_WORDS = ("how much", "price of", "price for", "cost of", "what does", "how expensive")
_AVAILABLE_WORDS = ("available", "do you still have", "in stock", "sold out", "on today")
_ALLERGEN_WORDS = ("allerg", "contain", "nuts", "dairy", "gluten", "shellfish", "eggs", "lactose")
_LIST_WORDS = ("what", "which", "list", "tell me about", "do you have", "any")

# Dish names longest-first, so "paneer butter masala" is matched before "paneer tikka" could
# ambiguously grab "paneer".
_DISH_NAMES: tuple[str, ...] = tuple(sorted((d.name for d in MENU), key=len, reverse=True))


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

    # 4. How hot a named dish is. Before the bare-dish fallback: "is the chicken kebab spicy"
    #    names a dish and no price words, so it used to be answered with a price -- a confidently
    #    wrong answer to a question about heat.
    if dish and spice is not None:
        return Route("spice_of", {"dish": dish}, "dish+spice")

    # 5. A dish named with no other signal -- treat as "tell me about it", which is its price.
    if dish:
        return Route("price_of", {"dish": dish}, "dish only")

    # 6. An allergy with no dish named: "I have a nut allergy, what can I eat?". Requires BOTH an
    #    allergen and an avoidance cue, so "do you have any fish" stays a menu browse rather than
    #    becoming a medical question.
    if allergen and any(word in spoken for word in _AVOIDANCE_WORDS):
        params: dict[str, object] = {"allergen": allergen}
        if category is not None:
            params["category"] = category.value
        return Route("safe_for", params, "allergen avoidance")

    # 7. Dietary request, optionally narrowed to a category.
    if diet:
        params = {"diet": diet}
        if category is not None:
            params["category"] = category.value
        return Route("find_by_diet", params, "diet")

    # 8. Spice request, optionally narrowed.
    if spice and any(word in spoken for word in _LIST_WORDS):
        params = {"spice": spice}
        if category is not None:
            params["category"] = category.value
        return Route("find_by_spice", params, "spice")

    # 9. A whole category.
    if category is not None and any(word in spoken for word in _LIST_WORDS):
        return Route("list_category", {"category": category.value}, "category")

    # Nothing confident. The LLM takes it -- a slower answer beats a wrong tool.
    return None
