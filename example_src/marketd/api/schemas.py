"""Request schemas and response presenters.

The two directions are kept apart on purpose.

**Inbound**: schemas are compiled once, at import time.  A handler calls the
compiled validator and gets back a dict of coerced values, or a 422 listing
every bad field.

**Outbound**: presenters turn domain objects into wire dicts.  They are the
only place that knows a price is stored as a scaled integer and shown as a
decimal string.  Serialising a domain object directly would leak internal
representation into the contract and make it impossible to change.
"""

from __future__ import annotations

from typing import Any

from ..core.models import (
    Account,
    Balance,
    Instrument,
    Order,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from ..core.money import MONEY_EXP, format_units
from ..core.validation import Dec, EnumOf, Obj, Str, compile_schema, optional, required

SYMBOL_PATTERN = r"^[A-Z0-9]{2,8}-[A-Z0-9]{2,8}$"

# --- inbound ---------------------------------------------------------------

CREATE_ACCOUNT = compile_schema(
    Obj(
        {
            "name": required(Str(min_len=2, max_len=64)),
            "tier": optional(Str(choices=("standard", "pro"), lower=True), "standard"),
        }
    )
)

CREATE_DEPOSIT = compile_schema(
    Obj(
        {
            "asset": required(Str(min_len=2, max_len=8)),
            "amount": required(Dec(minimum="0", maximum="1000000000", max_places=MONEY_EXP)),
        }
    )
)

PLACE_ORDER = compile_schema(
    Obj(
        {
            "symbol": required(Str(min_len=3, max_len=17, pattern=SYMBOL_PATTERN)),
            "side": required(EnumOf(Side)),
            "type": optional(EnumOf(OrderType), OrderType.LIMIT),
            "tif": optional(EnumOf(TimeInForce), TimeInForce.GTC),
            "quantity": required(Dec(minimum="0", max_places=MONEY_EXP)),
            # Required for limit orders, rejected for market orders - a
            # conditional the schema cannot express, so the service checks it.
            "price": optional(Dec(minimum="0", max_places=MONEY_EXP)),
            "client_order_id": optional(Str(min_len=1, max_len=64)),
        }
    )
)


# --- outbound --------------------------------------------------------------

def present_instrument(instrument: Instrument) -> dict[str, Any]:
    return {
        "symbol": instrument.symbol,
        "base": instrument.base,
        "quote": instrument.quote,
        "status": "active" if instrument.active else "halted",
        "tick_size": instrument.format_price(instrument.tick_units),
        "lot_size": instrument.format_qty(instrument.lot_units),
        "min_quantity": instrument.format_qty(instrument.min_qty_units),
        "max_quantity": instrument.format_qty(instrument.max_qty_units),
        "max_notional": format_units(instrument.max_notional_units, MONEY_EXP),
    }


def present_account(account: Account) -> dict[str, Any]:
    return {
        "id": account.id,
        "name": account.name,
        "tier": account.tier,
        "created_at": account.created_at,
        "status": "disabled" if account.disabled else "active",
    }


def present_balance(balance: Balance) -> dict[str, Any]:
    return balance.as_dict()


def present_order(order: Order, instrument: Instrument) -> dict[str, Any]:
    """Render an order.

    ``instrument`` is passed in rather than looked up because the caller
    already has it - and because a presenter that can hit a repository is a
    presenter that will eventually do it once per row of a listing.
    """
    payload: dict[str, Any] = {
        "id": order.id,
        "account_id": order.account_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "type": order.type.value,
        "tif": order.tif.value,
        "status": order.status.value,
        "quantity": instrument.format_qty(order.qty_units),
        "filled_quantity": instrument.format_qty(order.filled_units),
        "remaining_quantity": instrument.format_qty(order.remaining_units),
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }
    if order.type is OrderType.LIMIT:
        payload["price"] = instrument.format_price(order.price_units)
    if order.filled_units:
        payload["average_price"] = instrument.format_price(order.avg_price_units)
    if order.client_order_id:
        payload["client_order_id"] = order.client_order_id
    return payload


def present_trade(trade: Trade, instrument: Instrument) -> dict[str, Any]:
    """The public tape: price, size, time, aggressor.  No counterparties."""
    return {
        "id": trade.id,
        "symbol": trade.symbol,
        "price": instrument.format_price(trade.price_units),
        "quantity": instrument.format_qty(trade.qty_units),
        "taker_side": trade.taker_side.value,
        "ts": trade.ts,
    }


def present_book(snapshot: dict[str, Any], instrument: Instrument) -> dict[str, Any]:
    def level(entry: tuple[int, int]) -> list[str]:
        price_units, qty_units = entry
        return [instrument.format_price(price_units), instrument.format_qty(qty_units)]

    best_bid = snapshot["best_bid_units"]
    best_ask = snapshot["best_ask_units"]
    return {
        "symbol": snapshot["symbol"],
        "bids": [level(entry) for entry in snapshot["bids"]],
        "asks": [level(entry) for entry in snapshot["asks"]],
        "best_bid": instrument.format_price(best_bid) if best_bid is not None else None,
        "best_ask": instrument.format_price(best_ask) if best_ask is not None else None,
        "spread": (
            instrument.format_price(snapshot["spread_units"])
            if snapshot["spread_units"] is not None
            else None
        ),
    }
