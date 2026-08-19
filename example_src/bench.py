#!/usr/bin/env python3
"""Microbenchmarks for the paths marked HOT PATH in the source.

The point is not the absolute numbers - they depend on your machine and your
interpreter.  The point is the *shape*: which operations are nanoseconds, which
are microseconds, and how far apart those are.  Optimising the wrong one is the
most common way to spend a week and gain nothing.

Each benchmark reports nanoseconds per operation and operations per second.

    python bench.py            # everything
    python bench.py match      # only benchmarks whose name contains "match"
"""

from __future__ import annotations

import sys
from decimal import ROUND_HALF_UP, Decimal
from time import perf_counter_ns

from marketd.api.router import Router
from marketd.core.book import OrderBook, RestingOrder
from marketd.core.matching import MatchingEngine
from marketd.core.models import (
    Order,
    OrderType,
    Side,
    TimeInForce,
    default_instruments,
)
from marketd.core.money import notional_units, to_units
from marketd.core.validation import Dec, EnumOf, Obj, Str, compile_schema, optional, required
from marketd.http.message import Response
from marketd.http.parser import parse_head, parse_query
from marketd.telemetry.metrics import Registry
from marketd.util import jsonx
from marketd.util.lru import LRUCache
from marketd.util.token_bucket import TokenBucket

RESULTS: list[tuple[str, float, str]] = []


def bench(name: str, iterations: int, note: str = ""):
    """Decorator: time ``fn`` over ``iterations`` and record ns/op."""

    def decorate(fn):
        def run() -> None:
            fn(3)  # warm up: let the interpreter specialise before measuring
            started = perf_counter_ns()
            fn(iterations)
            elapsed = perf_counter_ns() - started
            RESULTS.append((name, elapsed / iterations, note))

        run.__name__ = fn.__name__
        run.bench_name = name  # type: ignore[attr-defined]
        return run

    return decorate


# --------------------------------------------------------------------- setup
INSTRUMENTS = default_instruments()
BTC = INSTRUMENTS["BTC-USD"]
REGISTRY = Registry()

RAW_HEAD = (
    b"POST /v1/orders HTTP/1.1\r\nhost: api.example.com\r\n"
    b"authorization: Bearer key_abc.secret\r\ncontent-type: application/json\r\n"
    b"content-length: 96\r\nuser-agent: marketd-sdk/1.4\r\naccept: application/json\r\n\r\n"
)
QUERY = "symbol=BTC-USD&limit=50&status=open&cursor=b3JkX0FCQw"
ORDER_PAYLOAD = {
    "symbol": "BTC-USD", "side": "buy", "type": "limit", "tif": "gtc",
    "quantity": "1.50000000", "price": "30000.00",
}
ORDER_SCHEMA = compile_schema(
    Obj({
        "symbol": required(Str(min_len=3, max_len=17, pattern=r"^[A-Z0-9]{2,8}-[A-Z0-9]{2,8}$")),
        "side": required(EnumOf(Side)),
        "type": optional(EnumOf(OrderType), OrderType.LIMIT),
        "tif": optional(EnumOf(TimeInForce), TimeInForce.GTC),
        "quantity": required(Dec(minimum="0")),
        "price": optional(Dec(minimum="0")),
    })
)

ROUTER = Router(cache_size=1024)


async def _noop(request):  # pragma: no cover - never called
    return None


for pattern in (
    "/v1/orders", "/v1/orders/{order_id}", "/v1/accounts/{account_id}/balances",
    "/v1/book/{symbol}", "/v1/trades/{symbol}", "/v1/ticker/{symbol}",
    "/healthz", "/metrics", "/v1/instruments",
):
    ROUTER.get(pattern, _noop)


# ---------------------------------------------------------------- benchmarks
@bench("http: parse request head", 200_000, "7 headers")
def bench_parse_head(n: int) -> None:
    raw = RAW_HEAD
    for _ in range(n):
        parse_head(raw)


@bench("http: parse query string", 500_000, "4 parameters")
def bench_parse_query(n: int) -> None:
    raw = QUERY
    for _ in range(n):
        parse_query(raw)


@bench("http: render response", 300_000, "json body, 1 header")
def bench_render(n: int) -> None:
    response = Response.json({"id": "ord_abc", "status": "open", "price": "30000.00"})
    for _ in range(n):
        response.render(keep_alive=True, now=1_700_000_000.5)


@bench("router: resolve (cached)", 1_000_000, "hits the LRU")
def bench_route_cached(n: int) -> None:
    resolve = ROUTER.resolve
    for _ in range(n):
        resolve("GET", "/v1/orders/ord_abc123")


@bench("router: resolve (cold trie)", 300_000, "cache cleared each call")
def bench_route_cold(n: int) -> None:
    router = ROUTER
    for _ in range(n):
        router._cache.clear()
        router.resolve("GET", "/v1/accounts/acct_1/balances")


@bench("json: encode order payload", 300_000, "6 fields")
def bench_json_dumps(n: int) -> None:
    payload = ORDER_PAYLOAD
    for _ in range(n):
        jsonx.dumps(payload)


@bench("json: decode order payload", 300_000, "6 fields")
def bench_json_loads(n: int) -> None:
    raw = jsonx.dumps(ORDER_PAYLOAD)
    for _ in range(n):
        jsonx.loads(raw)


@bench("validation: compiled schema", 300_000, "6 fields, all valid")
def bench_validate(n: int) -> None:
    payload = ORDER_PAYLOAD
    validate = ORDER_SCHEMA
    for _ in range(n):
        validate(payload, "$")


@bench("validation: schema compile", 20_000, "the cost you pay once")
def bench_compile(n: int) -> None:
    schema = Obj({
        "symbol": required(Str(min_len=3, max_len=17)),
        "side": required(EnumOf(Side)),
        "quantity": required(Dec(minimum="0")),
    })
    for _ in range(n):
        schema.compile()


@bench("money: Decimal -> units", 500_000, "exact, rejects excess precision")
def bench_to_units(n: int) -> None:
    value = Decimal("30000.00")
    for _ in range(n):
        to_units(value, 2)


@bench("money: notional (int path)", 2_000_000, "price x qty, exact")
def bench_notional(n: int) -> None:
    price, qty = 3_000_000, 150_000_000
    for _ in range(n):
        notional_units(price, 2, qty, 8)


@bench("money: notional (Decimal path)", 500_000, "same sum, rounded to 8dp")
def bench_notional_decimal(n: int) -> None:
    # Quantizing is what makes this the same operation as the integer path:
    # a bare multiply leaves the result at whatever precision it happens to
    # land on, which is not a ledger amount.
    price, qty = Decimal("30000.00"), Decimal("1.50000000")
    quantum = Decimal("1E-8")
    for _ in range(n):
        _ = (price * qty).quantize(quantum, rounding=ROUND_HALF_UP)


@bench("money: accumulate 20 fills (int)", 100_000, "what settlement does")
def bench_accumulate_int(n: int) -> None:
    price, qty = 3_000_000, 150_000_000
    for _ in range(n):
        total = 0
        for _fill in range(20):
            total += notional_units(price, 2, qty, 8)


@bench("money: accumulate 20 fills (Decimal)", 100_000, "the same, in Decimal")
def bench_accumulate_decimal(n: int) -> None:
    price, qty = Decimal("30000.00"), Decimal("1.50000000")
    quantum = Decimal("1E-8")
    for _ in range(n):
        total = Decimal(0)
        for _fill in range(20):
            total += (price * qty).quantize(quantum, rounding=ROUND_HALF_UP)


@bench("cache: LRU get (hit)", 2_000_000)
def bench_lru(n: int) -> None:
    cache: LRUCache[str, int] = LRUCache(128)
    cache.put("key", 1)
    get = cache.get
    for _ in range(n):
        get("key")


@bench("ratelimit: token bucket check", 2_000_000)
def bench_bucket(n: int) -> None:
    bucket = TokenBucket(1e12, 1e12)
    now = 0.0
    for _ in range(n):
        now += 1e-6
        bucket.consume(now)


@bench("metrics: counter increment", 5_000_000)
def bench_counter(n: int) -> None:
    counter = REGISTRY.counter("bench_counter")
    inc = counter.inc
    for _ in range(n):
        inc()


@bench("metrics: histogram observe", 2_000_000, "16 buckets, bisect")
def bench_histogram(n: int) -> None:
    histogram = REGISTRY.histogram("bench_histogram")
    observe = histogram.observe
    for _ in range(n):
        observe(0.0007)


@bench("book: add resting order", 300_000, "into a 200-level book")
def bench_book_add(n: int) -> None:
    book = OrderBook("BTC-USD")
    for i in range(n):
        book.add(
            Side.BUY,
            RestingOrder(f"o{i}", "acct", 3_000_000 - (i % 200) * 50, 100_000, i),
        )


@bench("book: best_bid", 2_000_000, "after lazy heap cleanup")
def bench_best_bid(n: int) -> None:
    book = OrderBook("BTC-USD")
    for i in range(200):
        book.add(Side.BUY, RestingOrder(f"o{i}", "acct", 3_000_000 - i * 50, 100_000, i))
    best_bid = book.best_bid
    for _ in range(n):
        best_bid()


@bench("engine: match 1 level", 200_000, "one taker, one maker")
def bench_match_one(n: int) -> None:
    engine = MatchingEngine(INSTRUMENTS, Registry())
    qty = to_units("1", 8)
    price = to_units("30000", 2)
    for i in range(n):
        maker = Order(
            id=f"m{i}", account_id="maker", symbol="BTC-USD", side=Side.SELL,
            type=OrderType.LIMIT, tif=TimeInForce.GTC, qty_units=qty, price_units=price,
        )
        engine.submit(maker, 0.0)
        taker = Order(
            id=f"t{i}", account_id="taker", symbol="BTC-USD", side=Side.BUY,
            type=OrderType.LIMIT, tif=TimeInForce.GTC, qty_units=qty, price_units=price,
        )
        engine.submit(taker, 0.0)


@bench("engine: match 10 levels", 20_000, "taker sweeps ten price levels")
def bench_match_sweep(n: int) -> None:
    engine = MatchingEngine(INSTRUMENTS, Registry())
    qty = to_units("1", 8)
    for i in range(n):
        for level in range(10):
            engine.submit(
                Order(
                    id=f"m{i}_{level}", account_id="maker", symbol="BTC-USD", side=Side.SELL,
                    type=OrderType.LIMIT, tif=TimeInForce.GTC, qty_units=qty,
                    price_units=to_units("30000", 2) + level * 50,
                ),
                0.0,
            )
        engine.submit(
            Order(
                id=f"t{i}", account_id="taker", symbol="BTC-USD", side=Side.BUY,
                type=OrderType.LIMIT, tif=TimeInForce.GTC, qty_units=qty * 10,
                price_units=to_units("30000", 2) + 9 * 50,
            ),
            0.0,
        )


def main(argv: list[str]) -> int:
    selector = argv[1].lower() if len(argv) > 1 else ""
    benchmarks = [
        value for name, value in sorted(globals().items())
        if name.startswith("bench_") and callable(value) and hasattr(value, "bench_name")
    ]
    chosen = [b for b in benchmarks if not selector or selector in b.bench_name.lower()]
    if not chosen:
        print(f"no benchmark matches {selector!r}")
        return 1

    runtime = f"{sys.implementation.name} {sys.version.split()[0]}"
    print(f"running {len(chosen)} benchmark(s) on {runtime}\n")
    for benchmark in chosen:
        benchmark()

    width = max(len(name) for name, _, _ in RESULTS)
    print(f"{'benchmark'.ljust(width)}   {'ns/op':>10}   {'ops/sec':>13}   note")
    print("-" * (width + 50))
    for name, ns, note in RESULTS:
        print(f"{name.ljust(width)}   {ns:>10.1f}   {1e9 / ns:>13,.0f}   {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
