"""Answers the model made up, remembered so it does not make up a different one tomorrow.

THE PROBLEM THIS SOLVES. A caller asks "do you have a rooftop terrace?". No row holds that, so the
model answers plausibly -- and it is allowed to (RULES.md R8b.3). The next caller asks the same
thing and gets a *different* plausible answer. One says the pool is on the roof, the other says
there isn't one. Neither is grounded, and the hotel now contradicts itself.

So a model answer to a hotel question is written down and reused. The second caller gets the first
caller's answer, instantly and with no model call at all.

THE PROBLEM THIS DELIBERATELY DOES NOT SOLVE, and the distinction matters more than the feature:

    a remembered answer is CONSISTENT. It is not TRUE.

Nothing here verifies that the hotel has a rooftop terrace. Promoting a guess straight into the hotel's
authoritative facts would let the model quietly write policy, and the manager would never know which
of their own rules they had never approved. So learned answers live in their OWN table, carry
`confirmed = 0`, and are reported separately by `scripts/review_learned.py`. A human turns a guess
into a fact; this file only stops the guess changing shape between calls. They are not even in the
same database as the hotel, so a guess cannot be joined to a price by accident.

Matching is EXACT on the normalised question -- no fuzzy nearest-neighbour. A near-miss asks the
model again, which costs a second; a bad fuzzy hit answers a question the caller did not ask, which
costs the claim. The same trade the router makes with ambiguous keywords.

Kept in its OWN file (`aether_learned.db`, beside whichever hotel database is in use), never in the
hotel's -- see `default_learned_path()` for why that is about more than locking.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import LIVE_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_answers (
    id          INTEGER PRIMARY KEY,
    question    TEXT NOT NULL,      -- normalised, and the lookup key
    asked_as    TEXT NOT NULL,      -- what the caller actually said, for review
    answer      TEXT NOT NULL,
    language    TEXT NOT NULL,
    confirmed   INTEGER NOT NULL DEFAULT 0,   -- 0 = the model's guess, 1 = a human agreed
    times_used  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE(question, language)
);
"""


def default_learned_path() -> Path:
    """Its own file, beside whichever hotel database is in use.

    IT SHARED THE HOTEL'S FILE AT FIRST, and two things made that wrong.

    The mechanical one: SQLite locks per file, so remembering an answer and taking a booking were
    two connections contending for one database. The suite made it visible -- 7.5 s teardowns
    waiting out the busy timeout, and one outright `database is locked` -- but the same contention
    is there on a live call, where it would cost seconds of silence on the telephone.

    The one that matters more: **a guess is not a hotel fact**, and that should be structural rather
    than enforced. Kept in a separate file it cannot be joined to a price, cannot be picked up by a
    schema dump of the hotel, and cannot survive `scripts/reset_hotel_db.py` deciding what the hotel
    is. `data/README.md` stays a description of the hotel, not of what a model once said about it.

    Follows `AETHER_HOTEL_DB` so a demo rehearsal or a test run on a scratch copy gets its own
    learned answers beside it and never writes the developer's -- but reads that variable directly
    rather than calling `default_db_path()`, which has a SIDE EFFECT: it creates the working copy
    from the shipped database when one is missing. `scripts/reset_hotel_db.py` deletes the working
    copy and then asks for this path, and with the side effect it recreated the file it had just
    removed. Naming a file is a question, not an action.
    """
    override = (os.environ.get("AETHER_LEARNED_DB") or "").strip()
    if override:
        return Path(override)
    hotel = (os.environ.get("AETHER_HOTEL_DB") or "").strip()
    return (Path(hotel) if hotel else LIVE_DB_PATH).with_name("aether_learned.db")


def _authorizer(action: int, arg1: str | None, arg2: str | None,
                _db: str | None, _trigger: str | None) -> int:
    """Refuse every write outside `learned_answers`, at the driver.

    BELT AND BRACES, kept after the store moved to its own file. Separation already means a guess
    cannot reach a price; this means it stays true if someone later points `AETHER_LEARNED_DB` at
    the hotel database, which is a one-line mistake with no other symptom. `bookings.py` narrows its
    own connection the same way, and the guarantee is worth having as a property of the system
    rather than of one module's good behaviour.

    Reads stay open -- this never needs them, but denying reads would mean reasoning about the
    schema lookups SQLite does on its own behalf.
    """
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_UPDATE):
        return sqlite3.SQLITE_OK if arg1 == "learned_answers" else sqlite3.SQLITE_DENY
    # CREATE is denied too, and not as an afterthought: without it, "make a new table" would be the
    # way around every denial above.
    if action in (sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
                  sqlite3.SQLITE_ALTER_TABLE):
        return sqlite3.SQLITE_OK if arg1 == "learned_answers" else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@dataclass(frozen=True)
class Learned:
    question: str
    asked_as: str
    answer: str
    language: str
    confirmed: bool
    times_used: int
    created_at: str


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace -- the lookup key.

    Deliberately the same shape as the router's own normalisation, so "Do you have a rooftop terrace?"
    and "do you have a rooftop terrace" are one question and "is the terrace covered" is another.
    """
    return " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())


class LearnedAnswers:
    """The store. One connection, to a database of its own."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = default_learned_path()
        self.path = Path(path)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # The schema runs first and the authorizer is installed after it. `executescript` issues an
        # implicit COMMIT, which the authorizer would have to be taught about for no benefit --
        # `SCHEMA` is a constant in this file and creates only the one table the authorizer allows.
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._db.set_authorizer(_authorizer)

    def close(self) -> None:
        self._db.close()

    def recall(self, question: str, language: str) -> str | None:
        """The answer given to this exact question before, or None.

        Bumps `times_used`, because an answer the hotel has now given six times is the one a manager
        should look at first -- reviewing by frequency beats reviewing by chance.
        """
        key = normalise(question)
        if not key:
            return None
        row = self._db.execute(
            "SELECT id, answer FROM learned_answers WHERE question = ? AND language = ?",
            (key, language),
        ).fetchone()
        if row is None:
            return None
        self._db.execute(
            "UPDATE learned_answers SET times_used = times_used + 1 WHERE id = ?", (row["id"],)
        )
        self._db.commit()
        return row["answer"]

    def remember(self, question: str, answer: str, language: str) -> bool:
        """Write down what the model said. Returns whether anything was stored.

        `INSERT OR IGNORE`, so the FIRST answer to a question wins and later calls do not rewrite
        it. That is the point: rewriting would reintroduce exactly the drift this exists to stop.
        A confirmed answer is likewise never overwritten by a fresh guess.
        """
        key, answer = normalise(question), (answer or "").strip()
        if not key or not answer:
            return False
        cur = self._db.execute(
            "INSERT OR IGNORE INTO learned_answers "
            "(question, asked_as, answer, language, confirmed, times_used, created_at) "
            "VALUES (?,?,?,?,0,0,?)",
            (key, str(question).strip(), answer, language,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self._db.commit()
        return cur.rowcount > 0

    def all(self, *, unconfirmed_only: bool = False) -> list[Learned]:
        sql = "SELECT * FROM learned_answers"
        if unconfirmed_only:
            sql += " WHERE confirmed = 0"
        sql += " ORDER BY times_used DESC, id"
        return [
            Learned(r["question"], r["asked_as"], r["answer"], r["language"],
                    bool(r["confirmed"]), r["times_used"], r["created_at"])
            for r in self._db.execute(sql)
        ]

    def confirm(self, question: str, language: str) -> bool:
        """A human agrees. The answer keeps being given; it stops being flagged as a guess."""
        cur = self._db.execute(
            "UPDATE learned_answers SET confirmed = 1 WHERE question = ? AND language = ?",
            (normalise(question), language),
        )
        self._db.commit()
        return cur.rowcount > 0

    def forget(self, question: str, language: str) -> bool:
        """A human disagrees. The next caller gets a fresh answer from the model."""
        cur = self._db.execute(
            "DELETE FROM learned_answers WHERE question = ? AND language = ?",
            (normalise(question), language),
        )
        self._db.commit()
        return cur.rowcount > 0
