"""The twelve policies added on 2026-09-10, and the menu questions they must not swallow.

Each new topic was a question the router could hear and the database could not answer, so the turn
fell through to the model. R8b.3 permits that -- but a fact the hotel definitely knows should not be
improvised differently on two different calls, so each one now has exactly one authoritative row.

Two risks come with adding keywords to `_POLICY_WORDS`, and both are tested here:

1. **Theft.** Policy words are matched BEFORE menu routing. A bare "restaurant" would swallow "what
   is on the restaurant menu" and answer it with opening hours -- a confident answer to a question
   nobody asked, which is the exact defect `"do you have room for this?"` produced on a real call.
2. **Silence.** A row nobody can reach is not an improvement. Every new topic is asked for in the
   words a caller would use, and must reach its own row.

The rendered wording itself is covered in every language by `tests/test_language.py`, which walks
the whole policy table -- so these rows inherit that coverage rather than duplicating it.
"""

from __future__ import annotations

import pytest

from aether.hotel import HotelStore
from aether.hotel.router import route
from aether.hotel.tools import HOTEL_TOOLS, render
from aether.lang import ENGLISH, HINDI, SPANISH
from aether.tools import ToolRunner
from aether.trace import Trace

STORE = HotelStore()

# The caller's words on the left, the row they must reach on the right.
ASKS = [
    ("Do you have a swimming pool?", "swimming_pool"),
    ("Can I go for a swim?", "swimming_pool"),
    ("Is there a gym?", "gym"),
    ("Do you have a fitness centre?", "gym"),
    ("Do you have a spa?", "spa"),
    ("Can I book a massage?", "spa"),
    ("Can I get an extra bed?", "extra_bed"),
    ("Is there a doctor available?", "doctor_on_call"),
    ("Can you book me a taxi?", "taxi_booking"),
    ("Do you have a conference room?", "conference_room"),
    ("Do you have a meeting room?", "conference_room"),
    ("Is there power backup?", "power_backup"),
    ("What happens in a power cut?", "power_backup"),
    ("What time does the restaurant open?", "restaurant"),
    ("What are the restaurant timings?", "restaurant"),
    ("Do you have a bar?", "bar"),
    ("Is there a security deposit?", "deposit"),
    ("What documents do I need at check in?", "id_proof"),
    ("Do I need to bring my passport?", "id_proof"),
]

# Questions that were already answered correctly and must still be. Every one of these contains a
# word that now also appears in the policy table.
MUST_NOT_BE_STOLEN = [
    ("What is on the restaurant menu?", "menu_overview"),
    ("What is on the menu?", "menu_overview"),
    ("What drinks do you have?", "list_category"),
    ("What desserts do you have?", "list_category"),
    ("How much is the chicken kebab?", "price_of"),
    ("What starters do you have?", "list_category"),
    ("What rooms do you have?", "list_room_types"),
    ("What time is check in?", "check_in_out"),
]

NEW_TOPICS = sorted({topic for _ask, topic in ASKS})


@pytest.mark.parametrize(("ask", "topic"), ASKS)
def test_the_caller_reaches_the_row_using_the_words_they_would_use(ask: str, topic: str) -> None:
    decided = route(ask)
    assert decided is not None, f"{ask!r} fell through to the model"
    assert decided.tool == "hotel_policy", f"{ask!r} routed to {decided.tool}"
    assert decided.params["topic"] == topic, f"{ask!r} reached {decided.params['topic']!r}"


@pytest.mark.parametrize(("ask", "tool"), MUST_NOT_BE_STOLEN)
def test_a_menu_or_room_question_is_not_stolen_by_the_new_policy_words(ask: str, tool: str) -> None:
    """Policy words are matched first, so every one of them is a chance to answer the wrong thing."""
    decided = route(ask)
    assert decided is not None, f"{ask!r} stopped being answerable from the database"
    assert decided.tool == tool, f"{ask!r} was stolen by {decided.tool}"


@pytest.mark.parametrize("topic", NEW_TOPICS)
def test_every_new_topic_exists_in_the_one_database(topic: str) -> None:
    """A keyword pointing at a row that is not there would answer "I could not find that"."""
    assert STORE.policy(topic) is not None, f"no row for {topic!r}"


@pytest.mark.parametrize("topic", NEW_TOPICS)
@pytest.mark.parametrize("language", [ENGLISH, HINDI, SPANISH], ids=lambda lang: lang.code)
def test_every_new_topic_is_a_real_sentence_in_every_language(topic: str, language) -> None:
    """R8b.6: an English-only hotel capability is a defect.

    "Real sentence" here means three things a template failure would break: it says something, it
    is not the not-found fallback, and it does not leak the machine key ("id proof", "power backup"
    with an underscore) into speech.
    """
    from aether.hotel.tools import _renderer_for

    runner = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS)
    result = runner.run("hotel_policy", gen="p", turn_id=1, is_valid=lambda: True, topic=topic)
    spoken = render(result, language)
    _templates, not_found = _renderer_for(language)

    assert spoken and spoken != not_found, f"{topic} has no {language.code} answer"
    # Only multi-word keys can "leak": `spa`, `gym` and `bar` ARE the natural word, so finding them
    # in the sentence is the template working. `power_backup` appearing verbatim would not be.
    if "_" in topic:
        assert topic not in spoken, f"{topic} leaked its machine key into speech: {spoken!r}"
    assert "_" not in spoken, f"{topic} leaked an underscore into speech: {spoken!r}"


@pytest.mark.parametrize("language", [ENGLISH, HINDI, SPANISH], ids=lambda lang: lang.code)
def test_opening_hours_are_not_phrased_as_something_the_hotel_offers(language) -> None:
    """"Yes, we offer the bar free of charge from five in the evening" is what the generic template
    produces, and it is three kinds of wrong in one sentence. These topics take their own branch."""
    runner = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS)
    for topic in ("restaurant", "bar"):
        result = runner.run("hotel_policy", gen="p", turn_id=1, is_valid=lambda: True, topic=topic)
        spoken = render(result, language)
        for offer in ("we offer", "ofrecemos", "उपलब्ध है"):
            assert offer not in spoken, f"{topic} in {language.code} reads as an offer: {spoken!r}"


@pytest.mark.parametrize("language", [ENGLISH, HINDI, SPANISH], ids=lambda lang: lang.code)
def test_the_accepted_documents_are_alternatives_not_a_checklist(language) -> None:
    """Any ONE of the three is enough. `say_list` joins with "and", which would tell a caller to
    bring all three -- so this branch builds its own "or"."""
    runner = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS)
    result = runner.run("hotel_policy", gen="p", turn_id=1, is_valid=lambda: True, topic="id_proof")
    spoken = render(result, language)
    alternatives = {"eng": " or ", "hin": " या ", "spa": " o "}[language.code]
    assert alternatives in spoken, f"the documents read as a checklist in {language.code}: {spoken!r}"


def test_the_fee_every_language_speaks_is_the_row_in_the_database() -> None:
    """The spa and the deposit carry prices, so they can drift from the database like any price."""
    from aether.hotel import say_price

    for topic in ("spa", "extra_bed", "conference_room", "deposit"):
        row = STORE.policy(topic)
        assert row is not None and row.fee, f"{topic} lost its fee"
        runner = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS)
        result = runner.run("hotel_policy", gen="p", turn_id=1, is_valid=lambda: True, topic=topic)
        assert say_price(row.fee) in render(result, ENGLISH), f"{topic} does not speak its own row"
