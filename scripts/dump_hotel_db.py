"""Regenerate `data/hotel_db/schema.sql` and `seed.sql` from `data/aether_hotel.db`.

    python scripts/dump_hotel_db.py

The database is the single source of truth. These two files are a readable, diffable VIEW of it,
for anyone reviewing the repository without opening SQLite. `tests/test_hotel_db_files.py`
regenerates them in memory and fails if the committed copies differ -- so after changing the
database, run this and commit all three together.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aether.hotel.db import SHIPPED_DB_PATH, dump_sql  # noqa: E402


def main() -> None:
    out = SHIPPED_DB_PATH.parent / "hotel_db"
    out.mkdir(exist_ok=True)
    schema, seed = dump_sql(SHIPPED_DB_PATH)
    for name, text in (("schema.sql", schema), ("seed.sql", seed)):
        # LF explicitly: on Windows a text-mode write turns every newline into CRLF, and the drift
        # test would then fail on line endings rather than on content.
        with open(out / name, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"wrote {out / name}  ({text.count(chr(10))} lines)")


if __name__ == "__main__":
    main()
