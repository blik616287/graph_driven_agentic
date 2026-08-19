"""Order book and matching engine.

These run with no ledger, no server and no event loop - the payoff for keeping
the domain free of infrastructure.
"""

from __future__ import annotations

import pytest

from marketd.core.book import OrderBook, RestingOrder
from marketd.core.matching import MatchingEngine
from marketd.core.models import (
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    default_instruments,
)
from marketd.core.money import to_units
from marketd.telemetry.metrics import Registry


@pytest.fixture
def engine():
    return MatchingEngine(default_instruments(), Registry())


def order(
    order_id, side, price, qty, *, type_=OrderType.LIMIT, tif=TimeInForce.GTC, account="acct"
):
    return Order(
        id=order_id,
        account_id=account,
        symbol="BTC-USD",
        side=side,
        type=type_,
        tif=tif,
        qty_units=to_units(qty, 8),
        price_units=to_units(price, 2) if price else 0,
    )


def test_best_prices_survive_cancellation():
    book = OrderBook("BTC-USD")
    book.add(Side.BUY, RestingOrder("a", "acct", 100, 5, 1))
    book.add(Side.BUY, RestingOrder("b", "acct", 101, 5, 2))
    assert book.best_bid() == 101
    book.cancel("b")
    # The stale price is still in the heap; best_bid has to skip it.
    assert book.best_bid() == 100


def test_cancelling_twice_is_not_an_error():
    book = OrderBook("BTC-USD")
    book.add(Side.SELL, RestingOrder("a", "acct", 100, 5, 1))
    assert book.cancel("a").remaining_units == 5
    assert book.cancel("a") is None


def test_price_then_time_priority(engine):
    engine.submit(order("m1", Side.SELL, "30100", "1"), 1.0)
    engine.submit(order("m2", Side.SELL, "30050", "1"), 2.0)
    engine.submit(order("m3", Side.SELL, "30100", "1"), 3.0)

    result = engine.submit(order("t1", Side.BUY, "30100", "3"), 4.0)

    # Better price first; within a price, whoever arrived first.
    assert [fill.maker_order_id for fill in result.fills] == ["m2", "m1", "m3"]
    assert [fill.price_units for fill in result.fills] == [3_005_000, 3_010_000, 3_010_000]


def test_fills_happen_at_the_maker_price(engine):
    engine.submit(order("m1", Side.SELL, "30000", "1"), 1.0)
    result = engine.submit(order("t1", Side.BUY, "31000", "1"), 2.0)
    # The taker was willing to pay 31000 and pays 30000: the resting quote is a
    # commitment, and crossing it does not let the taker do better than it.
    assert result.fills[0].price_units == to_units("30000", 2)


def test_partial_fill_rests_the_remainder(engine):
    engine.submit(order("m1", Side.SELL, "30000", "1"), 1.0)
    result = engine.submit(order("t1", Side.BUY, "30000", "3"), 2.0)
    assert result.resting is True
    assert result.order.status is OrderStatus.PARTIALLY_FILLED
    assert result.order.remaining_units == to_units("2", 8)
    assert engine.book("BTC-USD").best_bid() == to_units("30000", 2)


def test_ioc_cancels_the_remainder_and_is_terminal(engine):
    engine.submit(order("m1", Side.SELL, "30000", "1"), 1.0)
    result = engine.submit(order("t1", Side.BUY, "30000", "3", tif=TimeInForce.IOC), 2.0)
    assert result.resting is False
    assert result.cancelled_units == to_units("2", 8)
    # Terminal even though it partially filled: nothing is left to wait for, and
    # a non-terminal status would strand the order's reservation.
    assert result.order.status.is_terminal


def test_fok_is_all_or_nothing_and_leaves_the_book_untouched(engine):
    engine.submit(order("m1", Side.SELL, "30000", "1"), 1.0)
    before = engine.snapshot("BTC-USD")

    result = engine.submit(order("t1", Side.BUY, "30000", "5", tif=TimeInForce.FOK), 2.0)

    assert result.fills == []
    assert result.order.status is OrderStatus.CANCELLED
    assert "fill-or-kill" in result.rejected_reason
    assert engine.snapshot("BTC-USD") == before


def test_fok_fills_when_liquidity_suffices(engine):
    engine.submit(order("m1", Side.SELL, "30000", "3"), 1.0)
    result = engine.submit(order("t1", Side.BUY, "30000", "3", tif=TimeInForce.FOK), 2.0)
    assert result.order.status is OrderStatus.FILLED


def test_market_order_against_an_empty_book_is_cancelled(engine):
    result = engine.submit(
        order("t1", Side.BUY, None, "1", type_=OrderType.MARKET, tif=TimeInForce.IOC), 1.0
    )
    assert result.order.status is OrderStatus.CANCELLED
    assert result.rejected_reason == "no liquidity available"


def test_taker_stops_at_its_limit_price(engine):
    engine.submit(order("m1", Side.SELL, "30000", "1"), 1.0)
    engine.submit(order("m2", Side.SELL, "31000", "1"), 2.0)
    result = engine.submit(order("t1", Side.BUY, "30500", "2"), 3.0)
    assert len(result.fills) == 1
    assert result.resting is True  # the rest becomes a bid at 30500


def test_estimate_prices_the_walk_without_mutating(engine):
    for i, (price, qty) in enumerate([("30000", "1"), ("30100", "2")]):
        engine.submit(order(f"m{i}", Side.SELL, price, qty), 1.0)
    before = engine.snapshot("BTC-USD")

    fillable, cost = engine.estimate("BTC-USD", Side.BUY, to_units("3", 8), None)

    assert fillable == to_units("3", 8)
    assert cost == to_units("90200", 8)  # 30000 + 2 x 30100
    assert engine.snapshot("BTC-USD") == before


def test_cancelled_orders_are_skipped_during_matching(engine):
    engine.submit(order("m1", Side.SELL, "30000", "1"), 1.0)
    engine.submit(order("m2", Side.SELL, "30000", "1"), 2.0)
    engine.cancel("BTC-USD", "m1")

    result = engine.submit(order("t1", Side.BUY, "30000", "1"), 3.0)

    assert [fill.maker_order_id for fill in result.fills] == ["m2"]


def test_average_price_is_quantity_weighted(engine):
    engine.submit(order("m1", Side.SELL, "30000", "1"), 1.0)
    engine.submit(order("m2", Side.SELL, "31000", "3"), 2.0)
    result = engine.submit(order("t1", Side.BUY, "31000", "4"), 3.0)
    # (30000 x 1 + 31000 x 3) / 4
    assert result.order.avg_price_units == to_units("30750", 2)
