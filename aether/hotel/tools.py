"""Hotel menu tools, and the templates that turn their results into speech.

These plug into the EXISTING `aether.tools.ToolRunner` via its injectable registry. Nothing about
fencing, delay injection or result identity is reimplemented here -- the fence check placed after
the delay and before the tool body, the task/generation/state stamping and the canonical
`TaskStarted`/`ResultReceived`/`ResultDiscarded` events all come from that runner unchanged. A
second tool layer would mean a second place for a stale result to slip through.

The tool bodies follow the same contract as the warehouse ones: take the store plus validated
parameters, return `(records, summary)`, emit nothing, sleep for nothing, check nothing.

**Why templates and not the LLM.** A menu answer is a fact read aloud. Routing
`{"name": "chicken kebab", "price": 380}` through Gemini to be re-phrased costs a measured ~1.8 s
and introduces a way to quote a price the hotel does not charge. The model is for conversation, not
for reading the menu back.

Every template is written to be SPOKEN: numbers as words, no symbols, no markdown, one or two short
sentences. Rime receives what a person would say.
"""

from __future__ import annotations

from collections.abc import Callable

from . import (
    Category,
    Diet,
    MenuStore,
    UnknownDish,
    describe_dish,
    say_list,
    say_price,
)


# NOTE: the dish parameter is `dish`, not `name`. `ToolRunner.run(name, ...)` already takes the
# TOOL's name positionally, so a tool parameter called `name` collides with it. Worth stating
# because `name` is the obvious thing to call it and the failure is a confusing TypeError.


def _parse_category(value: str) -> Category:
    """Map spoken words to a category. Raises rather than guessing at an unknown one."""
    spoken = value.lower().strip().rstrip("s")
    for category in Category:
        if category.value.rstrip("s") == spoken:
            return category
    # Words a caller actually uses that are not the enum's own name.
    synonyms = {
        "appetiser": Category.STARTERS, "appetizer": Category.STARTERS,
        "starter": Category.STARTERS, "entree": Category.MAINS,
        "main course": Category.MAINS, "mains course": Category.MAINS,
        "sweet": Category.DESSERTS, "pudding": Category.DESSERTS,
        "drink": Category.DRINKS, "beverage": Category.DRINKS,
    }
    if spoken in synonyms:
        return synonyms[spoken]
    raise UnknownDish(f"no such menu category: {value}")


def _parse_diet(value: str) -> Diet:
    spoken = value.lower().strip()
    if spoken in ("veg", "vegetarian"):
        return Diet.VEG
    if spoken in ("vegan", "plant based", "plant-based"):
        return Diet.VEGAN
    if spoken in ("non veg", "non-veg", "nonveg", "non vegetarian", "non-vegetarian", "meat"):
        return Diet.NON_VEG
    raise UnknownDish(f"no such dietary preference: {value}")


# --- tool bodies -------------------------------------------------------------------------

def _list_category(store: MenuStore, *, category: str) -> tuple[list, dict]:
    """"What starters do you have?" -- the demo's opening question."""
    parsed = _parse_category(category)
    rows = [describe_dish(d) for d in store.in_category(parsed)]
    return rows, {"category": parsed.value}


def _price_of(store: MenuStore, *, dish: str) -> tuple[list, dict]:
    """"How much is the chicken kebab?" """
    found = store.find(dish)
    return [describe_dish(found)], {"name": found.name, "price": found.price,
                                    "available": found.available}


def _find_by_diet(store: MenuStore, *, diet: str, category: str | None = None) -> tuple[list, dict]:
    """"Do you have any vegetarian main courses?" """
    parsed_diet = _parse_diet(diet)
    parsed_category = _parse_category(category) if category else None
    rows = [describe_dish(d) for d in store.by_diet(parsed_diet, category=parsed_category)]
    return rows, {"diet": parsed_diet.value,
                  "category": parsed_category.value if parsed_category else None}


def _check_availability(store: MenuStore, *, dish: str) -> tuple[list, dict]:
    """"Do you still have the seafood platter?" """
    found = store.find(dish)
    return [describe_dish(found)], {"name": found.name, "available": found.available}


def _check_allergens(store: MenuStore, *, dish: str) -> tuple[list, dict]:
    """"Does the butter chicken have nuts?" -- the question a caller asks about themselves.

    Answered from the fixture, never from the model. Getting an allergen wrong is the one menu
    error that could actually hurt somebody, so it must never be inferred.
    """
    found = store.find(dish)
    return [describe_dish(found)], {"name": found.name, "allergens": list(found.allergens)}


def _find_by_spice(store: MenuStore, *, spice: str,
                   category: str | None = None) -> tuple[list, dict]:
    """"Do you have anything mild?" """
    wanted = spice.lower().strip()
    aliases = {"not spicy": "none", "no spice": "none", "plain": "none",
               "medium spicy": "medium", "very spicy": "hot", "spicy": "hot"}
    wanted = aliases.get(wanted, wanted)
    if wanted not in ("none", "mild", "medium", "hot"):
        raise UnknownDish(f"no such spice level: {spice}")
    parsed_category = _parse_category(category) if category else None
    rows = [describe_dish(d) for d in store.dishes()
            if d.available and d.spice == wanted
            and (parsed_category is None or d.category is parsed_category)]
    return rows, {"spice": wanted,
                  "category": parsed_category.value if parsed_category else None}


# name -> (function, mutates?). None of these mutate: a caller asking about the menu must never be
# able to change it. Availability edits are staff-side and deliberately absent from this registry.
HOTEL_TOOLS: dict[str, tuple[Callable[..., tuple[list, dict]], bool]] = {
    "list_category": (_list_category, False),
    "price_of": (_price_of, False),
    "find_by_diet": (_find_by_diet, False),
    "check_availability": (_check_availability, False),
    "check_allergens": (_check_allergens, False),
    "find_by_spice": (_find_by_spice, False),
}


# --- spoken templates ---------------------------------------------------------------------

def _speak_list_category(result) -> str:
    rows, category = result.records, result.summary.get("category", "menu")
    if not rows:
        return f"I am sorry, we have nothing on the {category} menu right now."
    names = say_list([r["name"] for r in rows])
    lead = f"For {category} we have {names}."
    if len(rows) == 1:
        return f"{lead} It is {say_price(rows[0]['price'])}."
    cheapest = min(rows, key=lambda r: r["price"])
    return f"{lead} They start at {say_price(cheapest['price'])}."


def _speak_price_of(result) -> str:
    dish = result.records[0]
    price = say_price(dish["price"])
    if not dish["available"]:
        return f"The {dish['name']} is {price}, but I am afraid it is not available today."
    return f"The {dish['name']} is {price}."


def _speak_find_by_diet(result) -> str:
    rows = result.records
    diet = result.summary.get("diet", "")
    category = result.summary.get("category")
    scope = f"{diet} {category}" if category else diet
    if not rows:
        return f"I am sorry, we have no {scope} options available today."
    return f"Yes. For {scope} we have {say_list([r['name'] for r in rows])}."


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


def _speak_find_by_spice(result) -> str:
    rows = result.records
    spice = result.summary.get("spice", "")
    label = "not spicy at all" if spice == "none" else spice
    if not rows:
        return f"I am sorry, we have nothing {label} available today."
    return f"For something {label} we have {say_list([r['name'] for r in rows])}."


SPEAK: dict[str, Callable[..., str]] = {
    "list_category": _speak_list_category,
    "price_of": _speak_price_of,
    "find_by_diet": _speak_find_by_diet,
    "check_availability": _speak_check_availability,
    "check_allergens": _speak_check_allergens,
    "find_by_spice": _speak_find_by_spice,
}

# Spoken when a tool ran but could not answer -- an unknown dish, an unrecognised category. A true
# "I do not have that" is a useful answer and may be spoken; only a STALE result may not.
NOT_FOUND = "I am sorry, I could not find that on our menu. Could you say it again?"


def render(result) -> str | None:
    """Turn a `ToolResult` into the sentence Rime will speak, or None if it must not be spoken.

    The stale check is first and is not negotiable: a result produced for a question the caller has
    already moved on from must never become speech, no matter how good the answer was.
    """
    if not result.may_speak:
        return None
    if result.reason or not result.records and result.tool != "list_category":
        return NOT_FOUND
    speaker = SPEAK.get(result.tool)
    if speaker is None:
        return None
    return speaker(result)
