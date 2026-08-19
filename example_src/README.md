# marketd

A miniature trading venue in ~4,800 lines of dependency-free Python
(plus ~1,100 lines of commentary, across 56 modules).

It is a real service: an HTTP/1.1 server written on `asyncio`, a price-time
priority matching engine, a double-entry ledger, pre-trade risk checks, an
append-only event log with background consumers, and a client SDK with pooling,
retries and a circuit breaker. It runs, it is tested, and the books balance.

It exists to be **read**. The domain is complex enough to need real structure -
layers, caches, invariants, hot paths - but small enough to hold in your head.

```bash
python demo.py                  # boot a server, drive it end to end, narrate
python bench.py                 # microbenchmark every HOT PATH
python -m pytest tests -q       # 122 tests, ~0.6s
python -m marketd serve         # run it for real
python -m marketd routes        # print the route table
```

No dependencies beyond the standard library. Python 3.11+ (3.13 tested).

---

## The shape of it

```
                    HTTP/1.1 over asyncio            http/
                            |
    request_id -> tracing -> access_log -> errors     api/middleware.py
       -> metrics -> auth -> rate_limit
                            |
                    trie router + LRU cache           api/router.py
                            |
                 handlers: parse, call, present       api/handlers/
                            |
      +---------------------+---------------------+
      |                     |                     |
 AccountService       OrderService         MarketDataService    services/
      |                     |                     |
      |         risk -> reserve -> match -> settle |
      |                     |                     |
 +----+------+   +----------+--------+   +---------+--------+
 |  Ledger   |   |  MatchingEngine   |   |   OrderBook      |   core/
 +-----------+   +-------------------+   +------------------+
                            |
                   append-only event log                storage/
                            |
              post-trade worker, scheduler              workers/
```

Dependencies point inward. `core/` imports nothing from `api/` or `http/`,
which is why the matching engine can be tested with no server, no sockets and
no event loop - see `tests/test_book_and_matching.py`.

---

## Hot paths

Functions that run once per request, per order or per fill are marked in the
source:

```python
# --- HOT PATH ---------------------------------------------------------
```

`python bench.py` measures each one. On CPython 3.13:

| path | ns/op | file |
|---|---:|---|
| metrics: counter increment | 29 | `telemetry/metrics.py` |
| book: best_bid | 56 | `core/book.py` |
| cache: LRU get (hit) | 70 | `util/lru.py` |
| metrics: histogram observe | 77 | `telemetry/metrics.py` |
| ratelimit: token bucket check | 102 | `util/token_bucket.py` |
| money: notional (int path) | 151 | `core/money.py` |
| router: resolve (cached) | 373 | `api/router.py` |
| http: parse query string | 570 | `http/parser.py` |
| http: render response | 566 | `http/message.py` |
| book: add resting order | 742 | `core/book.py` |
| router: resolve (cold trie) | 835 | `api/router.py` |
| json: encode order payload | 1,108 | `util/jsonx.py` |
| http: parse request head | 2,285 | `http/parser.py` |
| validation: compiled schema | 2,452 | `core/validation.py` |
| engine: match 1 level | 4,274 | `core/matching.py` |
| engine: match 10 levels | 42,508 | `core/matching.py` |

Numbers move 10-20% between runs; treat them as ratios, not constants.

The ordering is the lesson. Header parsing and schema validation each cost more
than the entire order book operation they are protecting - so if a request path
is slow, that is where to look first, not at the matching engine.

### The techniques used, and where to find them

**Compile the fixed part, once.** The shape of a schema, a route table and a
query are all known before any data arrives. `core/validation.py` turns a
schema into closures at import time; `api/router.py` builds a segment trie;
`storage/query.py` folds filters into one predicate. The per-request cost drops
to "call the thing" - no traversal, no dispatch, no re-deciding.

**Resolve handles at wiring time.** `telemetry/metrics.py` returns a metric
*object* that callers keep. Looking a metric up by name and labels on every
observation is how instrumentation ends up costing more than the code it
measures.

**Cache what repeats, bound what you cache, and never cache a miss.**
`api/router.py` caches successful resolutions only - caching misses lets anyone
flood the cache with junk paths and evict the entries that matter.

**Be lazy about work nobody asked for.** `http/message.py` parses the query
string and the JSON body only when a handler touches them. The `Date` header is
formatted at most once per second.

**Skip the expensive call when you can prove it is unnecessary.**
`http/parser.py` checks for `%` before percent-decoding. The check is far
cheaper than the decode, and the overwhelming majority of tokens need neither.

**Amortise with laziness in data structures.** `core/book.py` cancels orders
with tombstones rather than an O(n) deque removal, and lets stale heap entries
be discarded when they surface. Both are amortised O(1).

**Measure before believing.** `core/money.py` documents that scaled integers
beat `Decimal` - and then shows the benchmark proving the usual "Decimal is 50x
slower" claim is wrong on modern CPython, and gives the four real reasons
ranked by how much they matter. Speed is the smallest one.

---

## Domain invariants

The properties the system is built to preserve, and where each is enforced:

**The books balance.** Every posting set sums to zero per asset, checked on
every write (`core/ledger.py`). Deposits come from a real external account and
fees go to a real fee account, so `Ledger.verify()` is meaningful - it returns
`{}` or the system is broken. A scheduled job runs it every five seconds, the
readiness probe fails on drift, and every test that touches money asserts it.

**Funds cannot be double-spent.** Balances have two pockets, `available` and
`reserved`. Placing an order moves funds into `reserved` *before* matching;
settling debits from `reserved`; cancelling hands it back. A resting order
therefore always has money behind it.

**A reservation always covers what the order can spend.** A limit buy reserves
at its limit price, so any fill is cheaper. A market buy has no limit price, so
`MatchingEngine.estimate` prices the walk before a single unit moves
(`core/matching.py`). Settlement asserts the invariant rather than assuming it.

**Fills happen at the maker's price.** A resting quote is a commitment; a taker
that crosses it gets the advertised price and cannot do better.

**Fill-or-kill is all or nothing.** Checked before the book is touched, because
unwinding a partially applied match is the kind of code that eats a weekend.

**Retries are safe.** A repeated `client_order_id` returns the original order
instead of placing a second one (`services/orders.py`).

---

## Things worth stealing

- **`core/validation.py`** - the compile-to-closures pattern, and error
  accumulation so a client learns about every bad field in one round trip.
- **`storage/pool.py`** - an async resource pool that gets waiter queuing,
  cancellation and liveness checks right. Most hand-rolled pools leak a slot on
  timeout; there is a test for exactly that.
- **`util/backoff.py`** - full jitter, and a retry predicate that takes the
  *exception instance* rather than a tuple of types, because "retryable" is a
  property of the failure, not its class.
- **`sdk/client.py`** - the distinction between pooling, retries and circuit
  breaking, and why a 4xx must not trip the breaker.
- **`workers/scheduler.py`** - drift, overlap and crash recovery, which is most
  of what a scheduler is for.
- **`storage/eventlog.py`** - slow consumers get dropped and told about it,
  rather than being allowed to block the writer.

---

## Security decisions, stated on purpose

- **404, not 403, for other people's resources.** A 403 confirms the resource
  exists (`services/accounts.py`, `services/orders.py`).
- **Auth runs before routing**, so an anonymous caller cannot map the API by
  probing for 404s. There is a test asserting both halves of this. The
  consequence - the public allow-list can only name literal paths, so the
  market-data routes need a key - is documented where it bites, in
  `api/handlers/market.py`.
- **API keys are stored as HMAC digests** and compared in constant time
  (`api/auth.py`). The verification cache is keyed by digest, not by token.
- **Chunked transfer encoding is refused**, not partially supported. The
  interaction between `Transfer-Encoding` and `Content-Length` is where request
  smuggling lives (`http/parser.py`).
- **Public endpoints are an allow-list**, never a deny-list. Forgetting to add
  an endpoint to a deny-list publishes it to the world.
- **Internal error messages never reach the client.** A 500 says "internal
  error" and the traceback goes to the log (`api/middleware.py`).
- **Cursors are signed.** They are not secret, but a tampered cursor is fed
  straight into a query (`api/pagination.py`).
- **Metric labels use the route pattern, not the path.** Labelling by path
  creates one time series per order id and takes the metrics backend down.

---

## The single-threaded assumption

The whole service runs on one event loop thread. That is what lets the
repositories skip locking, the id generator use a plain counter, and
`OrderService.place` treat risk-check-through-settlement as atomic.

It is written down where it matters, and it comes with one rule, stated in
`services/orders.py`:

> Do not add an `await` between the risk check and the end of settlement.

If you port this to threads, that assumption - not the data structures - is
what has to change first.

---

## Layout

```
marketd/
  config.py            frozen settings, parsed from env at boot
  errors.py            error taxonomy; status and code live on the exception
  bootstrap.py         composition root - every dependency, constructed once
  http/                parser, request/response, server loop, client connection
  api/                 router, middleware, auth, schemas, pagination, handlers
  services/            orchestration: place order, deposit, snapshot book
  core/                money, models, validation, book, matching, ledger, risk
  storage/             repositories, query builder, event log, resource pool
  workers/             work queue, post-trade consumer, scheduler
  telemetry/           metrics, tracing, structured logging
  sdk/                 client with pooling, retries, circuit breaker
  util/                clock, ids, backoff, LRU, token bucket, JSON
tests/                 122 tests, no plugins - conftest runs async tests itself
demo.py                end-to-end walkthrough over real sockets
bench.py               hot-path microbenchmarks
```

## The API

| method | path | notes |
|---|---|---|
| `GET` | `/` | route table |
| `GET` | `/healthz` `/readyz` | liveness / readiness (readiness checks the ledger) |
| `GET` | `/metrics` | Prometheus text format |
| `POST` | `/v1/accounts` | public - how you get your first key |
| `GET` | `/v1/accounts/{id}` `/v1/accounts/{id}/balances` | |
| `POST` | `/v1/accounts/{id}/deposits` | |
| `POST` | `/v1/orders` | limit/market, gtc/ioc/fok, idempotent |
| `GET` | `/v1/orders` | cursor paginated |
| `GET` `DELETE` | `/v1/orders/{id}` | |
| `GET` | `/v1/instruments` | public |
| `GET` | `/v1/book/{symbol}` `/v1/trades/{symbol}` `/v1/ticker/{symbol}` | public data, but needs a key - see `api/handlers/market.py` |
| `GET` | `/v1/admin/stats` `/v1/admin/traces` | requires the `admin` scope |

```bash
curl -s localhost:8080/v1/accounts -H 'content-type: application/json' \
     -d '{"name":"Acme Trading"}'

curl -s localhost:8080/v1/orders -H "authorization: Bearer $KEY" \
     -H 'content-type: application/json' \
     -d '{"symbol":"BTC-USD","side":"buy","quantity":"1.5","price":"30000.00"}'
```
