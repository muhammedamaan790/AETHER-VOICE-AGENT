"""The demo script cannot drift from the database.

`DEMO_SCRIPT.md` quotes what AETHER says, turn by turn, and the recording is read off that sheet. So
the sheet is a promise about the running system, and a promise nothing checked is one that rots
quietly: `DEMO.md` carried "the chicken kebab is three hundred and eighty rupees" and a spice answer
for months after the database said 420 and `spice_of` had been deleted. It would have been read
aloud on camera.

The fixture here is the document itself. Every line is put through the real router and the real
`ToolRunner` against the real database, and the rendered answer must match the quoted answer
**exactly** -- not approximately, because the quote is what gets spoken.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aether.hotel import HotelStore
from aether.hotel.router import route
from aether.hotel.tools import HOTEL_TOOLS, render
from aether.tools import ToolRunner
from aether.trace import Trace

SCRIPT = Path(__file__).resolve().parents[1] / "DEMO_SCRIPT.md"
STORE = HotelStore()


def _rows(marker: str) -> list[tuple[str, str]]:
    """The (question, answer) pairs from one marked table.

    The tables are delimited by HTML comments rather than found by heading text, so that editing the
    prose around them -- which happens every time the demo is rehearsed -- cannot silently change
    what is under test.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    body = text.split(f"<!-- {marker}:BEGIN -->", 1)[1].split(f"<!-- {marker}:END -->", 1)[0]

    pairs: list[tuple[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        said, answer = (cells[1], cells[2]) if len(cells) == 3 else (cells[0], cells[1])
        if said.lower() in ("you say", "question"):      # the header row
            continue
        pairs.append((said, answer))
    return pairs


def _rows_with_language(marker: str) -> list[tuple[str, str, str]]:
    """(language code, question, answer) from a table whose first column is the language."""
    text = SCRIPT.read_text(encoding="utf-8")
    body = text.split(f"<!-- {marker}:BEGIN -->", 1)[1].split(f"<!-- {marker}:END -->", 1)[0]

    rows: list[tuple[str, str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3 or cells[0].lower() == "lang":
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


SCRIPT_TURNS = _rows("SCRIPT")
BANK_TURNS = _rows("BANK")
ALL_TURNS = SCRIPT_TURNS + BANK_TURNS

# The Hindi beat, checked exactly as hard as the English. Its table carries a language column, so
# the rows come back as (language code, question, answer) and are rendered by that language's
# renderer. Without this the Hindi answers on the sheet would be the one hand-written thing on the
# page -- and hand-written is precisely what this file exists to prevent, in the language fewest
# people in the room can check.
# Hindi AND Spanish since 2026-09-10: the script used to show the second language for a few
# seconds at the end, which undersold a multilingual product.
HINDI_TURNS = _rows_with_language("LANGUAGES")


def _spoken(said: str) -> str:
    """What AETHER actually says to this, through the real deterministic path."""
    decision = route(said)
    assert decision is not None, f"{said!r} no longer routes -- it would reach the model on camera"
    runner = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS)
    result = runner.run(decision.tool, gen="demo", turn_id=1, is_valid=lambda: True,
                        **decision.params)
    return render(result)


def test_the_script_was_found_and_is_not_empty():
    """A parser that silently matches nothing would make every test below vacuously pass."""
    assert len(SCRIPT_TURNS) == 8, f"expected the eight-turn call, parsed {len(SCRIPT_TURNS)}"
    assert len(BANK_TURNS) >= 12, f"the fallback bank looks truncated: {len(BANK_TURNS)} rows"
    assert len(HINDI_TURNS) >= 5, f"the language beat looks truncated: {len(HINDI_TURNS)} rows"
    codes = {code for code, _said, _quoted in HINDI_TURNS}
    assert {"hin", "spa"} <= codes, f"the script must speak Hindi AND Spanish, found {sorted(codes)}"


@pytest.mark.parametrize(("code", "said", "quoted"), HINDI_TURNS,
                         ids=[said for _c, said, _q in HINDI_TURNS])
def test_every_quoted_hindi_answer_is_what_aether_actually_says(code, said, quoted):
    """Same standard as English: the quote is what gets spoken, so it must match exactly."""
    from aether.lang import by_code

    language = by_code(code)
    assert language is not None, f"unknown language code on the sheet: {code!r}"

    decision = route(said)
    assert decision is not None, (
        f"{said!r} no longer routes -- on camera it would reach the model, in the language "
        f"fewest people in the room can check"
    )
    runner = ToolRunner(Trace(), STORE, tools=HOTEL_TOOLS)
    result = runner.run(decision.tool, gen="demo", turn_id=1, is_valid=lambda: True,
                        **decision.params)
    assert render(result, language) == quoted


@pytest.mark.parametrize(("code", "said", "_quoted"), HINDI_TURNS,
                         ids=[said for _c, said, _q in HINDI_TURNS])
def test_the_hindi_beat_asks_the_same_database_rows_as_the_english_call(code, said, _quoted):
    """The line worth saying out loud during the demo -- not a translated script, the same lookup
    through a different renderer -- has to actually be true."""
    english_tools = {route(q).tool for q, _a in SCRIPT_TURNS if route(q)}
    decision = route(said)
    assert decision is not None
    assert decision.tool in english_tools, (
        f"{said!r} uses {decision.tool}, which the English call never demonstrates"
    )


# The turn the presenter talks over on camera. Its subject is never committed -- exactly as in the
# pipeline, where a fenced turn never reaches the spoken boundary -- so "it" on turn 7 cannot mean it.
BARGE_IN_TURN = 5


def _run_the_call() -> list[tuple[str | None, str | None]]:
    """The scripted call, run as ONE conversation: in order, on a fresh hotel, carrying the subject.

    The fallback bank is a list of independent questions, and `_spoken` checks each alone. The call
    is not: turn 7 ("Book it for two nights") names nothing and only works because the subject from
    turn 6 was carried forward, and it WRITES a booking, so the room and reference it reads out
    depend on every turn before it. Checking its lines one at a time would test a call nobody makes.

    "Fresh hotel" is the shipped database, which is what `scripts/reset_hotel_db.py` gives the
    recording -- so this asserts exactly what will be said on camera after a reset.
    """
    import shutil
    import tempfile

    from aether.hotel.context import Subject
    from aether.hotel.db import SHIPPED_DB_PATH
    from aether.hotel.router import subject_of

    copy = Path(tempfile.mkdtemp()) / "fresh.db"
    shutil.copy(SHIPPED_DB_PATH, copy)
    store = HotelStore(path=copy)
    runner = ToolRunner(Trace(), store, tools=HOTEL_TOOLS)
    subject = Subject()
    heard: list[tuple[str | None, str | None]] = []
    try:
        for number, (said, _quoted) in enumerate(SCRIPT_TURNS, 1):
            decision = route(said, subject)
            if decision is None:
                heard.append((None, None))
                continue
            result = runner.run(decision.tool, gen="demo", turn_id=number, is_valid=lambda: True,
                                **decision.params)
            heard.append((decision.tool, render(result)))
            if number != BARGE_IN_TURN:
                subject.remember(*subject_of(decision, said))
    finally:
        if store._bookings is not None:
            store.bookings.close()
    return heard


@pytest.fixture(scope="module")
def the_call():
    return _run_the_call()


@pytest.mark.parametrize("index", range(len(SCRIPT_TURNS)), ids=[q for q, _ in SCRIPT_TURNS])
def test_the_call_says_exactly_what_the_sheet_quotes(the_call, index):
    said, quoted = SCRIPT_TURNS[index]
    tool, spoken = the_call[index]
    assert tool is not None, (
        f"turn {index + 1} ({said!r}) no longer routes -- on camera it would reach the model"
    )
    assert spoken == quoted


def test_turn_seven_books_what_the_caller_heard_not_what_they_talked_over(the_call):
    """"it" means the executive suite from turn 6, not the vegetarian list from turn 5 -- which was
    talked over, never heard, and so never became the subject."""
    tool, spoken = the_call[6]
    assert tool == "reserve_room"
    assert "Executive Suite" in spoken


def test_turn_seven_only_works_because_of_conversational_memory():
    """With no conversation behind it, "book it" names nothing and must fall through to the model.
    That is what makes the booking a demonstration of memory rather than of keyword matching."""
    said, _quoted = SCRIPT_TURNS[6]
    assert route(said) is None


@pytest.mark.parametrize(("said", "quoted"), BANK_TURNS, ids=[q for q, _ in BANK_TURNS])
def test_every_quoted_answer_is_what_aether_actually_says(said, quoted):
    assert _spoken(said) == quoted


@pytest.mark.parametrize("said", [q for q, _ in BANK_TURNS], ids=[q for q, _ in BANK_TURNS])
def test_no_scripted_line_reaches_the_model(said):
    """The claim made out loud during the demo is that none of this comes from a language model.

    `route` returning a tool is what makes that true: the turn is answered from the database and the
    LLM is never called. If a rule is narrowed and a line starts falling through, the answer would
    still probably be fine -- and the claim would be false.
    """
    assert route(said) is not None


@pytest.mark.parametrize(("said", "quoted"), ALL_TURNS, ids=[q for q, _ in ALL_TURNS])
def test_every_quoted_answer_is_speakable(said, quoted):
    """It goes to Rime as words. A digit or a symbol on this sheet is a mispronunciation on camera."""
    assert not re.search(r"\d", quoted), f"digits reach the voice as-is: {quoted!r}"
    assert not re.search(r"[%$£₹#*_`~<>{}\[\]]", quoted), f"unspeakable symbol in: {quoted!r}"


def test_every_room_number_quoted_in_the_script_exists():
    """A demo that asks about a room the hotel does not have gets an apology, not an answer.

    The one deliberate exception is "nine nine nine", which is turn 8 of the call and exists
    precisely to show AETHER refusing to invent a room. It is exempted by number, below, and only
    while the room genuinely does not exist.
    """
    spoken_digits = {"oh": "0", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                     "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
    real = {r.number for r in STORE.rooms()}

    for said, _ in ALL_TURNS:
        match = re.search(r"room ((?:\w+ ){2}\w+)", said.lower())
        if not match:
            continue
        words = match.group(1).split()
        if not all(w in spoken_digits for w in words):
            continue
        number = "".join(spoken_digits[w] for w in words)
        if number == "999":
            # Turn 8 asks about it ON PURPOSE, to show AETHER refusing to invent a room. The
            # exemption is for this one number only, and it has to earn it: the room must really
            # not exist, or the beat would be demonstrating nothing.
            assert number not in real, "room 999 exists now -- turn 8 no longer shows a refusal"
            continue
        assert number in real, f"{said!r} asks about room {number}, which does not exist"


def test_the_barge_in_answer_is_long_enough_to_talk_over():
    """The rehearsed beat needs an answer still playing when the next question starts. Turn 5 is the
    one the sheet tells you to interrupt, so it is the one that has to stay long."""
    said, quoted = SCRIPT_TURNS[4]
    assert "vegetarian" in said.lower(), f"turn 5 is no longer the vegetarian question: {said!r}"
    assert len(quoted.split()) >= 20, f"turn 5 is now too short to barge in on: {quoted!r}"


def test_the_turn_after_the_barge_in_changes_the_subject():
    """The brief's acceptance case is that the caller changes ONE PART OF THE REQUEST and gets an
    answer to the revised question -- not that they repeat themselves. Turn 6 has to be a different
    subject from turn 5, or the beat demonstrates a retry rather than a recovery."""
    interrupted, _ = SCRIPT_TURNS[4]
    revised, _ = SCRIPT_TURNS[5]
    assert route(interrupted).tool != route(revised).tool, (
        f"turn 6 ({revised!r}) asks the same kind of question as turn 5 ({interrupted!r})"
    )


def test_the_script_never_quotes_a_guest_name():
    """A reservation lookup deliberately never speaks the guest's name, and the sheet must not
    reintroduce one by hand -- it would be a fabricated hotel record read aloud during judging."""
    names = {g.name.split()[0].lower() for g in _guests()}
    page = SCRIPT.read_text(encoding="utf-8").lower()
    for first in names:
        assert first not in re.findall(r"[a-z]+", page), f"guest name {first!r} appears on the sheet"


def _guests():
    import sqlite3
    from aether.hotel import DEFAULT_DB_PATH
    db = sqlite3.connect(f"file:{DEFAULT_DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        rows = db.execute("select full_name from guests").fetchall()
    finally:
        db.close()
    return [type("G", (), {"name": r[0]})() for r in rows]


# --- the scenes, not just the tables --------------------------------------------------------------

def test_every_language_line_the_scenes_quote_is_what_the_code_speaks():
    """The scene-by-scene script quotes four sentences that come from `aether/lang`, not from the
    router: the opening question, the greeting, the offer of the language list, and the Hindi
    acknowledgement. They are quoted in full on the page, so they are checked in full here -- a
    presenter waiting for a sentence the system no longer says is a dead second on camera."""
    from aether.lang import ACKNOWLEDGEMENT, ENGLISH, HOTEL_GREETING, SELECT_PROMPT, language_offer

    page = SCRIPT.read_text(encoding="utf-8")
    for line in (SELECT_PROMPT, HOTEL_GREETING["eng"], language_offer(ENGLISH),
                 ACKNOWLEDGEMENT["hin"], HOTEL_GREETING["spa"]):
        assert line in page, f"the script no longer quotes, word for word: {line!r}"


def test_the_line_that_starts_the_language_switch_really_starts_it():
    """Scene 8 says "Can we switch language?" to get AETHER to ASK -- because an answer to a closed
    question is heard with a vocabulary hint, and "Hindi" without one came back as "in the"."""
    from aether.lang import asks_for_options

    assert asks_for_options("Can we switch language?")


# --- the screenplay: the part the presenter actually reads -------------------------------------------

def _screenplay_lines(prefix: str) -> list[str]:
    """Every line in the screenplay that starts with `prefix`, with the prefix removed."""
    page = SCRIPT.read_text(encoding="utf-8")
    body = page.split("<!-- SCREENPLAY:BEGIN -->", 1)[1].split("<!-- SCREENPLAY:END -->", 1)[0]
    return [line.strip()[len(prefix):].strip() for line in body.splitlines()
            if line.strip().startswith(prefix)]


def test_every_line_the_screenplay_gives_aether_is_one_it_really_says():
    """The screenplay is what gets read on camera, so it is checked like the tables are. Each
    **AETHER:** line must be a sentence the system actually produces -- a routed answer from the
    call, a Hindi answer, or one of the language sentences from `aether/lang`."""
    from aether.lang import (ACKNOWLEDGEMENT, ENGLISH, HOTEL_GREETING, LINE_CHECK, SELECT_PROMPT,
                             language_offer)

    allowed = ({answer for _q, answer in SCRIPT_TURNS}
               | {answer for _c, _q, answer in HINDI_TURNS}
               | {SELECT_PROMPT, HOTEL_GREETING["eng"], HOTEL_GREETING["spa"],
                  language_offer(ENGLISH), ACKNOWLEDGEMENT["hin"], LINE_CHECK})
    lines = _screenplay_lines("**AETHER:**")
    assert len(lines) >= 12, f"the screenplay looks truncated: {len(lines)} AETHER lines"
    for line in lines:
        assert line in allowed, f"the screenplay has AETHER say something it never says: {line!r}"


def test_the_screenplay_asks_every_scripted_question_in_order():
    """The presenter reads the screenplay, not the table. It must ask the same questions, in the
    same order the conversation test replays -- turn 7's "it" depends on that order."""
    said = _screenplay_lines("**YOU:**")
    positions = []
    for question, _answer in SCRIPT_TURNS:
        assert question in said, f"the screenplay never asks {question!r}"
        positions.append(said.index(question))
    assert positions == sorted(positions), "the screenplay asks the questions in a different order"


def test_the_screenplay_puts_the_talked_over_answer_where_the_barge_in_happens():
    """Turn 5 is interrupted, so the screenplay shows it cut off -- with its own label, so the
    exact-answer check above never mistakes a deliberately partial line for a wrong one."""
    page = SCRIPT.read_text(encoding="utf-8")
    assert "**AETHER (you talk over this):**" in page
