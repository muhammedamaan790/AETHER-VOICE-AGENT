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
