"""The middleware chain.

A middleware is ``async (request, next) -> Response``.  The chain is folded
**once**, at start-up, into a single nested callable - so dispatching a request
does not walk a list, check indices or allocate a per-request iterator.

Order is not cosmetic.  Reading top to bottom is reading the order in which a
request is processed:

    request_id -> tracing -> access_log -> errors -> metrics -> auth -> rate_limit

``errors`` sits *outside* auth and rate limiting so their exceptions become
proper responses, and *inside* access_log so every request is logged with the
status it actually returned.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..errors import MarketError, RateLimited
from ..http.message import Request, Response
from ..telemetry.logging import get_logger
from ..telemetry.metrics import Registry
from ..telemetry.tracing import Tracer
from ..util.token_bucket import BucketRegistry

Handler = Callable[[Request], Awaitable[Response]]
Middleware = Callable[[Request, Handler], Awaitable[Response]]

_logger = get_logger("api")


def build_chain(handler: Handler, middlewares: list[Middleware]) -> Handler:
    """Fold ``middlewares`` around ``handler``, outermost first.

    Each ``functools.partial``-like closure is created once here; the returned
    callable is what every request runs through.
    """
    chained = handler
    for middleware in reversed(middlewares):
        chained = _bind(middleware, chained)
    return chained


def _bind(middleware: Middleware, next_handler: Handler) -> Handler:
    async def invoke(request: Request) -> Response:
        return await middleware(request, next_handler)

    invoke.__name__ = getattr(middleware, "__name__", "middleware")
    return invoke


def request_id_middleware(ids) -> Middleware:
    """Give every request an id, honouring one the client supplied."""

    async def middleware(request: Request, next_handler: Handler) -> Response:
        request_id = request.header("x-request-id") or ids.next_token("req")
        request.ctx["request_id"] = request_id
        response = await next_handler(request)
        response.headers["x-request-id"] = request_id
        return response

    return middleware


def tracing_middleware(tracer: Tracer) -> Middleware:
    """Open the root span.  Everything downstream nests inside it."""

    async def middleware(request: Request, next_handler: Handler) -> Response:
        with tracer.span("http.request", method=request.method, path=request.path) as span:
            request.ctx["span"] = span
            response = await next_handler(request)
            span.set(status=response.status, bytes=len(response.body))
            if response.status >= 500:
                span.status = "error"
            response.headers["x-trace-id"] = span.trace_id
            return response

    return middleware


def error_middleware() -> Middleware:
    """Turn exceptions into responses.  The last line of defence."""

    async def middleware(request: Request, next_handler: Handler) -> Response:
        try:
            return await next_handler(request)
        except MarketError as exc:
            response = Response.json(exc.payload(), status=exc.status)
            # Some errors carry protocol obligations of their own.
            if isinstance(exc, RateLimited):
                response.headers["retry-after"] = f"{max(1, int(exc.retry_after + 0.5))}"
            allow = exc.details.get("allow")
            if allow:
                response.headers["allow"] = allow
            if exc.status >= 500:
                _logger.error("request failed", extra={"path": request.path, "code": exc.code})
            return response
        except Exception:
            # An unexpected exception is a bug.  Log it with the traceback, and
            # tell the client nothing beyond "we broke" - internal messages leak
            # paths, queries and versions.
            _logger.exception("unhandled error", extra={"path": request.path})
            return Response.json(
                {"error": {"code": "internal_error", "message": "internal error"}}, status=500
            )

    return middleware


def metrics_middleware(registry: Registry) -> Middleware:
    """Count and time requests, labelled by route *pattern*.

    Labelling by ``request.path`` would create one time series per order id and
    take the metrics backend down - the classic cardinality explosion.  The
    pattern is put into ``ctx`` by the dispatcher.
    """
    from time import perf_counter

    latency = registry.histogram("http_request_seconds", "request latency")
    in_flight = registry.gauge("http_requests_in_flight", "requests being handled")
    by_status: dict[tuple[str, int], object] = {}

    async def middleware(request: Request, next_handler: Handler) -> Response:
        started = perf_counter()
        in_flight.inc()
        try:
            response = await next_handler(request)
            status = response.status
            return response
        except MarketError as exc:
            status = exc.status
            raise
        except Exception:
            status = 500
            raise
        finally:
            in_flight.dec()
            latency.observe(perf_counter() - started)
            route = request.ctx.get("route_pattern", "unmatched")
            key = (route, status)
            counter = by_status.get(key)
            if counter is None:
                counter = registry.counter(
                    "http_requests_total", "requests by route and status",
                    route=route, status=str(status),
                )
                by_status[key] = counter
            counter.inc()

    return middleware


def access_log_middleware(clock) -> Middleware:
    """One structured line per request."""

    async def middleware(request: Request, next_handler: Handler) -> Response:
        started = clock.monotonic()
        response = await next_handler(request)
        _logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.path,
                "status": response.status,
                "duration_ms": round((clock.monotonic() - started) * 1000, 3),
                "remote": request.remote,
                "request_id": request.ctx.get("request_id"),
                "account_id": request.ctx.get("account_id"),
            },
        )
        return response

    return middleware


def auth_middleware(keys, public_paths: frozenset[str]) -> Middleware:
    """Authenticate everything except an explicit allow-list.

    An allow-list, never a deny-list: forgetting to add a new endpoint to a
    deny-list publishes it to the world, while forgetting to add it to an
    allow-list merely makes it require a key.
    """

    async def middleware(request: Request, next_handler: Handler) -> Response:
        if request.path not in public_paths:
            principal = keys.authenticate(request.header("authorization"))
            request.ctx["principal"] = principal
            request.ctx["account_id"] = principal.account_id
        return await next_handler(request)

    return middleware


def rate_limit_middleware(
    buckets: BucketRegistry, registry: Registry, clock
) -> Middleware:
    """Token-bucket limit, per principal and per remote address.

    Unauthenticated requests are limited by source address; authenticated ones
    by key.  Keying only on address would let one NAT'd office share one
    budget, and keying only on key would leave the login path unprotected.
    """
    rejected = registry.counter("rate_limit_rejected_total", "requests rejected by rate limit")

    async def middleware(request: Request, next_handler: Handler) -> Response:
        principal = request.ctx.get("principal")
        key = principal.key_id if principal is not None else request.remote.rsplit(":", 1)[0]
        allowed, remaining, retry_after = buckets.check(key, clock.monotonic())
        if not allowed:
            rejected.inc()
            raise RateLimited(
                "rate limit exceeded", retry_after=retry_after, limit=int(buckets.rate)
            )
        response = await next_handler(request)
        response.headers["x-ratelimit-limit"] = str(int(buckets.rate))
        response.headers["x-ratelimit-remaining"] = str(int(remaining))
        return response

    return middleware
