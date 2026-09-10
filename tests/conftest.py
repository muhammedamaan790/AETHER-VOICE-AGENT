"""The test suite never touches the shipped hotel database.

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
import tempfile
from pathlib import Path

_SHIPPED = Path(__file__).resolve().parents[1] / "data" / "aether_hotel.db"

if _SHIPPED.exists() and not os.environ.get("AETHER_HOTEL_DB"):
    _scratch = Path(tempfile.mkdtemp(prefix="aether-tests-")) / "aether_hotel.db"
    shutil.copy(_SHIPPED, _scratch)
    os.environ["AETHER_HOTEL_DB"] = str(_scratch)

    @atexit.register
    def _remove_scratch_database() -> None:
        shutil.rmtree(_scratch.parent, ignore_errors=True)
