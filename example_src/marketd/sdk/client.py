"""A client with the reliability policy the server cannot provide for you.

Three mechanisms, and the distinction between them is the whole point:

**Pooling** amortises connection setup.  A TCP handshake per request is a
round trip you are paying for nothing.

**Retries** handle transient failures.  Two rules keep them from making an
outage worse: only retry what is *safe* to retry, and back off with jitter.
A 422 will fail identically forever - retrying it just multiplies load.

**The circuit breaker** handles non-transient failures.  When a dependency is
down, retrying is actively harmful: it keeps the caller's threads busy, hides
the failure behind latency, and hammers a service that is trying to recover.
The breaker fails fast instead, and probes occasionally to find out when the
dependency is back.

    closed  -> normal.  Count failures.
    open    -> fail immediately.  After a cooldown, go half-open.
    half-open -> let a limited number of probes through.  Success closes the
                 breaker; a failure opens it again.
"""

from __future__ import annotations

import random
from typing import Any

from ..errors import CircuitOpen, MarketError, ServiceUnavailable
from ..http.client import ClientResponse, Connection
from ..storage.pool import ResourcePool
from ..telemetry.metrics import Registry
from ..util import jsonx
from ..util.backoff import BackoffPolicy, retry_async
from ..util.clock import SYSTEM_CLOCK, Clock

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    __slots__ = ("_threshold", "_cooldown", "_half_open_max", "_clock",
                 "_state", "_failures", "_opened_at", "_probes", "_m_rejected", "_m_opened")

    def __init__(
        self,
        clock: Clock,
        registry: Registry,
        *,
        failure_threshold: int = 5,
        cooldown: float = 2.0,
        half_open_max: int = 2,
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown
        self._half_open_max = half_open_max
        self._clock = clock
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probes = 0
        self._m_rejected = registry.counter(
            "circuit_rejected_total", "calls refused by the breaker"
        )
        self._m_opened = registry.counter("circuit_opened_total", "times the breaker opened")

    @property
    def state(self) -> str:
        return self._state

    def before_call(self) -> None:
        """Raise if the call must not be made."""
        if self._state == OPEN:
            if self._clock.monotonic() - self._opened_at < self._cooldown:
                self._m_rejected.inc()
                raise CircuitOpen("circuit is open; the upstream is failing")
            self._state = HALF_OPEN
            self._probes = 0
        if self._state == HALF_OPEN and self._probes >= self._half_open_max:
            self._m_rejected.inc()
            raise CircuitOpen("circuit is half-open and probing")
        if self._state == HALF_OPEN:
            self._probes += 1

    def on_success(self) -> None:
        # A single success in half-open closes the breaker.  Requiring several
        # sounds safer but leaves the breaker open longer than the dependency is
        # actually broken.
        self._state = CLOSED
        self._failures = 0

    def on_failure(self) -> None:
        self._failures += 1
        if self._state == HALF_OPEN or self._failures >= self._threshold:
            if self._state != OPEN:
                self._m_opened.inc()
            self._state = OPEN
            self._opened_at = self._clock.monotonic()


class MarketClient:
    """Typed-ish access to the marketd API."""

    def __init__(
        self,
        host: str,
        port: int,
        api_key: str | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
        registry: Registry | None = None,
        pool_size: int = 8,
        timeout: float = 5.0,
        policy: BackoffPolicy | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.api_key = api_key
        self._clock = clock
        self._timeout = timeout
        self._registry = registry or Registry()
        self._policy = policy or BackoffPolicy(base=0.02, max_delay=0.5, max_attempts=3)
        self._rng = random.Random(1234)
        self._breaker = CircuitBreaker(clock, self._registry)
        self._pool: ResourcePool[Connection] = ResourcePool(
            self._connect,
            max_size=pool_size,
            is_alive=lambda connection: connection.alive,
            close=lambda connection: connection.close(),
            registry=self._registry,
            name="http",
        )
        self._m_requests = self._registry.counter("sdk_requests_total", "client requests")

    async def _connect(self) -> Connection:
        return await Connection.connect(self.host, self.port, self._timeout)

    @property
    def breaker_state(self) -> str:
        return self._breaker.state

    async def close(self) -> None:
        await self._pool.close()

    # ------------------------------------------------------------- transport
    async def request(
        self, method: str, path: str, payload: Any = None, *, idempotent: bool | None = None
    ) -> Any:
        """Send a request, retrying only when it is safe to.

        ``idempotent`` defaults to "true for GET/DELETE, false for POST".  A
        POST that times out may well have been applied, so replaying it blindly
        can place a second order.  The override exists because *this* API gives
        POST an idempotency key: with a ``client_order_id`` set, a retry is
        safe, and the caller is the one who knows.
        """
        if idempotent is None:
            idempotent = method in ("GET", "HEAD", "DELETE")

        body = jsonx.dumps(payload) if payload is not None else b""
        headers = {"accept": "application/json"}
        if body:
            headers["content-type"] = "application/json"
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        async def attempt(_number: int) -> ClientResponse:
            self._breaker.before_call()
            try:
                async with self._pool.borrow(self._timeout) as connection:
                    response = await connection.request(
                        method, path, headers=headers, body=body, timeout=self._timeout
                    )
            except (ServiceUnavailable, OSError):
                self._breaker.on_failure()
                raise
            # 5xx counts against the breaker; 4xx does not.  A wall of 400s
            # means the *client* is wrong, and tripping the breaker would hide
            # that behind a misleading "upstream down".
            if response.status >= 500:
                self._breaker.on_failure()
            else:
                self._breaker.on_success()
            return response

        def should_retry(exc: BaseException) -> bool:
            if isinstance(exc, CircuitOpen):
                return False  # the breaker already decided; do not fight it
            return idempotent and isinstance(exc, (ServiceUnavailable, OSError))

        self._m_requests.inc()
        response = await retry_async(
            attempt,
            self._policy,
            should_retry=should_retry,
            clock=self._clock,
            rng=self._rng,
        )

        parsed = response.json()
        if not response.ok:
            raise _as_error(response.status, parsed)
        return parsed

    # ---------------------------------------------------------------- verbs
    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def post(self, path: str, payload: Any, *, idempotent: bool = False) -> Any:
        return await self.request("POST", path, payload, idempotent=idempotent)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)

    # ------------------------------------------------------------ endpoints
    async def create_account(self, name: str, tier: str = "standard") -> dict[str, Any]:
        return await self.post("/v1/accounts", {"name": name, "tier": tier})

    async def deposit(self, account_id: str, asset: str, amount: str) -> dict[str, Any]:
        return await self.post(
            f"/v1/accounts/{account_id}/deposits", {"asset": asset, "amount": amount}
        )

    async def balances(self, account_id: str) -> list[dict[str, Any]]:
        return (await self.get(f"/v1/accounts/{account_id}/balances"))["data"]

    async def instruments(self) -> list[dict[str, Any]]:
        return (await self.get("/v1/instruments"))["data"]

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: str,
        price: str | None = None,
        *,
        order_type: str = "limit",
        tif: str = "gtc",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": symbol, "side": side, "quantity": quantity, "type": order_type, "tif": tif
        }
        if price is not None:
            payload["price"] = price
        if client_order_id is not None:
            payload["client_order_id"] = client_order_id
        # A client order id makes the POST idempotent, so a retry is safe.
        return await self.post("/v1/orders", payload, idempotent=client_order_id is not None)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self.delete(f"/v1/orders/{order_id}")

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self.get(f"/v1/orders/{order_id}")

    async def list_orders(self, **params: Any) -> dict[str, Any]:
        query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
        return await self.get(f"/v1/orders{'?' + query if query else ''}")

    async def book(self, symbol: str, depth: int = 10) -> dict[str, Any]:
        return await self.get(f"/v1/book/{symbol}?depth={depth}")

    async def trades(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        return (await self.get(f"/v1/trades/{symbol}?limit={limit}"))["data"]

    async def ticker(self, symbol: str) -> dict[str, Any]:
        return await self.get(f"/v1/ticker/{symbol}")

    async def health(self) -> dict[str, Any]:
        return await self.get("/healthz")

    def stats(self) -> dict[str, Any]:
        return {"pool": self._pool.stats(), "breaker": self._breaker.state}


def _as_error(status: int, payload: Any) -> MarketError:
    """Rebuild a server-side error from the wire representation."""
    details = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
    error = MarketError(details.get("message", f"HTTP {status}"), **details.get("details", {}))
    error.status = status  # type: ignore[misc]
    error.code = details.get("code", "http_error")  # type: ignore[misc]
    error.retryable = status >= 500  # type: ignore[misc]
    return error
