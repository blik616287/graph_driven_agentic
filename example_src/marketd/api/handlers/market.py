"""``/v1/instruments``, ``/v1/book`` and the tape.

This is public *information* - no handler here looks at the principal, and the
data is the same for every caller.  It still requires an API key, and that is
worth understanding rather than working around.

Authentication runs before routing (see ``api/middleware.py``), which is what
stops an anonymous caller from mapping the API by probing for 404s.  The public
allow-list in ``api/app.py`` is therefore matched against the *literal* request
path, and none of these routes have one - they all carry a ``{symbol}``.

Two ways out, if you want an open tape: match the allow-list on a path prefix,
or move authentication inside the router so it can see the resolved pattern.
Both trade away some of the enumeration resistance.  The choice made here is to
keep the key requirement, because it also gives per-key rate limiting on reads -
"public" and "free to hammer" are not the same thing.
"""

from __future__ import annotations

from ...http.message import Request, Response
from ..schemas import present_book, present_instrument, present_trade


def register(router, container) -> None:
    market = container.market_data_service
    settings = container.settings

    async def list_instruments(request: Request) -> Response:
        return Response.json(
            {"data": [present_instrument(item) for item in market.instruments()]}
        )

    async def get_book(request: Request) -> Response:
        symbol = request.path_params["symbol"].upper()
        depth = request.int_param("depth", settings.book_depth_default)
        instrument = market.instrument(symbol)
        return Response.json(present_book(market.book(symbol, depth), instrument))

    async def get_trades(request: Request) -> Response:
        symbol = request.path_params["symbol"].upper()
        limit = min(request.int_param("limit", 50), 500)
        instrument = market.instrument(symbol)
        return Response.json(
            {"data": [present_trade(trade, instrument) for trade in market.trades(symbol, limit)]}
        )

    async def get_ticker(request: Request) -> Response:
        symbol = request.path_params["symbol"].upper()
        return Response.json(market.ticker(symbol))

    router.get("/v1/instruments", list_instruments, name="list_instruments")
    router.get("/v1/book/{symbol}", get_book, name="get_book")
    router.get("/v1/trades/{symbol}", get_trades, name="get_trades")
    router.get("/v1/ticker/{symbol}", get_ticker, name="get_ticker")
