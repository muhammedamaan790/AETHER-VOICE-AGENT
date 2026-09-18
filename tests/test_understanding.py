"""Every capability, in every language, reaches the tool that can answer it.

RULES.md R8b.6: an English-only hotel capability is a defect. That is easy to state and easy to let
slip, because a question that misses the router still gets a plausible answer -- from the model, in
the right language, which is exactly why it went unnoticed for so long the first time.

This is `scripts/measure_understanding.py` as a test. The script reports and measures; this fails.
Both read the same table, so a sentence added to one is checked by the other, and the score the
documentation quotes cannot drift from the score the suite enforces.

Routing is what is measured -- not recognition, which is a property of Whisper and is measured on
real audio in `scripts/compare_recognisers.py`, and not whether the sentence sounds natural, which
no test can judge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from measure_understanding import LANGUAGES, SUITES  # noqa: E402

from aether.hotel.router import route  # noqa: E402

CASES = [
    (code, suite, said, expected)
    for suite, by_language in SUITES.items()
    for code in LANGUAGES
    for said, expected in by_language[code]
]


@pytest.mark.parametrize(("language", "suite", "said", "expected"), CASES,
                         ids=[f"{c}-{s}-{q}" for c, s, q, _ in CASES])
def test_every_sentence_reaches_its_tool(language, suite, said, expected):
    decision = route(said)
    assert decision is not None, (
        f"[{language}] {said!r} reaches the model -- it should be answered from the database"
    )
    assert decision.tool == expected, (
        f"[{language}] {said!r} went to {decision.tool}, not {expected}"
    )


def test_the_three_languages_are_tested_equally():
    """A guard on the table itself.

    The cheapest way to make this file pass is to test English thoroughly and the other two
    lightly, which would report a healthy score and mean nothing. Every capability must offer the
    same number of sentences in each language.
    """
    for suite, by_language in SUITES.items():
        sizes = {code: len(by_language[code]) for code in LANGUAGES}
        assert len(set(sizes.values())) == 1, (
            f"{suite!r} is tested unevenly across languages: {sizes}"
        )


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_question_is_answered_by_a_mutating_tool_unless_it_asks_for_one(language):
    """The expensive direction, and it has happened twice in this codebase -- both times in a
    language other than English.

    "कमरा तीन शून्य पाँच पर कोई बुकिंग है क्या" (*is there a booking on room 305*) and
    "बुकिंग एक शून्य शून्य चार रद्द कीजिए" (*cancel booking 1004*) both routed to `reserve_room`
    and tried to take a booking. English is protected by exact-form verbs; Hindi and Spanish use
    the same word for the noun, so the protection had to be built rather than inherited.
    """
    from aether.hotel.tools import HOTEL_TOOLS

    asks_to_write = {"reserve_table", "reserve_room", "cancel_booking", "cancel_my_booking",
                     "add_to_order", "place_order", "cancel_order"}
    for suite, by_language in SUITES.items():
        for said, expected in by_language[language]:
            decision = route(said)
            if decision is None:
                continue
            _fn, mutating = HOTEL_TOOLS[decision.tool]
            if mutating and expected not in asks_to_write:
                pytest.fail(
                    f"[{language}] {said!r} is a question and it reached the mutating tool "
                    f"{decision.tool!r}"
                )
