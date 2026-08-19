"""A price-time priority limit order book.

Data structure choices, and why:

``levels``  ``dict[int, PriceLevel]``
    Price -> level.  Adding, cancelling and looking up a price are all O(1).

``prices``  ``list[int]`` used as a heap
    Only the *best* price is ever needed, so a full ordering is wasted work.  A
    heap gives O(log n) insert and O(1) peek.  Bids store negated prices so the
    same min-heap serves both sides.

``PriceLevel.orders``  ``deque``
    Time priority is strict FIFO: append right, consume left, both O(1).

Two forms of laziness keep the hot path short:

*Tombstones*  Cancelling does not remove the order from its deque (O(n)).  It
clears ``active`` and decrements the level total.  Matching skips dead entries
and drops them as it passes.

*Lazy heap cleanup*  Emptying a level does not remove its price from the heap
(there is no efficient way).  ``best_bid``/``best_ask`` discard stale heap
entries when they surface.  Both are amortised O(1) per order.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from heapq import heappop, heappush
from collections.abc import Iterator

from .models import Side


@dataclass(slots=True)
class RestingOrder:
    """An order's presence in the book.

    Deliberately *not* the :class:`~marketd.core.models.Order` itself.  The book
    needs four numbers per resting order; carrying the full entity through the
    matching loop would drag status, timestamps and reservation bookkeeping into
    the cache line for no benefit.
    """

    order_id: str
    account_id: str
    price_units: int
    remaining_units: int
    sequence: int
    active: bool = True


class PriceLevel:
    """Every resting order at one price, in arrival order."""

    __slots__ = ("price_units", "orders", "total_units", "live_count")

    def __init__(self, price_units: int) -> None:
        self.price_units = price_units
        self.orders: deque[RestingOrder] = deque()
        self.total_units = 0
        self.live_count = 0

    def append(self, order: RestingOrder) -> None:
        self.orders.append(order)
        self.total_units += order.remaining_units
        self.live_count += 1

    def remove(self, order: RestingOrder) -> None:
        """Tombstone an order.  The deque entry is reaped during matching."""
        order.active = False
        self.total_units -= order.remaining_units
        self.live_count -= 1
        order.remaining_units = 0

    # --- HOT PATH ---------------------------------------------------------
    def front(self) -> RestingOrder | None:
        """The next order to fill, discarding tombstones on the way."""
        orders = self.orders
        while orders:
            head = orders[0]
            if head.active and head.remaining_units > 0:
                return head
            orders.popleft()
        return None

    def __bool__(self) -> bool:
        return self.live_count > 0


class OrderBook:
    """One book per instrument."""

    __slots__ = ("symbol", "_bid_levels", "_ask_levels", "_bid_heap", "_ask_heap", "_index")

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._bid_levels: dict[int, PriceLevel] = {}
        self._ask_levels: dict[int, PriceLevel] = {}
        self._bid_heap: list[int] = []   # negated prices -> max-heap behaviour
        self._ask_heap: list[int] = []
        self._index: dict[str, tuple[Side, RestingOrder]] = {}

    # ------------------------------------------------------------------ reads
    def best_bid(self) -> int | None:
        heap, levels = self._bid_heap, self._bid_levels
        while heap:
            price = -heap[0]
            level = levels.get(price)
            if level is not None and level.live_count:
                return price
            heappop(heap)          # stale entry: the level emptied out
            levels.pop(price, None)
        return None

    def best_ask(self) -> int | None:
        heap, levels = self._ask_heap, self._ask_levels
        while heap:
            price = heap[0]
            level = levels.get(price)
            if level is not None and level.live_count:
                return price
            heappop(heap)
            levels.pop(price, None)
        return None

    def spread_units(self) -> int | None:
        bid, ask = self.best_bid(), self.best_ask()
        return None if bid is None or ask is None else ask - bid

    def mid_units(self) -> int | None:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return ask if bid is None else bid
        return (bid + ask) // 2

    def get(self, order_id: str) -> RestingOrder | None:
        entry = self._index.get(order_id)
        return entry[1] if entry is not None else None

    def depth(self, levels: int = 10) -> dict[str, list[tuple[int, int]]]:
        """Aggregated top-of-book, best price first.

        Sorts the live prices rather than draining the heap - the heap is the
        book's own state and must not be consumed by a read.
        """
        bids = sorted(
            ((price, lvl.total_units) for price, lvl in self._bid_levels.items() if lvl.live_count),
            key=lambda item: -item[0],
        )[:levels]
        asks = sorted(
            ((price, lvl.total_units) for price, lvl in self._ask_levels.items() if lvl.live_count),
            key=lambda item: item[0],
        )[:levels]
        return {"bids": bids, "asks": asks}

    def resting_count(self) -> int:
        return sum(1 for _, order in self._index.values() if order.active)

    # ----------------------------------------------------------------- writes
    def add(self, side: Side, order: RestingOrder) -> None:
        """Rest an order.  The caller must have matched off any crossing part."""
        if order.order_id in self._index:
            raise KeyError(f"order {order.order_id} is already resting")
        if side is Side.BUY:
            levels, heap, key = self._bid_levels, self._bid_heap, -order.price_units
        else:
            levels, heap, key = self._ask_levels, self._ask_heap, order.price_units
        level = levels.get(order.price_units)
        if level is None:
            level = PriceLevel(order.price_units)
            levels[order.price_units] = level
            heappush(heap, key)     # only new levels touch the heap
        level.append(order)
        self._index[order.order_id] = (side, order)

    def cancel(self, order_id: str) -> RestingOrder | None:
        entry = self._index.pop(order_id, None)
        if entry is None:
            return None
        side, order = entry
        if not order.active:
            return None
        levels = self._bid_levels if side is Side.BUY else self._ask_levels
        level = levels.get(order.price_units)
        remaining = order.remaining_units
        if level is not None:
            level.remove(order)
        order.remaining_units = remaining   # report what was actually cancelled
        return order

    # --- HOT PATH ---------------------------------------------------------
    def iter_levels(self, side: Side, limit_units: int | None) -> Iterator[PriceLevel]:
        """Walk the ``side`` of the book in match order, best price first.

        ``limit_units`` bounds how far the walk may go (``None`` means "any
        price", for market orders).  Yields levels lazily, so a taker that fills
        at the touch never inspects the rest of the book.

        The heap is drained as it goes: a level yielded here is either fully
        consumed by the caller or the walk stops, and stale prices left behind
        are cleaned up by ``best_bid``/``best_ask`` later.
        """
        if side is Side.BUY:        # walking the bids: highest price first
            heap, levels, sign = self._bid_heap, self._bid_levels, -1
        else:
            heap, levels, sign = self._ask_heap, self._ask_levels, 1

        while heap:
            price = sign * heap[0]
            level = levels.get(price)
            if level is None or not level.live_count:
                heappop(heap)
                levels.pop(price, None)
                continue
            if limit_units is not None:
                # A buy taker crosses asks at or below its limit; a sell taker
                # crosses bids at or above it.
                if side is Side.BUY and price < limit_units:
                    return
                if side is Side.SELL and price > limit_units:
                    return
            yield level
            if level.live_count:
                return              # caller stopped early: level still has depth
            heappop(heap)
            levels.pop(price, None)

    def walk(self, side: Side, limit_units: int | None) -> Iterator[tuple[int, int]]:
        """Read-only version of :meth:`iter_levels`, yielding ``(price, size)``.

        :meth:`iter_levels` consumes the heap as it goes, so it cannot be used
        for anything that must leave the book untouched - pre-trade checks,
        cost estimates, depth snapshots.  This walk sorts the live prices
        instead, paying O(n log n) to guarantee it has no side effects.
        """
        levels = self._bid_levels if side is Side.BUY else self._ask_levels
        prices = sorted(
            (price for price, level in levels.items() if level.live_count),
            reverse=side is Side.BUY,
        )
        for price in prices:
            if limit_units is not None:
                if side is Side.BUY and price < limit_units:
                    return
                if side is Side.SELL and price > limit_units:
                    return
            yield price, levels[price].total_units

    def available_units(self, side: Side, limit_units: int | None) -> int:
        """Total size resting on ``side`` within ``limit_units``.

        Used for fill-or-kill pre-checks, which must not disturb the book.
        """
        return sum(size for _price, size in self.walk(side, limit_units))
