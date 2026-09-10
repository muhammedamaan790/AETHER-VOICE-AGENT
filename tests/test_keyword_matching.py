"""No keyword in the router may match inside another word.

THE BUG THAT STARTED THIS. Every keyword table was matched with plain `in` -- substring matching --
and "night" is inside "tonight". So *"is the chicken kebab available tonight"* was answered with
*"we have forty-one rooms free"*: a confident answer about the wrong table, which is the single
failure mode this router exists to avoid.

It was not a one-off. The same trap was sitting unexploded in several other tables: "any" is inside
"company", "no" was written as `"no "` and is inside "casino ", "rate" is inside "corporate". None
had fired yet, because the caller had not happened to say the word.

The fix could not be "make everything a whole word", because some tables MEAN a prefix -- "allerg"
is there to catch allergy, allergic, allergen and allergies at once, and quietly breaking allergy
detection is the worst outcome available in this codebase. So intent is now declared per entry with
a trailing `*`, and this file holds the rules to it.

The first test is the general one and the reason this file exists: it walks EVERY table in the
router and proves no entry can fire inside a longer word. A new table added next month is covered
without anyone remembering to come back here.
"""

from __future__ import annotations

import re

import pytest

from aether.hotel import router
from aether.hotel.router import _compile, _says, route

# Every keyword table the router matches against, by name, so a failure says which one.
TABLES = {
    name: value for name, value in vars(router).items()
    if name.isupper() and isinstance(value, tuple) and value
    and all(isinstance(entry, str) for entry in value)
}

# Tables of (keyword, value) pairs are matched through `_find_pair`, which has always used word
# boundaries. This file is about the flat tuples matched through `_says`.
FLAT_TABLES = {name: value for name, value in TABLES.items()}


def test_the_tables_were_actually_found() -> None:
    """A discovery test that finds nothing would make everything below vacuously pass."""
    assert len(FLAT_TABLES) >= 8, f"only found {sorted(FLAT_TABLES)}"
    for expected in ("_ROOM_WORDS", "_LIST_WORDS", "_ALLERGEN_WORDS", "_AVOIDANCE_WORDS"):
        assert expected in FLAT_TABLES, f"{expected} is no longer discoverable"


@pytest.mark.parametrize("name", sorted(FLAT_TABLES))
def test_no_keyword_can_fire_inside_a_longer_word(name: str) -> None:
    """The general rule. Every entry is embedded in a longer word and must NOT match.

    A stem entry (`allerg*`) is still anchored at a word start, so "unallergic" must not match it
    either -- the licence a `*` grants is at the end of the word, never at the beginning.
    """
    for entry in FLAT_TABLES[name]:
        stem = entry[:-1] if entry.endswith("*") else entry
        if " " in stem or len(stem) < 2:
            continue                      # phrases and single letters cannot hide inside a word
        haystack = f"xx{stem}xx"
        assert not _compile(entry).search(haystack), (
            f"{name}: {entry!r} fires inside {haystack!r}"
        )


@pytest.mark.parametrize("name", sorted(FLAT_TABLES))
def test_every_keyword_still_matches_itself(name: str) -> None:
    """The other half. A pattern that matches nothing would pass the test above trivially."""
    for entry in FLAT_TABLES[name]:
        stem = entry[:-1] if entry.endswith("*") else entry
        assert _compile(entry).search(stem), f"{name}: {entry!r} does not match its own text"


def test_a_stem_matches_its_inflections_and_a_whole_word_does_not() -> None:
    """What the `*` is for, stated once."""
    assert _says("i have an allergy", ("allerg*",))
    assert _says("she is allergic", ("allerg*",))
    assert _says("does it contain nuts", ("contain*",))
    assert _says("dishes containing nuts", ("contain*",))

    assert not _says("doughnuts please", ("nuts",))
    assert not _says("a company car", ("any",))
    assert not _says("is there a casino", ("no",))
    assert not _says("our corporate booking", ("rate",))   # "corporate" ends in "rate"
    assert _says("what is the rate", ("rate",))            # and the real word still matches

    # A stem is anchored at the START of a word: the `*` licenses an ending, never a beginning.
    assert not _says("hypoallergenic soap", ("allerg*",))


@pytest.mark.parametrize(("table", "said"), [
    ("_ALLERGEN_WORDS", "is she allergic"),
    ("_ALLERGEN_WORDS", "does it have allergens"),
    ("_ALLERGEN_WORDS", "dishes containing nuts"),
    ("_AVOIDANCE_WORDS", "i am allergic"),
    ("_AVOIDANCE_WORDS", "she has allergies"),
    ("_AVOIDANCE_WORDS", "i am intolerant"),
    ("_AVOIDANCE_WORDS", "an intolerance"),
    ("_AVOIDANCE_WORDS", "i am avoiding it"),
])
def test_the_real_tables_still_catch_the_inflections_they_exist_for(table: str, said: str) -> None:
    """Asserted against the SHIPPED tables, not a literal tuple.

    The stem tests above prove `*` works; this proves the tables still use it. Dropping the marker
    from `_ALLERGEN_WORDS` is a one-character edit that would leave the sentence "is she allergic"
    matching nothing -- and it would survive the routing tests, because those sentences also contain
    "nuts", which matches on its own. Allergy detection would degrade silently, which is the one
    failure this codebase treats as unacceptable.
    """
    assert _says(said, FLAT_TABLES[table]), f"{table} no longer matches {said!r}"


# --- the specific wrong answers that were possible ------------------------------------------

@pytest.mark.parametrize(("said", "tool"), [
    # The one that actually happened.
    ("is the chicken kebab available tonight", "check_availability"),
    ("is the fish curry available tonight", "check_availability"),
    # The ones that had not happened yet.
    ("is there a casino nearby", None),
    ("does the company deliver", None),
])
def test_a_word_hiding_inside_another_word_no_longer_answers(said: str, tool: str | None) -> None:
    decision = route(said)
    assert (decision.tool if decision else None) == tool, f"{said!r} -> {decision}"


@pytest.mark.parametrize(("said", "tool"), [
    # A time word must not turn a food question into a room question.
    ("what is on the menu tonight", "menu_overview"),
    ("what starters do you have tonight", "list_category"),
    ("is the chicken kebab available tonight", "check_availability"),
    # ...and must still work when nothing else claims the sentence.
    ("do you have anything free tonight", "room_availability"),
    ("any rooms free tonight", "room_availability"),
    # A room NOUN claims the sentence outright, food words or not.
    ("do you have a suite free tonight", "room_availability"),
    ("is room 305 free", "room_status"),
])
def test_a_time_word_supports_a_room_reading_but_does_not_establish_one(said, tool) -> None:
    """The half of the fix that a naive "make it a whole word" change gets wrong in both directions.

    Matching "night" as a substring made "is the chicken kebab available tonight" a room question.
    Matching it as a whole word and adding "tonight" to the room nouns made "what starters do you
    have tonight" a room question instead -- the same bug wearing different clothes. Time words are
    now a separate table that only claims a sentence mentioning no food at all.
    """
    decision = route(said)
    assert decision is not None, f"{said!r} fell through to the model"
    assert decision.tool == tool, f"{said!r} -> {decision.tool}"


def test_the_room_question_that_genuinely_uses_tonight_still_works() -> None:
    """The fix had two halves, and this is the half a naive fix breaks.

    "tonight" IS a room word -- "do you have anything free tonight" is a room question and always
    was. Simply matching room words as whole words made this fall through, and the suite caught it.
    What resolves the two is that a NAMED DISH wins: the room branch yields when the caller has
    said what they are asking about.
    """
    decision = route("do you have anything free tonight")
    assert decision is not None and decision.tool == "room_availability"

    decision = route("any rooms free tonight")
    assert decision is not None and decision.tool == "room_availability"


def test_allergy_detection_survived_the_change() -> None:
    """The thing that would have broken if `*` had not been introduced, and the reason it was.

    A wrong opening time is corrected on the next call. A wrong allergen answer is not.
    """
    for said in ("i am allergic to nuts", "i have a nut allergy what can i eat",
                 "what is gluten free", "i cannot eat dairy"):
        decision = route(said)
        assert decision is not None, f"{said!r} no longer reaches an allergen tool"
        assert decision.tool in ("safe_for", "check_allergens"), f"{said!r} -> {decision.tool}"

    decision = route("does the butter chicken contain nuts")
    assert decision is not None and decision.tool == "check_allergens"


def test_no_table_still_carries_a_trailing_space() -> None:
    """`"no "` was how the old code approximated a word boundary. The space is now the `(?!\\w)`
    in the pattern, and leaving a stray one behind would mean a keyword that can never match a
    sentence ending on that word."""
    for name, table in FLAT_TABLES.items():
        for entry in table:
            assert entry == entry.strip(), f"{name}: {entry!r} has stray whitespace"


def test_no_keyword_contains_a_star_except_as_the_stem_marker() -> None:
    """`*` is the one special character. A keyword containing it anywhere else would be silently
    reinterpreted."""
    for name, table in FLAT_TABLES.items():
        for entry in table:
            assert "*" not in entry[:-1], f"{name}: {entry!r} contains a star that is not the marker"


def test_the_matcher_is_used_everywhere_rather_than_bare_substring_tests() -> None:
    """Read the source, because a single `in spoken` left behind reopens the whole class.

    Checked as text deliberately: this is a property of how the module is WRITTEN, and no runtime
    assertion can see a substring test that simply has not been reached yet.
    """
    from pathlib import Path

    source = Path(router.__file__).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )
    literals = re.findall(r'"[^"]+" in spoken', body) + re.findall(r"'[^']+' in spoken", body)
    # `"room for" not in spoken` is a deliberate NEGATIVE guard on a phrase, not a keyword match.
    literals = [o for o in literals if "room for" not in o]

    # And the form the original bug actually wore: a generator over a whole table. This is what
    # `_says` replaced, and one of these left behind reopens the entire class of defect.
    generators = re.findall(r"\bany\s*\(\s*\w+\s+in\s+spoken\s+for\b", body)

    assert not literals, f"bare substring tests remain: {literals}"
    assert not generators, f"substring matching over a keyword table remains: {generators}"
