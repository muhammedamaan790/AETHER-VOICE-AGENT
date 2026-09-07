"""Synthetic warehouse fixture: the smallest dataset that makes the continuity problem visible.

Not a warehouse management system (RULES.md R12). Every record here is invented. The sizing is
driven by one demo sentence from `data/README.md`:

    "8 priority orders, actually only aisle 9"

so the fixture must make that a *genuine* refinement: enough priority orders that the first answer
is a real list, and a strict, non-trivial aisle-9 subset that a later constraint narrows it to.
That is why there are 8 priority orders spread across three aisles with 3 of them in aisle 9 --
few enough to verify by hand, many enough that "narrow it to aisle 9" actually discards something.

Two things live here and nothing else:

* **The records** -- orders, products, bins, inventory. Module-level constants, hand-written, in a
  fixed order. No randomness, no clocks, no IDs derived from time: two runs a week apart produce
  byte-identical results, which is what makes a fenced-vs-unfenced comparison meaningful.
* **`WarehouseStore`** -- the only mutable state, and the only way to change it.

The store is deliberately *not* handed to the model. The LLM never sees this module, never gets a
record dict, and cannot write to it: mutation happens exclusively through the tool functions in
`aether.tools`, which are ordinary Python. A model can choose *which* tool runs, never *what* the
data becomes.

`state_version` increments on every mutation and is stamped onto every tool result. It is how a
result proves which version of the world it was computed from -- so a late-arriving result is
recognisable as describing a world that has since moved on, independently of generation fencing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from ..errors import ToolLookupError


class OrderStatus(str, Enum):
    """Pick lifecycle. Deliberately four states -- there is no partial-pick model."""

    PENDING = "pending"
    PICKED = "picked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"


@dataclass(frozen=True)
class Product:
    sku: str
    name: str


@dataclass(frozen=True)
class Bin:
    bin_id: str
    aisle: int
    shelf: str


@dataclass(frozen=True)
class Order:
    """One pick. `status` is the only field the tools ever change."""

    order_id: str
    sku: str
    quantity: int
    priority: Priority
    status: OrderStatus = OrderStatus.PENDING


# --- the fixture ------------------------------------------------------------------------
#
# Hand-written and fixed. Read the aisle spread before changing anything: the demo depends on
# 8 high-priority orders existing, and on exactly 3 of them being in aisle 9.

PRODUCTS: tuple[Product, ...] = (
    Product("SKU-1001", "cordless drill"),
    Product("SKU-1002", "impact driver"),
    Product("SKU-1003", "socket set"),
    Product("SKU-2001", "safety goggles"),
    Product("SKU-2002", "work gloves"),
    Product("SKU-3001", "extension cord"),
    Product("SKU-3002", "LED work light"),
)

BINS: tuple[Bin, ...] = (
    Bin("B-03-A", 3, "A"),
    Bin("B-03-C", 3, "C"),
    Bin("B-07-A", 7, "A"),
    Bin("B-07-B", 7, "B"),
    Bin("B-09-A", 9, "A"),
    Bin("B-09-B", 9, "B"),
    Bin("B-09-D", 9, "D"),
)

# sku -> (bin_id, quantity on hand)
INVENTORY: dict[str, tuple[str, int]] = {
    "SKU-1001": ("B-09-A", 14),
    "SKU-1002": ("B-09-B", 6),
    "SKU-1003": ("B-09-D", 0),      # deliberately out of stock: a lookup that must say so
    "SKU-2001": ("B-03-A", 40),
    "SKU-2002": ("B-03-C", 25),
    "SKU-3001": ("B-07-A", 9),
    "SKU-3002": ("B-07-B", 3),
}

# 12 orders: 8 high priority (3 of them in aisle 9), 4 normal.
ORDERS: tuple[Order, ...] = (
    Order("ORD-4471", "SKU-1001", 2, Priority.HIGH),     # aisle 9
    Order("ORD-4472", "SKU-1002", 1, Priority.HIGH),     # aisle 9
    Order("ORD-4473", "SKU-1003", 4, Priority.HIGH),     # aisle 9, out of stock
    Order("ORD-4474", "SKU-2001", 6, Priority.HIGH),     # aisle 3
    Order("ORD-4475", "SKU-2002", 3, Priority.HIGH),     # aisle 3
    Order("ORD-4476", "SKU-3001", 1, Priority.HIGH),     # aisle 7
    Order("ORD-4477", "SKU-3002", 2, Priority.HIGH),     # aisle 7
    Order("ORD-4478", "SKU-3001", 5, Priority.HIGH),     # aisle 7
    Order("ORD-4479", "SKU-2001", 1, Priority.NORMAL),   # aisle 3
    Order("ORD-4480", "SKU-1001", 3, Priority.NORMAL),   # aisle 9
    Order("ORD-4481", "SKU-3002", 1, Priority.NORMAL),   # aisle 7
    Order("ORD-4482", "SKU-2002", 2, Priority.NORMAL),   # aisle 3
)

_PRODUCTS_BY_SKU = {p.sku: p for p in PRODUCTS}
_BINS_BY_ID = {b.bin_id: b for b in BINS}


class UnknownRecord(ToolLookupError):
    """A lookup for something the fixture does not contain. Never guessed at."""


class WarehouseStore:
    """The only mutable warehouse state, and the only thing that may change it.

    Each session constructs its own store, so two sessions cannot see each other's picks. Orders
    are frozen dataclasses replaced wholesale on mutation rather than edited in place, so a record
    handed out earlier can never change under its holder -- a tool result stays a faithful snapshot
    of the moment it was produced.
    """

    def __init__(self, orders: tuple[Order, ...] = ORDERS):
        self._orders: dict[str, Order] = {o.order_id: o for o in orders}
        self._order_ids: tuple[str, ...] = tuple(o.order_id for o in orders)
        # Bumped by every mutation. Stamped onto every tool result so a result carries the identity
        # of the world it describes, not just the generation that asked for it.
        self.state_version: int = 0
        # The pick the operator is currently on, if any. Selection is itself a mutation.
        self.selected_order_id: str | None = None

    # --- reading ------------------------------------------------------------------------

    def orders(self) -> list[Order]:
        """Every order, in fixture order. A copy: callers cannot mutate our state."""
        return [self._orders[oid] for oid in self._order_ids]

    def order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise UnknownRecord(f"no such order: {order_id}") from exc

    def product(self, sku: str) -> Product:
        try:
            return _PRODUCTS_BY_SKU[sku]
        except KeyError as exc:
            raise UnknownRecord(f"no such sku: {sku}") from exc

    def bin_for_sku(self, sku: str) -> Bin:
        try:
            bin_id, _ = INVENTORY[sku]
        except KeyError as exc:
            raise UnknownRecord(f"no inventory record for sku: {sku}") from exc
        return _BINS_BY_ID[bin_id]

    def quantity_on_hand(self, sku: str) -> int:
        try:
            return INVENTORY[sku][1]
        except KeyError as exc:
            raise UnknownRecord(f"no inventory record for sku: {sku}") from exc

    def aisle_for_order(self, order_id: str) -> int:
        return self.bin_for_sku(self.order(order_id).sku).aisle

    # --- mutation -----------------------------------------------------------------------
    #
    # Every method here bumps state_version. There is no other way to change the store, and no
    # method takes free-form input: a model can pick which of these runs, never what they do.

    def set_status(self, order_id: str, status: OrderStatus) -> Order:
        current = self.order(order_id)
        updated = replace(current, status=status)
        self._orders[order_id] = updated
        self.state_version += 1
        # Selecting an order and then closing it should not leave a dangling selection.
        if self.selected_order_id == order_id and status is not OrderStatus.PENDING:
            self.selected_order_id = None
        return updated

    def select(self, order_id: str) -> Order:
        order = self.order(order_id)          # raises before any state changes
        self.selected_order_id = order_id
        self.state_version += 1
        return order

    def clear_selection(self) -> None:
        if self.selected_order_id is not None:
            self.selected_order_id = None
            self.state_version += 1


def describe_order(store: WarehouseStore, order: Order) -> dict[str, object]:
    """Flatten one order into the plain dict a tool result carries.

    Joined here rather than in the tools so every tool describes an order identically, and so what
    reaches a caller is data -- never a live store reference.
    """
    bin_ = store.bin_for_sku(order.sku)
    return {
        "order_id": order.order_id,
        "sku": order.sku,
        "product": store.product(order.sku).name,
        "quantity": order.quantity,
        "priority": order.priority.value,
        "status": order.status.value,
        "bin": bin_.bin_id,
        "aisle": bin_.aisle,
        "shelf": bin_.shelf,
    }
