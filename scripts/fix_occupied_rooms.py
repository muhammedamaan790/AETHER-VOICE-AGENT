"""Give every occupied room the booking that must exist behind it.

    python scripts/fix_occupied_rooms.py            # apply
    python scripts/fix_occupied_rooms.py --check    # report only, change nothing

**Idempotent.** Safe to run twice; it creates what is missing and leaves what exists.

THE PROBLEM. Five rooms carried `occupied` or `reserved` and only three had a row in `reservations`.
A hotel cannot be in that state: somebody is in room three zero five, so somebody booked it and is
leaving on some date. The database said the room was occupied and had no idea by whom or until when.

WHICH ROOMS COUNT, and the first version of this script got it wrong. Nine rooms are not available,
but only `occupied` (a guest is in it) and `reserved` (a guest is coming) mean a GUEST. The other
four are `housekeeping` and `maintenance` -- being cleaned, being repaired -- and inventing a
reservation for those put a guest in a room that is out of service, so "when will room five zero
eight be available?" answered "booked until the twenty-fifth" about a room nobody has booked. Those
rooms have no date, and saying so is the correct answer; `room_free_from` names the reason instead.

WHY IT MATTERS OUT LOUD. "When will room three zero five be available?" is a question a caller
actually asks, and the honest answer from that data was "it is occupied, and I do not have a date
for when it frees up" -- a hotel that cannot say when its own rooms free up. Six of the nine
occupied rooms answered that way. `room_free_from` reads `reservations.check_out`, so the gap was
not a rendering problem; the fact was missing.

WHAT THIS DOES NOT DO. It does not touch `rooms.status`, so no room changes hands and every count
quoted anywhere -- "forty one rooms free" on the demo sheet -- stays exactly as it was. It adds the
reservation and the guest that were always implied, and nothing else. Check-out dates are spread
across the coming days so the answers differ from room to room rather than all reading alike, which
is what a real occupancy chart looks like.

Guest names are invented, and that is safe here for the reason the rest of this project relies on:
nothing ever speaks a guest's name. `_reservation_for_room` deliberately returns dates and status
and not the name, and `tests/test_language.py` enforces it -- so these names exist only to satisfy
the foreign key, exactly as `Telephone booking` does for a booking taken on the phone.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "aether_hotel.db"

# The occupancy chart, keyed by room. Nights remaining is what varies, so that consecutive callers
# asking about different rooms get genuinely different answers rather than one repeated date.
#
# `checked_in` for rooms whose guest is in the building (`occupied`), `confirmed` for a room that is
# held but not yet arrived (`reserved`) -- the same two values the three existing rows use.
_STAY_NIGHTS = {"102": 2, "202": 1, "207": 6, "302": 3, "305": 4}

# A guest is in it, or is coming. `housekeeping` and `maintenance` are deliberately absent: those
# rooms are unavailable for a reason that has nothing to do with anybody's booking.
_HAS_A_GUEST = ("occupied", "reserved")


def _existing(con: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in con.execute(
        "SELECT rm.room_number FROM reservations rs JOIN rooms rm USING(room_id)"
        " WHERE rs.status IN ('checked_in', 'confirmed')")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report only; change nothing")
    args = ap.parse_args()

    if not DB.exists():
        raise SystemExit(f"no database at {DB}")

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        taken = con.execute(
            "SELECT room_id, room_number, status FROM rooms WHERE status IN (?, ?)"
            " ORDER BY room_number", _HAS_A_GUEST).fetchall()
        have = _existing(con)
        missing = [r for r in taken if str(r["room_number"]) not in have]

        out_of_service = con.execute(
            "SELECT COUNT(*) FROM rooms WHERE status NOT IN ('available', ?, ?)",
            _HAS_A_GUEST).fetchone()[0]
        print(f"{len(taken)} rooms hold a guest; {len(have)} have a reservation behind them.")
        print(f"{out_of_service} more are out of service (housekeeping, maintenance) and correctly "
              f"have none.")
        if not missing:
            print("Nothing to do -- every room with a guest already has a booking.")
            return
        print(f"{len(missing)} need one: "
              f"{', '.join(str(r['room_number']) for r in missing)}")
        if args.check:
            print("\n--check: nothing was written.")
            return

        today = date.today()
        added = 0
        for row in missing:
            number = str(row["room_number"])
            nights = _STAY_NIGHTS.get(number, 3)
            # Arrived a day or two ago for a guest who is in; arriving today for one who is not.
            checked_in = row["status"] == "occupied"
            check_in = today - timedelta(days=1 if checked_in else 0)
            cur = con.execute(
                "INSERT INTO guests (full_name, phone, email, language) VALUES (?,?,?,?)",
                (f"In-house guest {number}", None, None, "English"))
            con.execute(
                "INSERT INTO reservations (guest_id, room_id, check_in, check_out, adults,"
                " children, status, special_requests, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(cur.lastrowid), row["room_id"], check_in.isoformat(),
                 (check_in + timedelta(days=nights)).isoformat(), 2, 0,
                 "checked_in" if checked_in else "confirmed", None,
                 f"{check_in.isoformat()}T12:00:00"))
            added += 1
            print(f"  room {number}: {check_in} -> {check_in + timedelta(days=nights)} "
                  f"({'checked in' if checked_in else 'confirmed'})")
        con.commit()
        print(f"\nAdded {added} reservations. Every occupied room can now say when it frees up.")
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
