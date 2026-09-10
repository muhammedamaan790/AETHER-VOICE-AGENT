# Data

Two datasets, and they are not the same kind of thing. `aether_hotel.db` is the **hotel product**'s
source of truth and is real to the demo; the warehouse fixture below belongs to the older
warehouse-assistant path and is a hand-written constant.

---

# The hotel database — `aether_hotel.db`

**The source of truth for every hotel fact AETHER states.** SQLite, 12 tables and 3 views,
opened read-only for facts. If it is not in this file, AETHER does not say it.

**Bookings are the one exception, and a narrow one.** `aether/hotel/bookings.py` holds the only
read-write connection, behind a `sqlite3` authorizer that permits writes to `reservations`,
`table_bookings`, `guests` and the single column `rooms.status`, and refuses everything else at the
driver. A price, an allergen or a room number cannot be changed by any code path in this project.

| Table | Rows |
|---|---|
| `menu_items` | 12 (2 deliberately unavailable) |
| `menu_categories` | 5 — starters, mains, vegetarian mains, desserts, drinks |
| `rooms` | 50 — 101–510 across 5 floors |
| `room_types` | 5 — 6 500 to 15 000 INR a night |
| `hotel_services` | 6 |
| `hotel` | 1 — check-in 14:00, check-out 12:00, INR |
| `guests` / `reservations` | 4 / 3 |
| `hotel_policies` | **27** — parking, wi-fi, breakfast, pets, smoking, children, airport transfer, early check-in, late check-out, luggage storage, accessibility, cancellation, payment, currency exchange, laundry, **swimming pool, gym, spa, extra bed, doctor on call, taxi booking, conference room, power backup, restaurant hours, bar hours, deposit, ID at check-in** |
| `table_bookings` | 0 — written by `reserve_table`; a party size and a sitting, which a room reservation has no place for |
| `service_requests` / `restaurant_orders` / `restaurant_order_items` | 0 / 2 / 4 |

`hotel_policies` was added after an audit found that the commonest questions a hotel line receives
had no fact behind them at all. It stores **structured** values -- `available`, `fee_inr`, `hours`,
`limit_hours`, `options` -- and never a sentence. Storing "Yes, parking is free" would put English
inside the fact store and leave Hindi and Spanish with nothing to render; storing `available=1,
fee=NULL` lets three languages each build their own sentence from one fact. `available=0` is an
answer, not a gap: "we do not take pets" is exactly what a caller needs.

Floors and room counts are deliberately **not** stored. They are derived from `rooms`, because a
stored copy is a second source of truth that can contradict the first.

Views `available_menu`, `available_rooms` and `current_reservations` exist in the file; the code
reads the base tables and filters in Python, so availability logic is testable without a database
round-trip.

`schema.sql` and `seed.sql` in [`hotel_db/`](hotel_db/) are **generated from** the database by
`scripts/dump_hotel_db.py`, for reading it without opening SQLite, and a test fails if they drift.
(This paragraph used to call them "the files the database was built from". Nothing ever built it
from them, and by the time that was noticed they described a hotel with no policies and no way to
book a table.)

## Facts read-only, bookings fenced — both enforced by SQLite

[`aether/hotel/db.py`](../aether/hotel/db.py) reads through a `file:...?mode=ro` connection, so a
write there raises `OperationalError` from the driver. The one read-write connection lives in
[`aether/hotel/bookings.py`](../aether/hotel/bookings.py) behind a `sqlite3` authorizer that allows
writes to `reservations`, `table_bookings`, `guests` and the single column `rooms.status`, and denies
everything else mid-statement — so a price, an allergen or a room number cannot be changed by any
code path. Three tools write (`reserve_room`, `reserve_table`, `cancel_booking`) and are stamped
`mutates: true`; the rest are `mutates: false`.

**The running system never writes this committed file.** It works on `aether_hotel.live.db`, a
gitignored copy made on first use and refreshed when the shipped file is newer, so rehearsal
bookings cannot drift the hotel away from what the documents describe.
`python scripts/reset_hotel_db.py` discards it.

`state_version` now moves. It was stamped on every result from the start and sat at `0` while
nothing could write; each booking advances it, so a late answer is recognisable as describing a
hotel that has since changed.

## What changed when this replaced the Python fixture

Not cosmetic, and worth knowing when reading older traces or test names:

| | Old fixture | This database |
|---|---|---|
| Menu size | 29 dishes | 12 items |
| Chicken Kebab | 380 | 420 |
| Categories | starters, mains, desserts, drinks | + **vegetarian mains** as its own category |
| Spice level | a column, and a `find_by_spice` tool | **does not exist** |
| Sold out | seafood platter, gulab jamun | Fish Curry, Vegetable Samosa |

Because there is no spice column, `find_by_spice` and `spice_of` were removed rather than faked.
"Is it spicy?" now routes to `describe_item`, which reads the hotel's own description back. "Do you
have anything mild?" routes nowhere and reaches Gemini, which is given the whole menu.

Deterministic: no randomness, no clocks, no IDs derived from time. Two runs a week apart produce
byte-identical answers, which is what makes a demo rehearsable and a test meaningful.

No data here is real. It is synthetic and invented for the demo.

---

# Synthetic warehouse fixture

**Implemented.** The fixture itself lives in [`aether/warehouse/__init__.py`](../aether/warehouse/__init__.py)
as module-level constants, not as files in this directory — it is small enough that keeping it in
Python removes a loader, a parser and a failure mode, and makes it importable by tests directly.
This directory remains the documented home for any fixture that does outgrow that.

Deterministic, hand-written, and deliberately small — the smallest dataset that makes the
continuity problem obvious and testable. Not a warehouse management system (RULES.md R12).

## What it contains

| Records | Count |
|---|---|
| Products | 7 |
| Bins (aisles 3, 7, 9) | 7 |
| Inventory rows | 7 (one deliberately out of stock) |
| Orders | 12 — **8 high priority**, 4 normal |

## Sizing constraint, and why these numbers

The brief's demo sentence is *"8 priority orders, actually only aisle 9"*, so the fixture has to
make that a **genuine** refinement rather than a cosmetic one:

- 8 high-priority pending orders, so the opening answer is a real list;
- exactly **3** of them in aisle 9, so narrowing discards 5 and the subset is strictly smaller;
- spread across three aisles, so "aisle 9" is a real constraint and not the only option.

`SKU-1003` is out of stock on purpose: an inventory lookup that has to say "none on hand" is a
different answer shape from one that returns a quantity, and both need to be exercised.

Small enough that every run is reproducible by hand; no randomness, no clocks, and no IDs derived
from time, so two runs a week apart are byte-identical.

## Mutation

`WarehouseStore` is the only mutable state and the only thing that may change it. The model never
receives a store, never sees a record dict it could edit, and cannot reach a mutation except
through the named tools in [`aether/tools/`](../aether/tools/__init__.py). `state_version`
increments on every mutation and is stamped onto every tool result, so a result carries the
identity of the world it describes.

No data here is real. It is synthetic and invented for the demo.
