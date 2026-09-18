"""Throw away what rehearsal wrote, so the next run starts from the shipped hotel.

    python scripts/reset_hotel_db.py

RUN IT BEFORE RECORDING. Two things accumulate across rehearsals and both follow you onto camera:

* **Bookings**, in `data/aether_hotel.live.db`. A room booked in rehearsal is still reserved on
  camera, and "forty one rooms free" on the demo sheet becomes forty.
* **Learned answers**, in `data/aether_learned.db` -- what the model said about things the hotel has
  no row for. These are replayed verbatim to the next caller, which is the point of them, but it
  means a phrasing you did not like in rehearsal is exactly what you get on the take.

The committed `data/aether_hotel.db` is never written by the running system, so this is always safe
-- the next call recreates the working copy from it, and the next unanswerable question is answered
afresh.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.hotel.db import LIVE_DB_PATH  # noqa: E402
from aether.hotel.learned import default_learned_path  # noqa: E402


def _remove(path, what: str, stuck: list) -> bool:
    """Delete one file. A locked file is REPORTED, not raised.

    Raising killed the whole reset on the second file and left the first already gone, so a
    presenter who had the worker running got half a reset and an error. Each file is independent:
    clearing the bookings is still worth doing when the learned answers are locked, and the exit
    code at the end says whether anything was missed.
    """
    if not path.exists():
        return False
    try:
        path.unlink()
    except PermissionError:
        # Windows will not delete a file another process has open.
        stuck.append(path.name)
        return False
    print(f"removed {path.name} -- {what}")
    return True


def main() -> None:
    stuck: list[str] = []
    removed = _remove(LIVE_DB_PATH, "the next run starts from the shipped database", stuck)
    removed |= _remove(default_learned_path(),
                       "the next unanswerable question is answered afresh", stuck)
    if stuck:
        raise SystemExit(
            f"{', '.join(stuck)} {'is' if len(stuck) == 1 else 'are'} in use -- stop the worker "
            f"and the console, then run this again")
    if not removed:
        print("nothing to reset -- no rehearsal data exists yet")


if __name__ == "__main__":
    main()
