"""Add the hotel facts a real caller asks for and this database could not answer.

    python scripts/expand_hotel_db.py            # apply
    python scripts/expand_hotel_db.py --check    # report only, change nothing

**Idempotent.** Safe to run twice; it creates what is missing and leaves what exists.

WHY A NEW TABLE RATHER THAN PROSE IN A COLUMN. The obvious way to answer "do you have parking?" is
to store the sentence. That would be English-only, and this product answers in three languages from
one set of facts -- storing the sentence would put English inside the fact store and leave Hindi and
Spanish with nothing to render. So `hotel_policies` holds **structured** values (available, fee,
hours, options) and each language's renderer builds its own sentence. A price or a yes/no lives once;
three languages phrase it.

Floors, room counts and the like are deliberately NOT stored. They are derivable from `rooms`, and a
stored copy is a second source of truth that can contradict the first -- exactly the drift this
project keeps a single database to avoid.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "aether_hotel.db"

TABLE_BOOKINGS = """
-- Restaurant table reservations. A separate table from `reservations`, which is for ROOMS: a table
-- booking has a party size and a sitting, not a check-in and a check-out, and forcing both into one
-- shape would mean nullable columns that only make sense half the time.
CREATE TABLE IF NOT EXISTS table_bookings (
    booking_id   INTEGER PRIMARY KEY,
    guest_id     INTEGER REFERENCES guests(guest_id),
    party_size   INTEGER NOT NULL CHECK(party_size > 0),
    sitting      TEXT NOT NULL,          -- "HH:MM", the hour the table is held from
    booked_for   TEXT NOT NULL,          -- ISO date
    status       TEXT NOT NULL CHECK(status IN('confirmed','seated','cancelled')),
    created_at   TEXT NOT NULL
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS hotel_policies (
    topic        TEXT PRIMARY KEY,   -- machine key: parking, wifi, pets, ...
    available    INTEGER NOT NULL,   -- 0 or 1. 0 means "we do not offer this", which is an answer
    fee_inr      REAL,               -- NULL when free or not applicable
    hours        TEXT,               -- "HH:MM-HH:MM" or "24 hours"; NULL when not time-bound
    limit_hours  INTEGER,            -- notice period, e.g. free cancellation up to N hours before
    options      TEXT                -- comma-separated machine keys, e.g. "card,cash,upi"
);
"""

# Internally consistent with the rest of the hotel, which matters more than being extensive:
#  * breakfast is chargeable here AND appears as an amenity on the two suites -- both are true, the
#    suites include it and everyone else pays;
#  * the property is non-smoking and does not take pets, which are answers, not gaps;
#  * airport transfer is priced in the same currency as everything else (INR).
POLICIES = [
    # topic,             available, fee,    hours,          limit, options
    ("parking",              1,     None,   "24 hours",     None,  None),
    ("wifi",                 1,     None,   "24 hours",     None,  None),
    ("breakfast",            1,     450.0,  "07:00-10:30",  None,  None),
    ("pets",                 0,     None,   None,           None,  None),
    ("smoking",              0,     None,   None,           None,  None),
    ("children",             1,     None,   None,           None,  None),
    ("airport_transfer",     1,     1500.0, None,           None,  None),
    ("early_check_in",       1,     None,   None,           None,  None),
    ("late_check_out",       1,     1000.0, None,           None,  None),
    ("luggage_storage",      1,     None,   "24 hours",     None,  None),
    ("accessibility",        1,     None,   None,           None,  None),
    ("cancellation",         1,     None,   None,           24,    None),
    ("payment",              1,     None,   None,           None,  "card,cash,upi"),
    ("currency_exchange",    0,     None,   None,           None,  None),
    ("laundry",              1,     None,   "08:00-20:00",  None,  None),

    # --- 2026-09-10: the second pass, driven by what callers actually ask ---------------------
    # Every one of these was a question the router could route and the database could not answer,
    # so the turn fell through to the model. That is not wrong -- R8b.3 says the model may answer
    # what the database cannot -- but a fact the hotel definitely knows should not be improvised
    # differently on two calls. Facilities, timings and the two money questions a front desk hears
    # every day now have one authoritative row each.
    ("swimming_pool",        1,     None,   "06:00-20:00",  None,  None),
    ("gym",                  1,     None,   "24 hours",     None,  None),
    ("spa",                  1,     2500.0, "10:00-20:00",  None,  None),
    ("extra_bed",            1,     1500.0, None,           None,  None),
    ("doctor_on_call",       1,     None,   "24 hours",     None,  None),
    ("taxi_booking",         1,     None,   "24 hours",     None,  None),
    ("conference_room",      1,     8000.0, None,           None,  None),
    ("power_backup",         1,     None,   "24 hours",     None,  None),
    # Timings, not offers. "Yes, we offer a bar free of charge" is what the generic template would
    # produce, so each of these takes its own branch in every renderer.
    ("restaurant",           1,     None,   "07:00-23:00",  None,  None),
    ("bar",                  1,     None,   "17:00-23:30",  None,  None),
    # Money and paperwork at the desk. `options` carries machine keys, exactly as `payment` does,
    # so each language names the documents itself rather than storing an English list.
    ("deposit",              1,     5000.0, None,           None,  None),
    ("id_proof",             1,     None,   None,           None,  "passport,aadhaar,driving_licence"),
]


def apply(check_only: bool = False) -> int:
    if not DB.exists():
        print(f"database not found: {DB}")
        return 1

    if check_only:
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(str(DB))
    try:
        have_table = con.execute(
            "select count(*) from sqlite_master where type='table' and name='hotel_policies'"
        ).fetchone()[0]
        existing = set()
        if have_table:
            existing = {r[0] for r in con.execute("select topic from hotel_policies")}

        wanted = {p[0] for p in POLICIES}
        missing = sorted(wanted - existing)

        have_bookings = con.execute(
            "select count(*) from sqlite_master where type='table' and name='table_bookings'"
        ).fetchone()[0]
        print(f"table_bookings table exists : {bool(have_bookings)}")
        print(f"hotel_policies table exists : {bool(have_table)}")
        print(f"topics present              : {len(existing)}")
        print(f"topics that would be added  : {len(missing)}" + (f" -> {missing}" if missing else ""))

        if check_only:
            return 0

        con.executescript(SCHEMA)
        con.executescript(TABLE_BOOKINGS)
        con.executemany(
            "INSERT OR IGNORE INTO hotel_policies "
            "(topic, available, fee_inr, hours, limit_hours, options) VALUES (?,?,?,?,?,?)",
            POLICIES,
        )
        con.commit()
        total = con.execute("select count(*) from hotel_policies").fetchone()[0]
        print(f"hotel_policies now holds    : {total} topics")
        return 0
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report only; change nothing")
    sys.exit(apply(check_only=ap.parse_args().check))


if __name__ == "__main__":
    main()
