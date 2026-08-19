"""The order service: the sequence in ``OrderService.place``, exercised."""

from __future__ import annotations

from decimal import Decimal

import pytest

from marketd.core.models import OrderStatus, OrderType, Side, TimeInForce
from marketd.core.money import MONEY_EXP, to_units
from marketd.errors import Conflict, NotFound, RiskRejected


def place(container, principal, side, quantity, price=None, **overrides):
    payload = {
        "symbol": "BTC-USD",
        "side": Side(side),
        "type": overrides.pop("type", OrderType.LIMIT),
        "tif": overrides.pop("tif", TimeInForce.GTC),
        "quantity": Decimal(quantity),
        "price": Decimal(price) if price is not None else None,
        "client_order_id": overrides.pop("client_order_id", None),
    }
    payload.update(overrides)
    return container.order_service.place(principal, payload)


def available(container, account_id, asset):
    return container.ledger.balance(account_id, asset).available_units


def test_resting_order_reserves_funds(container, funded):
    (maker_account, maker_principal, _), _ = funded
    before = available(container, maker_account.id, "USD")

    order = place(container, maker_principal, "buy", "1", "30000.00")

    assert order.status is OrderStatus.OPEN
    balance = container.ledger.balance(maker_account.id, "USD")
    # Notional plus the worst-case (taker) fee.
    assert balance.reserved_units == to_units("30030", MONEY_EXP)
    assert balance.available_units == before - to_units("30030", MONEY_EXP)
    assert container.ledger.verify() == {}


def test_a_full_trade_settles_both_sides(container, funded):
    (maker_account, maker_principal, _), (taker_account, taker_principal, _) = funded
    place(container, maker_principal, "sell", "1", "30000.00")

    order = place(container, taker_principal, "buy", "1", "30000.00")

    assert order.status is OrderStatus.FILLED
    def btc(account_id):
        return container.ledger.balance(account_id, "BTC").available_units

    assert btc(taker_account.id) == to_units("51", MONEY_EXP)
    assert btc(maker_account.id) == to_units("49", MONEY_EXP)
    # Taker pays 10bps, maker pays 2bps, both in the quote asset.
    assert container.ledger.balance("sys_fees", "USD").available_units == to_units("36", MONEY_EXP)
    assert container.ledger.verify() == {}


def test_reservations_are_released_when_an_order_is_cancelled(container, funded):
    (maker_account, maker_principal, _), _ = funded
    before = available(container, maker_account.id, "USD")

    order = place(container, maker_principal, "buy", "1", "30000.00")
    container.order_service.cancel(maker_principal, order.id)

    assert available(container, maker_account.id, "USD") == before
    assert container.ledger.balance(maker_account.id, "USD").reserved_units == 0
    assert container.ledger.verify() == {}


def test_filling_below_the_limit_price_returns_the_difference(container, funded):
    """A buy that fills cheaply must not keep the excess reserved."""
    (maker_account, maker_principal, _), (taker_account, taker_principal, _) = funded
    place(container, maker_principal, "sell", "1", "29000.00")
    before = available(container, taker_account.id, "USD")

    order = place(container, taker_principal, "buy", "1", "30000.00")

    assert order.status is OrderStatus.FILLED
    spent = before - available(container, taker_account.id, "USD")
    # Charged at the maker's 29000, not at the 30000 the reservation covered.
    assert spent == to_units("29029", MONEY_EXP)
    assert container.ledger.balance(taker_account.id, "USD").reserved_units == 0


def test_client_order_id_makes_placement_idempotent(container, funded):
    _, (_, taker_principal, _) = funded
    first = place(container, taker_principal, "buy", "1", "29000.00", client_order_id="abc")
    second = place(container, taker_principal, "buy", "1", "29000.00", client_order_id="abc")
    assert first.id == second.id
    assert container.orders_repo.count() == 1


def test_open_order_count_tracks_the_lifecycle(container, funded):
    (maker_account, maker_principal, _), _ = funded
    service = container.order_service
    assert service.open_orders(maker_account.id) == 0

    first = place(container, maker_principal, "buy", "1", "29000.00")
    place(container, maker_principal, "buy", "1", "28000.00")
    assert service.open_orders(maker_account.id) == 2

    service.cancel(maker_principal, first.id)
    assert service.open_orders(maker_account.id) == 1


def test_market_order_reserves_exactly_what_the_walk_costs(container, funded):
    (_, maker_principal, _), (taker_account, taker_principal, _) = funded
    place(container, maker_principal, "sell", "1", "30000.00")
    place(container, maker_principal, "sell", "1", "30500.00")
    before = available(container, taker_account.id, "USD")

    order = place(
        container, taker_principal, "buy", "2",
        type=OrderType.MARKET, tif=TimeInForce.IOC,
    )

    assert order.status is OrderStatus.FILLED
    spent = before - available(container, taker_account.id, "USD")
    notional = to_units("60500", MONEY_EXP)
    assert spent == notional + to_units("60.5", MONEY_EXP)  # 10bps taker fee
    assert container.ledger.balance(taker_account.id, "USD").reserved_units == 0
    assert container.ledger.verify() == {}


def test_orders_beyond_the_balance_are_rejected_before_anything_moves(container, funded):
    (maker_account, maker_principal, _), _ = funded
    before = available(container, maker_account.id, "USD")

    with pytest.raises(RiskRejected) as caught:
        place(container, maker_principal, "buy", "100", "30000.00")

    assert caught.value.code == "insufficient_funds"
    assert available(container, maker_account.id, "USD") == before
    assert container.orders_repo.count() == 0  # nothing was persisted


def test_prices_off_the_tick_grid_are_rejected(container, funded):
    _, (_, taker_principal, _) = funded
    with pytest.raises(RiskRejected, match="multiple of"):
        place(container, taker_principal, "buy", "1", "30000.07")


def test_cancelling_someone_elses_order_reports_not_found(container, funded):
    (_, maker_principal, _), (_, taker_principal, _) = funded
    order = place(container, maker_principal, "buy", "1", "29000.00")
    # 404, not 403: a 403 would confirm the order exists.
    with pytest.raises(NotFound):
        container.order_service.cancel(taker_principal, order.id)


def test_cancelling_twice_conflicts(container, funded):
    (_, maker_principal, _), _ = funded
    order = place(container, maker_principal, "buy", "1", "29000.00")
    container.order_service.cancel(maker_principal, order.id)
    with pytest.raises(Conflict):
        container.order_service.cancel(maker_principal, order.id)


def test_self_trade_nets_out_and_keeps_the_books_balanced(container, funded):
    """Crossing your own quote is allowed here; it must still balance."""
    (_, maker_principal, _), _ = funded
    place(container, maker_principal, "sell", "1", "30000.00")
    order = place(container, maker_principal, "buy", "1", "30000.00")
    assert order.status is OrderStatus.FILLED
    assert container.ledger.verify() == {}


def test_books_balance_after_a_burst_of_random_trading(container, funded):
    """The invariant that matters, under load rather than in isolation."""
    import random

    (_, maker_principal, _), (_, taker_principal, _) = funded
    rng = random.Random(20240617)
    for _ in range(120):
        principal = rng.choice([maker_principal, taker_principal])
        side = rng.choice(["buy", "sell"])
        price = f"{rng.randrange(29_000, 31_000, 50)}.00"
        quantity = f"0.{rng.randrange(1, 9)}"
        try:
            place(container, principal, side, quantity, price)
        except RiskRejected:
            pass  # rejections are a legitimate outcome, not a test failure

    assert container.ledger.verify() == {}
    assert container.trades_repo.count() > 0
