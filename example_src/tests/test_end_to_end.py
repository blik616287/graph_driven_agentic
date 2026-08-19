"""The full stack: real sockets, real SDK, real server.

Slower than the unit tests and worth every millisecond - this is the only layer
where a mistake in the wiring shows up.
"""

from __future__ import annotations

import asyncio

import pytest

from marketd.api.app import create_app
from marketd.config import Settings
from marketd.http.server import HTTPServer
from marketd.sdk import MarketClient
from marketd.bootstrap import build_container
from marketd.util.clock import SYSTEM_CLOCK

class _raw_connection:
    """Borrow a bare HTTP connection, for endpoints that do not return JSON."""

    def __init__(self, port: int) -> None:
        self._port = port

    async def __aenter__(self):
        from marketd.http.client import Connection

        self._connection = await Connection.connect("127.0.0.1", self._port)
        return self._connection

    async def __aexit__(self, *exc) -> bool:
        await self._connection.close()
        return False


class Venue:
    """A running server plus the container behind it."""

    def __init__(self, container, server):
        self.container = container
        self.server = server
        self.port = server.port
        self._clients: list[MarketClient] = []

    def client(self, api_key=None) -> MarketClient:
        created = MarketClient("127.0.0.1", self.port, api_key, timeout=5.0)
        self._clients.append(created)
        return created

    async def funded_client(self, name: str, deposits: dict[str, str]):
        """Open an account, fund it, and return ``(account, client)``."""
        anonymous = self.client()
        account = await anonymous.create_account(name)
        client = self.client(account["api_key"])
        for asset, amount in deposits.items():
            await client.deposit(account["id"], asset, amount)
        return account, client

    async def aclose(self) -> None:
        for client in self._clients:
            await client.close()
        await self.server.close()
        await self.container.stop_workers()


class venue:
    """``async with venue() as v:`` - starts a server, always tears it down."""

    async def __aenter__(self) -> Venue:
        settings = Settings(
            port=0, api_secret="test-secret",
            rate_limit_rps=10_000.0, rate_limit_burst=10_000.0, log_level="CRITICAL",
        )
        # The real clock: the server measures its timeouts with it.
        container = build_container(settings, SYSTEM_CLOCK)
        app = create_app(container)
        server = HTTPServer(app, settings, container.registry, SYSTEM_CLOCK)
        await container.start_workers()
        await server.start()
        self._venue = Venue(container, server)
        return self._venue

    async def __aexit__(self, *exc) -> bool:
        await self._venue.aclose()
        return False


async def test_health_and_index_are_public():
    async with venue() as running:
        client = running.client()
        assert (await client.health())["status"] == "ok"
        index = await client.get("/")
        assert index["service"] == "marketd"
        assert any(route["path"] == "/v1/orders" for route in index["routes"])
        assert (await client.get("/readyz"))["ledger_balanced"] is True


async def test_a_trade_travels_the_whole_stack():
  async with venue() as running:
        maker_account, maker = await running.funded_client("Maker", {"BTC": "5"})
        taker_account, taker = await running.funded_client("Taker", {"USD": "200000"})
        resting = await maker.place_order("BTC-USD", "sell", "1", "30000.00")
        assert resting["status"] == "open"

        book = await taker.book("BTC-USD")
        assert book["best_ask"] == "30000.00"

        filled = await taker.place_order("BTC-USD", "buy", "1", "30000.00")
        assert filled["status"] == "filled"
        assert filled["average_price"] == "30000.00"

        await running.container.post_trade.idle()
        trades = await taker.trades("BTC-USD")
        assert len(trades) == 1
        assert trades[0]["price"] == "30000.00"
        assert trades[0]["taker_side"] == "buy"

        balances = {b["asset"]: b for b in await taker.balances(taker_account["id"])}
        assert balances["BTC"]["available"] == "1.00000000"
        assert running.container.ledger.verify() == {}


async def test_unauthenticated_requests_are_refused():
    async with venue() as running:
        client = running.client()
        with pytest.raises(Exception) as caught:
            await client.get("/v1/orders")
        assert caught.value.status == 401


async def test_validation_errors_name_every_bad_field():
    async with venue() as running:
        _, client = await running.funded_client("Trader", {"USD": "1000"})
        with pytest.raises(Exception) as caught:
            await client.post("/v1/orders", {"symbol": "nope", "side": "sideways", "quantity": "0"})
        error = caught.value
        assert error.status == 422
        assert error.code == "validation_failed"
        assert set(error.details) == {"$.symbol", "$.side", "$.quantity"}


async def test_unknown_route_and_wrong_method():
    async with venue() as running:
        # Authenticated on purpose: auth runs *before* routing, so an anonymous
        # request to an unknown path gets 401, not 404.  That ordering is
        # deliberate - it stops anyone without a key from mapping the API.
        _, client = await running.funded_client("Explorer", {})
        with pytest.raises(Exception) as caught:
            await client.get("/v1/nothing-here")
        assert caught.value.status == 404

        with pytest.raises(Exception) as caught:
            await client.request("DELETE", "/v1/instruments")
        assert caught.value.status == 405


async def test_pagination_walks_every_order_exactly_once():
  async with venue() as running:
        account, client = await running.funded_client("Pager", {"USD": "500000"})
        placed = []
        for i in range(12):
            order = await client.place_order("BTC-USD", "buy", "0.01", f"{25000 + i * 50}.00")
            placed.append(order["id"])

        seen, cursor = [], None
        for _page in range(10):
            page = await client.list_orders(limit=5, cursor=cursor)
            seen.extend(row["id"] for row in page["data"])
            cursor = page["page"]["next_cursor"]
            if not cursor:
                break

        assert len(seen) == len(set(seen)) == 12
        assert set(seen) == set(placed)


async def test_concurrent_orders_keep_the_books_balanced():
  """Sixty orders in flight at once, over a pooled set of connections."""
  async with venue() as running:
        maker_account, maker = await running.funded_client("Maker", {"BTC": "20", "USD": "500000"})
        taker_account, taker = await running.funded_client("Taker", {"BTC": "20", "USD": "500000"})
        await asyncio.gather(
            *[
                maker.place_order("BTC-USD", "sell", "0.1", f"{30000 + i * 50}.00")
                for i in range(30)
            ]
        )
        results = await asyncio.gather(
            *[
                taker.place_order("BTC-USD", "buy", "0.1", f"{30000 + i * 50}.00")
                for i in range(30)
            ],
            return_exceptions=True,
        )
        assert all(not isinstance(r, Exception) for r in results)
        assert running.container.ledger.verify() == {}

        stats = taker.stats()
        assert stats["pool"]["in_use"] == 0
        assert stats["breaker"] == "closed"


async def test_metrics_endpoint_is_public_and_prometheus_shaped():
    async with venue() as running:
        client = running.client()
        await client.health()
        # /metrics is text, not JSON, so go under the SDK's JSON decoding.
        async with _raw_connection(running.port) as connection:
            response = await connection.request("GET", "/metrics")
        assert response.status == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.body.decode()
        assert "# TYPE http_request_seconds histogram" in body
        assert "http_requests_total{" in body


async def test_anonymous_callers_cannot_enumerate_routes():
    """The flip side of the test above, asserted directly."""
    async with venue() as running:
        client = running.client()
        with pytest.raises(Exception) as caught:
            await client.get("/v1/nothing-here")
        assert caught.value.status == 401


async def test_keep_alive_reuses_one_connection():
    async with venue() as running, _raw_connection(running.port) as connection:
        for _ in range(5):
            response = await connection.request("GET", "/healthz")
            assert response.status == 200
            assert response.headers["connection"] == "keep-alive"
        assert connection.requests_served == 5


async def test_oversized_body_is_refused_before_it_is_read():
    async with venue() as running, _raw_connection(running.port) as connection:
        limit = running.container.settings.max_body_bytes
        response = await connection.request(
            "POST", "/v1/accounts",
            headers={"content-type": "application/json"},
            body=b"x" * (limit + 1),
        )
        assert response.status == 413


async def test_malformed_request_gets_one_reply_then_the_socket_closes():
    """A parse failure desynchronises the stream, so the connection must end."""
    async with venue() as running:
        reader, writer = await asyncio.open_connection("127.0.0.1", running.port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 400 Bad Request")
            assert b"connection: close" in head
            length = int(dict(
                line.split(b": ", 1) for line in head.split(b"\r\n")[1:] if b": " in line
            )[b"content-length"])
            await reader.readexactly(length)
            assert await reader.read(1) == b""  # server closed, as it must
        finally:
            writer.close()
            await writer.wait_closed()


async def test_market_data_requires_a_key_because_auth_precedes_routing():
    """Pins the trade-off documented in api/handlers/market.py."""
    async with venue() as running:
        anonymous = running.client()
        with pytest.raises(Exception) as caught:
            await anonymous.book("BTC-USD")
        assert caught.value.status == 401

        # /v1/instruments has no path parameter, so it *is* on the allow-list.
        assert await anonymous.instruments()

        _, client = await running.funded_client("Reader", {})
        assert (await client.book("BTC-USD"))["symbol"] == "BTC-USD"
        assert await client.trades("BTC-USD") == []
        assert (await client.ticker("BTC-USD"))["last_price"] is None
