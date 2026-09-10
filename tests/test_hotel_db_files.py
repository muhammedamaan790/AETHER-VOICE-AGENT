"""The database has one definition, and the running system never writes the committed copy.

Two sweep findings, one file:

1. **`data/hotel_db/*.sql` had drifted.** `schema.sql` was a one-line note and `seed.sql` a hand-kept
   dump with no `hotel_policies` and no `table_bookings` -- a second, older definition of a database
   whose entire design is that there is exactly one. Both are now generated from the database, and
   the first tests here regenerate them and compare.
2. **Rehearsals would have drifted the demo.** Bookings wrote into the committed database, so a room
   booked in rehearsal stayed reserved on camera and "forty one rooms free" on the demo sheet slowly
   stopped being true. The running system now works on a gitignored copy.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from aether.hotel import db

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "data" / "hotel_db"


# --- 1. one definition --------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "index"), [("schema.sql", 0), ("seed.sql", 1)])
def test_the_committed_sql_is_exactly_what_the_database_says(name: str, index: int) -> None:
    generated = db.dump_sql(db.SHIPPED_DB_PATH)[index]
    committed = (SQL_DIR / name).read_text(encoding="utf-8")
    assert committed == generated, (
        f"data/hotel_db/{name} no longer matches the database -- "
        f"run `python scripts/dump_hotel_db.py` and commit it"
    )


def test_the_dump_carries_the_tables_the_old_hand_kept_seed_was_missing() -> None:
    schema, seed = db.dump_sql(db.SHIPPED_DB_PATH)
    assert "table_bookings" in schema
    assert "hotel_policies" in schema
    assert 'INSERT INTO "hotel_policies"' in seed


def test_the_dump_is_deterministic() -> None:
    """A generator whose output changes between runs would make the drift test flaky rather than
    strict -- and a flaky strict test gets deleted."""
    assert db.dump_sql(db.SHIPPED_DB_PATH) == db.dump_sql(db.SHIPPED_DB_PATH)


# --- 2. the working copy --------------------------------------------------------------------------

@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    """A private shipped file and a private working copy, with the test-suite override removed."""
    shipped = tmp_path / "aether_hotel.db"
    shipped.write_bytes(db.SHIPPED_DB_PATH.read_bytes())
    live = tmp_path / "aether_hotel.live.db"
    monkeypatch.setattr(db, "SHIPPED_DB_PATH", shipped)
    monkeypatch.setattr(db, "LIVE_DB_PATH", live)
    monkeypatch.delenv("AETHER_HOTEL_DB", raising=False)
    return shipped, live


def _reservations(path: Path) -> int:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return int(con.execute("SELECT COUNT(*) FROM reservations").fetchone()[0])
    finally:
        con.close()


def test_the_running_system_uses_a_working_copy_made_from_the_shipped_file(isolated) -> None:
    shipped, live = isolated
    assert db.default_db_path() == live
    assert live.read_bytes() == shipped.read_bytes()


def test_a_booking_lands_in_the_working_copy_and_never_in_the_committed_file(isolated) -> None:
    from aether.hotel import HotelStore

    shipped, live = isolated
    before = shipped.read_bytes()

    store = HotelStore()                       # resolves default_db_path() -> the working copy
    store.bookings.reserve_room(room_type="Deluxe King")
    store.bookings.close()

    assert shipped.read_bytes() == before, "a booking wrote to the committed database"
    assert _reservations(live) == _reservations(shipped) + 1


def test_rehearsal_bookings_survive_the_next_run(isolated) -> None:
    """A working copy that reset itself on every start would make "book a room, hang up, call back
    and ask about it" impossible to demonstrate."""
    from aether.hotel import HotelStore

    shipped, live = isolated
    store = HotelStore()
    store.bookings.reserve_room(room_type="Deluxe King")
    store.bookings.close()

    assert db.default_db_path() == live
    assert _reservations(live) == _reservations(shipped) + 1


def test_a_newer_shipped_database_refreshes_the_working_copy(isolated) -> None:
    """A `git pull` or a schema change has to reach the running system. A copy that silently lacked
    `table_bookings` would fail on its first table booking."""
    shipped, live = isolated
    db.default_db_path()
    live.write_bytes(b"an old copy from before the last schema change")
    old = shipped.stat().st_mtime - 3600
    os.utime(live, (old, old))

    assert db.default_db_path() == live
    assert live.read_bytes() == shipped.read_bytes(), "the stale working copy was not refreshed"


def test_the_override_still_wins(isolated, monkeypatch, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere.db"
    monkeypatch.setenv("AETHER_HOTEL_DB", str(other))
    assert db.default_db_path() == other


def test_the_working_copy_is_gitignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/*.live.db" in ignore


def test_no_half_written_copy_is_left_behind(isolated) -> None:
    _shipped, live = isolated
    db.default_db_path()
    assert not live.with_name(live.name + ".partial").exists()


def test_the_reset_script_starts_the_next_run_from_the_shipped_file(isolated) -> None:
    import importlib.util

    shipped, live = isolated
    db.default_db_path()
    live.write_bytes(b"rehearsal state")
    time.sleep(0.01)

    spec = importlib.util.spec_from_file_location("reset_hotel_db",
                                                  ROOT / "scripts" / "reset_hotel_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LIVE_DB_PATH = live
    module.main()

    assert not live.exists()
    assert db.default_db_path() == live and live.read_bytes() == shipped.read_bytes()
