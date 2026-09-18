"""The test suite never touches the shipped hotel database, and every test starts from a hotel
that has remembered nothing.

IT DID, ONCE, and that is why this file exists. The booking tools landed on 2026-09-10, the suite
ran, and `data/aether_hotel.db` came back with two rooms marked reserved, two extra guests and two
table bookings in it. `git checkout` undid the damage, but nothing had prevented it and nothing
would have reported it -- the tests all passed, and the committed data quietly drifted.

So every run gets its own copy, made once per session before any test module is imported. The copy
is what `AETHER_HOTEL_DB` points at, and `default_db_path()` reads that variable at construction
time rather than at import, which is the only reason this can be set from here at all.

WHY AT MODULE LEVEL RATHER THAN IN A FIXTURE. `aether.hotel.router` and `aether.hotel.clarify` build
a `HotelStore` at import to compile their keyword tables, and pytest imports test modules -- and so
those -- before any fixture runs. A fixture would be too late for exactly the objects most likely to
be handed to a booking tool. conftest is imported first; that is the whole trick.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_SHIPPED = Path(__file__).resolve().parents[1] / "data" / "aether_hotel.db"

if _SHIPPED.exists() and not os.environ.get("AETHER_HOTEL_DB"):
    _scratch = Path(tempfile.mkdtemp(prefix="aether-tests-")) / "aether_hotel.db"
    shutil.copy(_SHIPPED, _scratch)
    os.environ["AETHER_HOTEL_DB"] = str(_scratch)

    @atexit.register
    def _remove_scratch_database() -> None:
        shutil.rmtree(_scratch.parent, ignore_errors=True)


@pytest.fixture(autouse=True)
def _forget_learned_answers():
    """Every test gets a hotel that has never answered a question before.

    WHY THIS IS NEEDED, and it is a property of the feature rather than an artifact of the harness.
    `aether/hotel/learned.py` writes down a model answer and replays it to the next caller asking
    the same thing, skipping the model entirely. That is the point of it. But the suite reuses a
    handful of questions across modules -- "what is your star rating" appears in a dozen tests --
    so without this, the first test to ask one teaches the database the answer and every later test
    asking it is served from recall. Ten streaming and telemetry tests failed exactly that way:
    they asserted on what the model streamed, and the model was never called.

    Cleared BETWEEN tests, not within one, because a test that needs the recall path needs two
    callers inside the same test -- see `test_learned_answers.py`.

    A missing file is the normal case: most tests never open the store at all.

    THIS RUNS AFTER EVERY TEST IN THE SUITE, so it is written to cost nothing when there is nothing
    to do. Two gates: the module is absent from `sys.modules` for almost every test, and the file
    only exists once something has opened the store. Learned answers live in their own database
    (`aether/hotel/learned.py`), so clearing them contends with nothing -- an earlier version shared
    the hotel's file and spent up to 7.5 s per teardown waiting out SQLite's busy timeout.
    """
    yield
    if "aether.hotel.learned" not in sys.modules:
        return                                  # nothing in this process has ever opened the store
    from aether.hotel.learned import default_learned_path

    path = default_learned_path()
    if not path.exists():
        return
    db = sqlite3.connect(str(path))
    try:
        db.execute("DELETE FROM learned_answers")
        db.commit()
    except sqlite3.OperationalError:
        pass                                    # no such table: nothing was ever remembered
    finally:
        db.close()
