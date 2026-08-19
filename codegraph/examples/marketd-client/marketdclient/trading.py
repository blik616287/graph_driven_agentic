"""The client's trading surface.

Every method here corresponds to one endpoint on the venue. Keeping that
mapping one-to-one is what makes the correspondence checkable — by a reader, and
by a graph that links the two codebases.
"""

from __future__ import annotations

from decimal import Decimal

from .core.models import Instrument, Order, OrderType, Side, TimeInForce


class TradingClient:
    """Calls the venue's ``/v1`` surface."""

    def __init__(self, transport, account_id: str) -> None:
        self._transport = transport
        self.account_id = account_id

    def list_instruments(self) -> list[Instrument]:
        payload = self._transport.get("/v1/instruments")
        return [Instrument.from_payload(entry) for entry in payload["data"]]

    def place_order(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        price: Decimal | None = None,
        *,
        order_type: OrderType = OrderType.LIMIT,
        tif: TimeInForce = TimeInForce.GTC,
        client_order_id: str | None = None,
    ) -> Order:
        """Submit an order.

        ``client_order_id`` is what makes a retry safe: the venue treats a
        repeat as a lookup rather than a second order.
        """
        body = {
            "symbol": symbol,
            "side": side.value,
            "type": order_type.value,
            "tif": tif.value,
            "quantity": str(quantity),
        }
        if price is not None:
            body["price"] = str(price)
        if client_order_id is not None:
            body["client_order_id"] = client_order_id
        return Order.from_payload(self._transport.post("/v1/orders", body))

    def cancel_order(self, order_id: str) -> Order:
        return Order.from_payload(self._transport.delete(f"/v1/orders/{order_id}"))

    def get_order(self, order_id: str) -> Order:
        return Order.from_payload(self._transport.get(f"/v1/orders/{order_id}"))

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        query = f"?symbol={symbol}" if symbol else ""
        payload = self._transport.get(f"/v1/orders{query}")
        return [o for o in (Order.from_payload(e) for e in payload["data"]) if o.is_open]
