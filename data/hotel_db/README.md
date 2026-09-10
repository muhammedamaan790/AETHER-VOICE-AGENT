# `data/hotel_db/` — a readable view of the hotel database

**Generated, not maintained.** `schema.sql` and `seed.sql` are dumped from
[`../aether_hotel.db`](../aether_hotel.db) by `scripts/dump_hotel_db.py`, so a reviewer can read the
whole hotel without opening SQLite. The database is the single source of truth; these files are a
view of it.

`tests/test_hotel_db_files.py` regenerates both in memory and fails if the committed copies differ.
After changing the database:

```bash
python scripts/dump_hotel_db.py
```

**Why this changed (2026-09-10).** `schema.sql` used to be a one-line note and `seed.sql` a hand-kept
dump that predated `hotel_policies` and `table_bookings` — a second, older definition of a database
whose whole design is that there is only one. A reviewer reading it would have seen a hotel with no
policies and no way to book a table.

## What is in it

Thirteen tables and three views. Synthetic data only — no real person appears anywhere.

| Table | Rows | Written by AETHER? |
|---|---|---|
| `hotel` | 1 | no |
| `room_types` | 5 | no |
| `rooms` | 50 — 101–110 through 501–510 | `status` column only, by a booking |
| `menu_categories` / `menu_items` | 5 / 12 | no |
| `hotel_services` | 6 | no |
| `hotel_policies` | 27 | no |
| `guests` | 4 | yes — one row per telephone booking |
| `reservations` | 3 | yes — `reserve_room`, `cancel_booking` |
| `table_bookings` | 0 | yes — `reserve_table`, `cancel_booking` |
| `service_requests` / `restaurant_orders` / `restaurant_order_items` | 0 / 2 / 4 | no |

Views: `available_rooms`, `available_menu`, `current_reservations`.

"Written by AETHER" is enforced by SQLite, not by convention: the only read-write connection sits
behind an authorizer that refuses every other write (`aether/hotel/bookings.py`). And the running
system never writes this committed file at all — it works on `data/aether_hotel.live.db`, a
gitignored copy made on first use.

## Room types

Standard King · Standard Twin · Deluxe King · Executive Suite · Family Suite
