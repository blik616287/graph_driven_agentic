"""The matching engine.

Continuous double auction with price-time priority.  One book per instrument,
one engine per process, single threaded by design: matching is a sequence of
state transitions that must be totally ordered, and a lock around the whole
thing is just a slower way of saying "single threaded".

Every fill executes **at the maker's price**.  The taker crossed the spread and
gets the price that was already advertised - that is what makes a resting quote
a real commitment and stops a taker from improving on the price it agreed to.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import NotFound
from ..telemetry.metrics import Registry
from .book import OrderBook, RestingOrder
from .models import Fill, Instrument, Order, OrderStatus, OrderType, Side, TimeInForce
from .money import notional_units


@dataclass(slots=True)
class MatchResult:
    """Everything that happened while an order was processed."""

    order: Order
    fills: list[Fill] = field(default_factory=list)
    resting: bool = False
    cancelled_units: int = 0
    rejected_reason: str | None = None

    @property
    def filled_units(self) -> int:
        return sum(fill.qty_units for fill in self.fills)

    @property
    def touched_makers(self) -> set[str]:
        return {fill.maker_order_id for fill in self.fills}


class MatchingEngine:
    """Owns the books and the sequence counter that defines time priority."""

    __slots__ = ("_books", "_instruments", "_sequence", "_m_fills", "_m_orders", "_m_depth")

    def __init__(self, instruments: dict[str, Instrument], registry: Registry) -> None:
        self._instruments = instruments
        self._books: dict[str, OrderBook] = {sym: OrderBook(sym) for sym in instruments}
        self._sequence = 0
        # Metric handles resolved once, at construction - see telemetry.metrics.
        self._m_fills = registry.counter("engine_fills_total", "fills produced")
        self._m_orders = registry.counter("engine_orders_total", "orders submitted")
        self._m_depth = registry.histogram(
            "engine_levels_walked",
            "price levels visited per submitted order",
            bounds=(0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0),
        )

    def book(self, symbol: str) -> OrderBook:
        book = self._books.get(symbol)
        if book is None:
            raise NotFound(f"unknown instrument: {symbol}", symbol=symbol)
        return book

    def reference_price_units(self, symbol: str) -> int | None:
        """Mid price if two-sided, else whatever side exists.  Used by risk."""
        return self.book(symbol).mid_units()

    # --- HOT PATH ---------------------------------------------------------
    def submit(self, order: Order, now: float) -> MatchResult:
        """Match ``order`` against the book, then rest or cancel the remainder.

        The order object is mutated in place: status, filled quantity and
        average price all advance here, so the caller sees a fully settled view
        by the time this returns.
        """
        self._m_orders.inc()
        book = self.book(order.symbol)
        result = MatchResult(order=order)

        is_market = order.type is OrderType.MARKET
        limit_units = None if is_market else order.price_units
        opposite = order.side.opposite

        # Fill-or-kill is checked before a single unit moves: a partially
        # applied FOK would have to be unwound, and unwinding a match is the
        # kind of code that eats a weekend.
        if order.tif is TimeInForce.FOK:
            if book.available_units(opposite, limit_units) < order.qty_units:
                order.status = OrderStatus.CANCELLED
                order.updated_at = now
                result.cancelled_units = order.qty_units
                result.rejected_reason = "insufficient liquidity for fill-or-kill"
                return result

        remaining = order.qty_units
        levels_walked = 0

        for level in book.iter_levels(opposite, limit_units):
            levels_walked += 1
            price_units = level.price_units
            while remaining:
                maker = level.front()
                if maker is None:
                    break
                traded = maker.remaining_units
                if traded > remaining:
                    traded = remaining
                maker.remaining_units -= traded
                level.total_units -= traded
                remaining -= traded
                if maker.remaining_units == 0:
                    maker.active = False
                    level.live_count -= 1
                    level.orders.popleft()
                result.fills.append(
                    Fill(
                        taker_order_id=order.id,
                        maker_order_id=maker.order_id,
                        taker_account_id=order.account_id,
                        maker_account_id=maker.account_id,
                        symbol=order.symbol,
                        side=order.side,
                        price_units=price_units,
                        qty_units=traded,
                    )
                )
                order.record_fill(price_units, traded, now)
            if not remaining:
                break

        self._m_depth.observe(levels_walked)
        if result.fills:
            self._m_fills.inc(len(result.fills))

        if remaining:
            if is_market or order.tif in (TimeInForce.IOC, TimeInForce.FOK):
                # Nothing left to trade against and nowhere to wait.  The
                # order is terminal even if it partially filled: ``filled_units``
                # records what traded, and a terminal status is what lets the
                # service release the unused part of the reservation.
                result.cancelled_units = remaining
                order.status = OrderStatus.CANCELLED
                if not result.fills:
                    result.rejected_reason = "no liquidity available"
            else:
                self._sequence += 1
                book.add(
                    order.side,
                    RestingOrder(
                        order_id=order.id,
                        account_id=order.account_id,
                        price_units=order.price_units,
                        remaining_units=remaining,
                        sequence=self._sequence,
                    ),
                )
                result.resting = True
                order.status = (
                    OrderStatus.OPEN if not result.fills else OrderStatus.PARTIALLY_FILLED
                )
        order.updated_at = now
        return result

    def estimate(
        self, symbol: str, side: Side, qty_units: int, limit_units: int | None
    ) -> tuple[int, int]:
        """Dry-run a match: ``(fillable_units, notional_units)``.

        The service needs this *before* it reserves funds for a market order.
        A market buy has no limit price, so the only honest way to size its
        reservation is to price the walk it is about to do.  Guessing with a
        slippage percentage either over-reserves (blocking other orders) or
        under-reserves (an overdraft discovered halfway through settlement,
        with fills already applied and no clean way back).

        Safe to call before matching: the walk does not mutate the book, and
        the engine is single threaded, so nothing can change in between.
        """
        instrument = self._instruments[symbol]
        book = self.book(symbol)
        remaining = qty_units
        total_notional = 0
        for price_units, size_units in book.walk(side.opposite, limit_units):
            taken = size_units if size_units < remaining else remaining
            total_notional += notional_units(
                price_units, instrument.price_exp, taken, instrument.qty_exp
            )
            remaining -= taken
            if not remaining:
                break
        return qty_units - remaining, total_notional

    def cancel(self, symbol: str, order_id: str) -> int:
        """Pull an order from the book.  Returns the units removed."""
        removed = self.book(symbol).cancel(order_id)
        return removed.remaining_units if removed is not None else 0

    def snapshot(self, symbol: str, levels: int = 10) -> dict:
        book = self.book(symbol)
        depth = book.depth(levels)
        return {
            "symbol": symbol,
            "bids": depth["bids"],
            "asks": depth["asks"],
            "best_bid_units": book.best_bid(),
            "best_ask_units": book.best_ask(),
            "spread_units": book.spread_units(),
        }

    def stats(self) -> dict[str, int]:
        return {symbol: book.resting_count() for symbol, book in self._books.items()}
