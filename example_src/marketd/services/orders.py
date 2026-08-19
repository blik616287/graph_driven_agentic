"""Order placement, cancellation and lookup.

:meth:`OrderService.place` is the hot path of the whole service.  It runs in a
fixed sequence, and the sequence is the design:

1. **Resolve and convert.**  Decimal in, scaled integers out.  Everything below
   this line is integer arithmetic.
2. **Idempotency.**  A repeated ``client_order_id`` returns the original order
   instead of placing a second one.  Retries are a fact of networked life; the
   only question is whether the API makes them safe.
3. **Risk.**  Cheap checks first, the ledger read last.
4. **Reserve.**  Funds move to ``reserved`` *before* matching.  Reserving after
   would leave a window where a filled order has no money behind it.
5. **Match.**  The engine mutates the book and the order.
6. **Settle.**  Every fill becomes a balanced journal entry and a trade.
7. **Finalise.**  Terminal orders give back what they did not spend.
8. **Publish.**  Events go out last, once the state they describe is durable.

Steps 3-7 must not be interleaved with another order.  They are not, because
the service runs on a single event loop thread and contains no ``await``.  That
is a real constraint, and it is written down here rather than discovered later:
**do not add an await between the risk check and the end of settlement.**
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..core.matching import MatchingEngine, MatchResult
from ..core.models import (
    Instrument,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from ..core.money import MONEY_EXP, apply_bps, mul_div, notional_units, pow10, to_units
from ..errors import Conflict, NotFound, RiskRejected, ValidationError
from ..storage.memstore import MemoryRepository
from ..storage.query import Query
from ..telemetry.metrics import Registry


class OrderService:
    __slots__ = (
        "_instruments", "_engine", "_ledger", "_risk", "_orders", "_trades",
        "_ids", "_clock", "_bus", "_log", "_tracer", "_taker_bps", "_maker_bps",
        "_open_counts", "_m_placed", "_m_rejected", "_m_cancelled", "_m_notional", "_t_place",
    )

    def __init__(
        self,
        *,
        instruments: dict[str, Instrument],
        engine: MatchingEngine,
        ledger,
        risk,
        orders: MemoryRepository[Order],
        trades: MemoryRepository[Trade],
        ids,
        clock,
        bus,
        event_log,
        tracer,
        registry: Registry,
        taker_fee_bps: int = 10,
        maker_fee_bps: int = 2,
    ) -> None:
        self._instruments = instruments
        self._engine = engine
        self._ledger = ledger
        self._risk = risk
        self._orders = orders
        self._trades = trades
        self._ids = ids
        self._clock = clock
        self._bus = bus
        self._log = event_log
        self._tracer = tracer
        self._taker_bps = taker_fee_bps
        self._maker_bps = maker_fee_bps
        # Open orders per account, maintained incrementally.  Risk needs this
        # count on every order; deriving it by scanning the repository would
        # make order placement O(orders in the venue).
        self._open_counts: dict[str, int] = {}
        self._m_placed = registry.counter("orders_placed_total", "orders accepted")
        self._m_rejected = registry.counter("orders_rejected_total", "orders rejected")
        self._m_cancelled = registry.counter("orders_cancelled_total", "orders cancelled")
        self._m_notional = registry.counter("traded_notional_units_total", "notional traded")
        self._t_place = registry.histogram("order_place_seconds", "end-to-end order placement")

    # ------------------------------------------------------------------ read
    def instrument(self, symbol: str) -> Instrument:
        instrument = self._instruments.get(symbol)
        if instrument is None:
            raise NotFound(f"unknown instrument: {symbol}", symbol=symbol)
        return instrument

    def get(self, principal, order_id: str) -> Order:
        order = self._orders.get(order_id)
        # Same reasoning as accounts: never confirm that another account's
        # order exists.
        if order is None or not principal.owns(order.account_id):
            raise NotFound(f"no order with id {order_id}", id=order_id)
        return order

    def list_for_account(
        self,
        account_id: str,
        *,
        symbol: str | None = None,
        status: str | None = None,
        before_id: str | None = None,
        limit: int = 50,
    ) -> list[Order]:
        """Newest-first page of an account's orders.

        The account index narrows the candidate set before any filtering, so
        the cost is proportional to one account's orders rather than to the
        venue's.  ``limit + 1`` rows are returned so the caller can tell
        whether another page exists.
        """
        query: Query[Order] = Query()
        if symbol is not None:
            query = query.eq("symbol", symbol)
        if status is not None:
            query = query.eq("status", OrderStatus(status))
        if before_id is not None:
            query = query.where("id", "lt", before_id)
        # Ids are time-sortable, so ordering by id *is* ordering by creation.
        query = query.order_by("id", descending=True).limit(limit + 1)
        return self._orders.query(query, index=("account", account_id))

    def open_orders(self, account_id: str) -> int:
        return self._open_counts.get(account_id, 0)

    # --- HOT PATH ---------------------------------------------------------
    def place(self, principal, payload: dict[str, Any]) -> Order:
        """Place an order.  See the module docstring for the step order."""
        with self._tracer.span("order.place") as span:
            symbol: str = payload["symbol"]
            instrument = self.instrument(symbol)
            span.set(symbol=symbol, side=payload["side"].value)

            order_type: OrderType = payload["type"]
            tif: TimeInForce = payload["tif"]
            side: Side = payload["side"]

            qty_units = self._convert(payload["quantity"], instrument.qty_exp, "quantity")
            price_units = 0
            if order_type is OrderType.LIMIT:
                if payload.get("price") is None:
                    raise ValidationError(**{"$.price": "price is required for limit orders"})
                price_units = self._convert(payload["price"], instrument.price_exp, "price")
            elif payload.get("price") is not None:
                raise ValidationError(**{"$.price": "market orders must not carry a price"})

            client_order_id = payload.get("client_order_id")
            if client_order_id:
                duplicate = self._orders.find_by(
                    "client_order_id", (principal.account_id, client_order_id)
                )
                if duplicate:
                    # Idempotent replay: return the original, do not place again.
                    return duplicate[0]

            now = self._clock.now()
            order = Order(
                id=self._ids.next_token("ord"),
                account_id=principal.account_id,
                symbol=symbol,
                side=side,
                type=order_type,
                tif=tif,
                qty_units=qty_units,
                price_units=price_units,
                client_order_id=client_order_id,
                created_at=now,
                updated_at=now,
            )

            # A market order is priced by walking the book, so the reservation
            # covers exactly what the match will cost.  See MatchingEngine.estimate.
            estimated_notional = None
            if order_type is OrderType.MARKET:
                _fillable, estimated_notional = self._engine.estimate(symbol, side, qty_units, None)

            decision = self._risk.evaluate(
                order,
                instrument,
                tier=principal.tier,
                open_orders=self._open_counts.get(principal.account_id, 0),
                reference_price_units=self._engine.reference_price_units(symbol),
                now=self._clock.monotonic(),
                estimated_notional_units=estimated_notional,
            )
            if not decision.ok:
                self._m_rejected.inc()
                order.status = OrderStatus.REJECTED
                self._log.append(
                    "order.rejected", order_id=order.id, account_id=order.account_id,
                    reason=decision.reason, code=decision.code,
                )
                error = RiskRejected(decision.reason, symbol=symbol)
                error.code = decision.code  # type: ignore[misc]
                raise error

            self._ledger.reserve(
                order.account_id, decision.reserve_asset, decision.reserve_units, ref=order.id
            )
            order.reserved_asset = decision.reserve_asset
            order.reserved_units = decision.reserve_units

            self._orders.add(order)
            self._track_open(order.account_id, +1)

            result = self._engine.submit(order, now)
            self._settle(result, instrument, now)
            self._finalise(order, now)

            self._m_placed.inc()
            span.set(fills=len(result.fills), status=order.status.value)
            self._publish_placed(order, result)
            return order

    def cancel(self, principal, order_id: str) -> Order:
        """Pull an order from the book and release its reservation."""
        order = self.get(principal, order_id)
        if not order.is_open:
            raise Conflict(
                f"order is already {order.status.value}",
                id=order_id,
                status=order.status.value,
            )
        self._engine.cancel(order.symbol, order.id)
        now = self._clock.now()
        order.status = OrderStatus.CANCELLED
        order.updated_at = now
        order.version += 1
        self._finalise(order, now)
        self._m_cancelled.inc()
        self._log.append("order.cancelled", order_id=order.id, account_id=order.account_id)
        self._bus.publish_nowait(
            self._bus.emit("order.cancelled", order_id=order.id, symbol=order.symbol)
        )
        return order

    # -------------------------------------------------------------- internals
    def _convert(self, value: Decimal, exp: int, field: str) -> int:
        """Decimal to scaled integer, with the failure reported as a 422.

        ``to_units`` raises ``ValueError`` for excess precision; letting that
        escape would be a 500 for what is a client mistake.
        """
        try:
            return to_units(value, exp)
        except ValueError as exc:
            raise ValidationError(**{f"$.{field}": str(exc)}) from None

    # --- HOT PATH ---------------------------------------------------------
    def _settle(self, result: MatchResult, instrument: Instrument, now: float) -> None:
        """Turn fills into journal entries and trades.

        Called with the book already mutated, so it cannot fail on funds: every
        participant reserved enough before its order was allowed to rest.  The
        reservation arithmetic is asserted rather than assumed - if it is ever
        wrong, the loud failure here is far cheaper than a silent one in the
        ledger.
        """
        if not result.fills:
            return
        taker = result.order
        base, quote = instrument.base, instrument.quote
        qty_exp, price_exp = instrument.qty_exp, instrument.price_exp

        for fill in result.fills:
            maker = self._orders.require(fill.maker_order_id)
            notional = notional_units(fill.price_units, price_exp, fill.qty_units, qty_exp)
            taker_fee = apply_bps(notional, self._taker_bps)
            maker_fee = apply_bps(notional, self._maker_bps)
            qty_ledger = _rescale(fill.qty_units, qty_exp, MONEY_EXP)

            if fill.side is Side.BUY:
                buyer, seller = taker, maker
                buyer_fee, seller_fee = taker_fee, maker_fee
            else:
                buyer, seller = maker, taker
                buyer_fee, seller_fee = maker_fee, taker_fee

            buyer_cost = notional + buyer_fee
            self._consume_reservation(buyer, quote, buyer_cost)
            self._consume_reservation(seller, base, qty_ledger)

            entry = self._ledger.settle_fill(
                base=base,
                quote=quote,
                buyer_id=buyer.account_id,
                seller_id=seller.account_id,
                qty_units=qty_ledger,
                notional_units=notional,
                buyer_fee_units=buyer_fee,
                seller_fee_units=seller_fee,
                buyer_from_reserved=True,
                seller_from_reserved=True,
                ref=fill.taker_order_id,
            )

            # The engine advanced the taker's order; the maker's entity is this
            # layer's responsibility.
            maker.record_fill(fill.price_units, fill.qty_units, now)

            trade = Trade(
                id=self._ids.next_token("trd"),
                symbol=fill.symbol,
                price_units=fill.price_units,
                qty_units=fill.qty_units,
                taker_side=fill.side,
                buyer_account_id=buyer.account_id,
                seller_account_id=seller.account_id,
                buy_order_id=buyer.id,
                sell_order_id=seller.id,
                ts=now,
            )
            self._trades.add(trade)
            self._m_notional.inc(notional / pow10(MONEY_EXP))

            self._log.append(
                "trade.executed",
                trade_id=trade.id,
                symbol=trade.symbol,
                price_units=trade.price_units,
                qty_units=trade.qty_units,
                taker_side=trade.taker_side.value,
                entry_id=entry.id,
            )
            self._bus.publish_nowait(
                self._bus.emit(
                    "trade.executed",
                    trade_id=trade.id,
                    symbol=trade.symbol,
                    price_units=trade.price_units,
                    qty_units=trade.qty_units,
                )
            )
            if not maker.is_open:
                self._finalise(maker, now)

    def _consume_reservation(self, order: Order, asset: str, units: int) -> None:
        """Draw ``units`` from an order's reservation.

        An order can only ever consume what it reserved, because the reservation
        was sized against the worst price the order could trade at.  A shortfall
        means the sizing logic and the matching logic disagree, which is a bug
        that must not be papered over by quietly dipping into available funds.
        """
        if order.reserved_asset != asset:
            raise Conflict(
                "reservation asset mismatch",
                order_id=order.id,
                reserved=order.reserved_asset,
                required=asset,
            )
        if order.reserved_units < units:
            raise Conflict(
                "reservation shortfall while settling",
                order_id=order.id,
                reserved_units=order.reserved_units,
                required_units=units,
            )
        order.reserved_units -= units

    def _finalise(self, order: Order, now: float) -> None:
        """Release what a terminal order did not spend."""
        if order.is_open:
            return
        if order.reserved_units > 0:
            self._ledger.release(order.account_id, order.reserved_asset, order.reserved_units)
            order.reserved_units = 0
        self._track_open(order.account_id, -1)
        order.updated_at = now

    def _track_open(self, account_id: str, delta: int) -> None:
        count = self._open_counts.get(account_id, 0) + delta
        if count > 0:
            self._open_counts[account_id] = count
        else:
            self._open_counts.pop(account_id, None)

    def _publish_placed(self, order: Order, result: MatchResult) -> None:
        self._log.append(
            "order.placed",
            order_id=order.id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side.value,
            status=order.status.value,
            fills=len(result.fills),
        )
        self._bus.publish_nowait(
            self._bus.emit(
                "order.placed",
                order_id=order.id,
                symbol=order.symbol,
                status=order.status.value,
            )
        )


def _rescale(units: int, from_exp: int, to_exp: int) -> int:
    if from_exp == to_exp:
        return units
    if to_exp > from_exp:
        return units * pow10(to_exp - from_exp)
    return mul_div(units, 1, pow10(from_exp - to_exp))
