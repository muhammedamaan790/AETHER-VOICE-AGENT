"""Hotel menu fixture: the structured data AETHER answers from, never LLM knowledge.

A caller asking "how much is the chicken kebab?" must get the hotel's actual price, not a plausible
number a language model produced. That is the whole reason this exists as data rather than as
prompt text: a menu is a fact table, and facts belong in a lookup.

Modelled deliberately on `aether/warehouse/` -- frozen dataclasses, module-level constants, a store
that owns the only mutable state and bumps `state_version` on every change. Same shape, so the same
`ToolRunner` drives both and there is one set of fencing semantics rather than two.

Deterministic by construction: no randomness, no clocks, no IDs derived from time. Two runs a week
apart produce byte-identical answers, which is what makes a demo rehearsable and a test meaningful.

Scope: this is Milestone 1 -- enough menu to prove the call path end to end with real questions.
It is not a hotel management system, and it is not trying to be complete.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from ..errors import ToolLookupError


class Category(str, Enum):
    """The four things a caller asks for by name."""

    STARTERS = "starters"
    MAINS = "mains"
    DESSERTS = "desserts"
    DRINKS = "drinks"


class Diet(str, Enum):
    VEG = "vegetarian"
    NON_VEG = "non-vegetarian"
    VEGAN = "vegan"


@dataclass(frozen=True)
class Dish:
    """One menu item. `available` is the only field the tools ever change."""

    dish_id: str
    name: str
    category: Category
    price: float               # in the hotel's local currency, rendered by the templates
    diet: Diet
    spice: str                 # "none" | "mild" | "medium" | "hot"
    allergens: tuple[str, ...] = ()
    available: bool = True


# --- the fixture ------------------------------------------------------------------------
#
# Hand-written and fixed. Sized so the demo questions in the plan all have real answers, and so at
# least one dish is unavailable and one category has a clear price spread -- both are answer shapes
# the templates must handle and a judge may probe.

MENU: tuple[Dish, ...] = (
    # --- Starters ---------------------------------------------------------------------
    Dish("D-101", "chicken kebab",        Category.STARTERS, 380.0, Diet.NON_VEG, "medium", ("dairy",)),
    Dish("D-102", "paneer tikka",         Category.STARTERS, 340.0, Diet.VEG,     "medium", ("dairy",)),
    Dish("D-103", "tomato shorba",        Category.STARTERS, 220.0, Diet.VEGAN,   "mild"),
    Dish("D-104", "crispy corn",          Category.STARTERS, 260.0, Diet.VEG,     "mild"),
    Dish("D-105", "seafood platter",      Category.STARTERS, 890.0, Diet.NON_VEG, "medium",
         ("shellfish", "fish"), False),                      # sold out on purpose
    Dish("D-106", "mushroom galouti",     Category.STARTERS, 360.0, Diet.VEG,     "medium", ("dairy", "nuts")),
    Dish("D-107", "prawn koliwada",       Category.STARTERS, 520.0, Diet.NON_VEG, "hot",    ("shellfish",)),
    Dish("D-108", "beetroot carpaccio",   Category.STARTERS, 290.0, Diet.VEGAN,   "none",   ("nuts",)),
    Dish("D-109", "chilli garlic squid",  Category.STARTERS, 480.0, Diet.NON_VEG, "hot",    ("shellfish",)),

    # --- Mains ------------------------------------------------------------------------
    Dish("D-201", "butter chicken",       Category.MAINS,    520.0, Diet.NON_VEG, "mild",   ("dairy", "nuts")),
    Dish("D-202", "dal makhani",          Category.MAINS,    380.0, Diet.VEG,     "mild",   ("dairy",)),
    Dish("D-203", "lamb rogan josh",      Category.MAINS,    640.0, Diet.NON_VEG, "hot"),
    Dish("D-204", "vegetable biryani",    Category.MAINS,    420.0, Diet.VEG,     "medium", ("dairy",)),
    Dish("D-205", "chana masala",         Category.MAINS,    310.0, Diet.VEGAN,   "medium"),
    Dish("D-206", "grilled sea bass",     Category.MAINS,    780.0, Diet.NON_VEG, "mild",   ("fish",)),
    Dish("D-207", "wild mushroom risotto", Category.MAINS,   560.0, Diet.VEG,     "none",   ("dairy",)),
    Dish("D-208", "jackfruit rendang",    Category.MAINS,    440.0, Diet.VEGAN,   "hot",    ("nuts",)),
    Dish("D-209", "tandoori pomfret",     Category.MAINS,    720.0, Diet.NON_VEG, "medium", ("fish", "dairy"), False),
    Dish("D-210", "paneer butter masala", Category.MAINS,    460.0, Diet.VEG,     "mild",   ("dairy", "nuts")),

    # --- Desserts ---------------------------------------------------------------------
    Dish("D-301", "gulab jamun",          Category.DESSERTS, 180.0, Diet.VEG,     "none",   ("dairy",)),
    Dish("D-302", "chocolate fondant",    Category.DESSERTS, 260.0, Diet.VEG,     "none",   ("dairy", "eggs", "gluten")),
    Dish("D-303", "seasonal fruit plate", Category.DESSERTS, 200.0, Diet.VEGAN,   "none"),
    Dish("D-304", "pistachio kulfi",      Category.DESSERTS, 240.0, Diet.VEG,     "none",   ("dairy", "nuts")),
    Dish("D-305", "coconut panna cotta",  Category.DESSERTS, 280.0, Diet.VEGAN,   "none"),

    # --- Drinks -----------------------------------------------------------------------
    Dish("D-401", "masala chai",          Category.DRINKS,    90.0, Diet.VEG,     "none",   ("dairy",)),
    Dish("D-402", "fresh lime soda",      Category.DRINKS,   120.0, Diet.VEGAN,   "none"),
    Dish("D-403", "mango lassi",          Category.DRINKS,   160.0, Diet.VEG,     "none",   ("dairy",)),
    Dish("D-404", "filter coffee",        Category.DRINKS,   110.0, Diet.VEG,     "none",   ("dairy",)),
    Dish("D-405", "ginger lemon tea",     Category.DRINKS,   100.0, Diet.VEGAN,   "none"),
)


CURRENCY = "rupees"


class UnknownDish(ToolLookupError):
    """A dish the menu does not contain. Never guessed at, never approximated to a near match."""


class MenuStore:
    """The only mutable menu state, and the only thing that may change it.

    Each call constructs its own store, so two concurrent calls cannot see each other's changes.
    Dishes are frozen and replaced wholesale on mutation, so a record handed to a tool result stays
    a faithful snapshot of the moment it was produced.
    """

    def __init__(self, menu: tuple[Dish, ...] = MENU):
        self._dishes: dict[str, Dish] = {d.dish_id: d for d in menu}
        self._order: tuple[str, ...] = tuple(d.dish_id for d in menu)
        # Stamped onto every tool result, so a late-arriving answer is recognisable as describing a
        # menu that has since changed -- independently of generation fencing.
        self.state_version: int = 0

    # --- reading ------------------------------------------------------------------------

    def dishes(self) -> list[Dish]:
        """Every dish, in menu order. A copy: callers cannot mutate our state."""
        return [self._dishes[i] for i in self._order]

    def in_category(self, category: Category, *, available_only: bool = True) -> list[Dish]:
        rows = [d for d in self.dishes() if d.category is category]
        return [d for d in rows if d.available] if available_only else rows

    def by_diet(self, diet: Diet, *, category: Category | None = None) -> list[Dish]:
        """Vegan counts as vegetarian for a caller who asks for vegetarian options.

        A caller asking "do you have vegetarian mains" wants everything they can eat, not a
        taxonomy lesson -- excluding the vegan dishes would be a technically-correct wrong answer.
        """
        rows = [d for d in self.dishes() if d.available]
        if diet is Diet.VEG:
            rows = [d for d in rows if d.diet in (Diet.VEG, Diet.VEGAN)]
        else:
            rows = [d for d in rows if d.diet is diet]
        if category is not None:
            rows = [d for d in rows if d.category is category]
        return rows

    def find(self, name: str) -> Dish:
        """Look a dish up by spoken name. Exact match first, then a containment match.

        STT will not reliably produce "chicken kebab" -- it may give "the chicken kebab" or
        "chicken kebabs". Containment handles that without inventing a fuzzy matcher whose failures
        would be hard to explain. An ambiguous or absent match raises rather than guessing, because
        quoting the wrong price is worse than admitting we did not catch it.
        """
        spoken = " ".join(name.lower().split())
        if not spoken:
            raise UnknownDish("no dish name given")

        for dish in self.dishes():
            if dish.name == spoken:
                return dish

        matches = [d for d in self.dishes() if d.name in spoken or spoken in d.name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise UnknownDish(f"no dish matching: {name}")
        raise UnknownDish(f"more than one dish matches: {name}")

    def dish(self, dish_id: str) -> Dish:
        try:
            return self._dishes[dish_id]
        except KeyError as exc:
            raise UnknownDish(f"no such dish id: {dish_id}") from exc

    # --- mutation -----------------------------------------------------------------------

    def set_available(self, dish_id: str, available: bool) -> Dish:
        """The only way menu state changes. Bumps `state_version`, like the warehouse store."""
        updated = replace(self.dish(dish_id), available=available)
        self._dishes[dish_id] = updated
        self.state_version += 1
        return updated


def describe_dish(dish: Dish) -> dict[str, object]:
    """Flatten one dish into the plain dict a tool result carries.

    Done here rather than in each tool so every tool describes a dish identically, and so what
    reaches a caller is data rather than a live store reference.
    """
    return {
        "dish_id": dish.dish_id,
        "name": dish.name,
        "category": dish.category.value,
        "price": dish.price,
        "diet": dish.diet.value,
        "spice": dish.spice,
        "allergens": list(dish.allergens),
        "available": dish.available,
    }


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
    """`380.0` -> `three hundred and eighty rupees`. Whole units only; this menu has no paise."""
    return f"{say_number(int(round(price)))} {CURRENCY}"


def say_list(items: list[str]) -> str:
    """`a, b and c` -- the way a person reads a short list aloud."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"
