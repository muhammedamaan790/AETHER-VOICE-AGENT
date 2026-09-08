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


SCRIPT_TURNS = _rows("SCRIPT")
BANK_TURNS = _rows("BANK")
ALL_TURNS = SCRIPT_TURNS + BANK_TURNS


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
    assert len(SCRIPT_TURNS) == 12, f"expected the twelve-turn call, parsed {len(SCRIPT_TURNS)}"
    assert len(BANK_TURNS) >= 12, f"the fallback bank looks truncated: {len(BANK_TURNS)} rows"


@pytest.mark.parametrize(("said", "quoted"), ALL_TURNS, ids=[q for q, _ in ALL_TURNS])
def test_every_quoted_answer_is_what_aether_actually_says(said, quoted):
    assert _spoken(said) == quoted


@pytest.mark.parametrize("said", [q for q, _ in ALL_TURNS], ids=[q for q, _ in ALL_TURNS])
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

    The one deliberate exception is the "nine nine nine" line in the prose, which exists precisely to
    show AETHER refusing to invent a room; it is not in either table.
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
        assert number in real, f"{said!r} asks about room {number}, which does not exist"


def test_the_barge_in_answer_is_long_enough_to_talk_over():
    """The rehearsed beat needs an answer still playing when the next question starts. Turn 6 is the
    one the sheet tells you to interrupt, so it is the one that has to stay long."""
    said, quoted = SCRIPT_TURNS[5]
    assert "vegetarian" in said.lower(), f"turn 6 is no longer the vegetarian question: {said!r}"
    assert len(quoted.split()) >= 20, f"turn 6 is now too short to barge in on: {quoted!r}"


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
