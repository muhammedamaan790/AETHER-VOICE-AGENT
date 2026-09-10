"""Throw away the working copy of the hotel, so the next run starts from the shipped database.

    python scripts/reset_hotel_db.py

RUN IT BEFORE RECORDING. Rehearsal bookings accumulate in `data/aether_hotel.live.db`: a room booked
in rehearsal is still reserved on camera, and "forty one rooms free" on the demo sheet becomes forty.
The committed `data/aether_hotel.db` is never written by the running system, so this is always safe
-- the next call recreates the working copy from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.hotel.db import LIVE_DB_PATH  # noqa: E402


def main() -> None:
    if not LIVE_DB_PATH.exists():
        print(f"nothing to reset -- {LIVE_DB_PATH.name} does not exist yet")
        return
    try:
        LIVE_DB_PATH.unlink()
    except PermissionError:
        # Windows will not delete a file another process has open.
        raise SystemExit(f"{LIVE_DB_PATH.name} is in use -- stop the worker and the console "
                         "first, then run this again") from None
    print(f"removed {LIVE_DB_PATH.name} -- the next run starts from the shipped database")


if __name__ == "__main__":
    main()
