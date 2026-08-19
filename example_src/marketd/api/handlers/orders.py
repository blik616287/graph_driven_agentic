"""``/v1/orders`` - the trading surface."""

from __future__ import annotations

from ...http.message import Request, Response
from ..pagination import paginate
from ..schemas import PLACE_ORDER, present_order


def register(router, container) -> None:
    orders = container.order_service
    codec = container.cursor_codec
    settings = container.settings

    async def place_order(request: Request) -> Response:
        principal = request.ctx["principal"]
        principal.require("trade")
        payload = PLACE_ORDER(request.json(), "$")
        order = orders.place(principal, payload)
        instrument = orders.instrument(order.symbol)
        return Response.json(
            present_order(order, instrument),
            status=201,
            headers={"location": f"/v1/orders/{order.id}"},
        )

    async def get_order(request: Request) -> Response:
        order = orders.get(request.ctx["principal"], request.path_params["order_id"])
        return Response.json(present_order(order, orders.instrument(order.symbol)))

    async def cancel_order(request: Request) -> Response:
        principal = request.ctx["principal"]
        principal.require("trade")
        order = orders.cancel(principal, request.path_params["order_id"])
        return Response.json(present_order(order, orders.instrument(order.symbol)))

    async def list_orders(request: Request) -> Response:
        principal = request.ctx["principal"]
        limit = min(
            request.int_param("limit", settings.default_page_size), settings.max_page_size
        )
        cursor = request.param("cursor")
        rows = orders.list_for_account(
            principal.account_id,
            symbol=request.param("symbol"),
            status=request.param("status"),
            # The cursor is the last id of the previous page; ids sort by time,
            # so "older than this" is the whole of the query.
            before_id=codec.decode(cursor) if cursor else None,
            limit=limit,
        )
        page = paginate(rows, limit=limit, codec=codec)
        return Response.json(
            page.to_dict(lambda order: present_order(order, orders.instrument(order.symbol)))
        )

    router.post("/v1/orders", place_order, name="place_order")
    router.get("/v1/orders", list_orders, name="list_orders")
    router.get("/v1/orders/{order_id}", get_order, name="get_order")
    router.delete("/v1/orders/{order_id}", cancel_order, name="cancel_order")
