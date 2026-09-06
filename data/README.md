# Synthetic warehouse fixture

TODO (Day 2). Deterministic, hand-written, and deliberately small — the smallest dataset that makes
the continuity problem obvious and testable. Not a warehouse management system (RULES.md R12).

Planned records: orders, order priorities, bins, aisle locations, inventory, pick status.

Sizing constraint: enough rows that "8 priority orders, actually only aisle 9" is a meaningful
refinement with genuine salvage, few enough that every run is reproducible by hand.

No data in this directory is real. It is synthetic and invented for the demo.
