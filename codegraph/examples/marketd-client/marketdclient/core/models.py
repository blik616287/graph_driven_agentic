"""Client-side domain types.

Mirrors the server's vocabulary so callers speak one language across the wire.
Deliberately a *copy*, not an import: the client ships independently of the
venue, and coupling the two at build time would mean neither could be deployed
alone. The cost of that decision is drift, which is exactly the thing a
cross-root graph edge is meant to make visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(StrEnum):
    NEW = "new"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradeable pair as the client sees it.

    The client keeps only what it needs to format a request: the venue owns the
    authoritative tick and lot rules and will reject anything that violates them.
    """

    symbol: str
    base: str
    quote: str
    tick_size: Decimal
    lot_size: Decimal

    @classmethod
    def from_payload(cls, payload: dict) -> "Instrument":
        return cls(
            symbol=payload["symbol"],
            base=payload["base"],
            quote=payload["quote"],
            tick_size=Decimal(payload["tick_size"]),
            lot_size=Decimal(payload["lot_size"]),
        )


@dataclass(frozen=True, slots=True)
class Order:
    """An order as returned by the venue."""

    id: str
    symbol: str
    side: Side
    type: OrderType
    tif: TimeInForce
    status: OrderStatus
    quantity: Decimal
    filled_quantity: Decimal
    price: Decimal | None = None

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.NEW)

    @classmethod
    def from_payload(cls, payload: dict) -> "Order":
        return cls(
            id=payload["id"],
            symbol=payload["symbol"],
            side=Side(payload["side"]),
            type=OrderType(payload["type"]),
            tif=TimeInForce(payload["tif"]),
            status=OrderStatus(payload["status"]),
            quantity=Decimal(payload["quantity"]),
            filled_quantity=Decimal(payload["filled_quantity"]),
            price=Decimal(payload["price"]) if payload.get("price") else None,
        )
