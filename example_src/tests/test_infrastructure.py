"""Utilities, storage and workers.

These are the pieces most likely to be lifted out of this project and reused, so
they get tested on their own terms rather than only through the service.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from marketd.core.models import Order, OrderStatus, OrderType, Side, TimeInForce
from marketd.errors import CircuitOpen, Conflict, NotFound, ServiceUnavailable
from marketd.sdk.client import CircuitBreaker
from marketd.storage.eventlog import EventLog
from marketd.storage.memstore import MemoryRepository
from marketd.storage.pool import ResourcePool
from marketd.storage.query import Query
from marketd.telemetry.metrics import Registry
from marketd.telemetry.tracing import Tracer, current_trace_id
from marketd.util.backoff import BackoffPolicy, retry_async
from marketd.util.idgen import IdGenerator
from marketd.util.lru import LRUCache, memoize
from marketd.util.token_bucket import BucketRegistry, TokenBucket
from marketd.workers.queue import WorkQueue
from marketd.workers.scheduler import Scheduler


# ------------------------------------------------------------------- caching
def test_lru_evicts_the_least_recently_used():
    cache: LRUCache[str, int] = LRUCache(maxsize=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")            # "a" is now the most recent
    cache.put("c", 3)         # so "b" is what leaves
    assert "b" not in cache and "a" in cache
    assert cache.stats()["evictions"] == 1


def test_lru_honours_its_ttl():
    cache: LRUCache[str, int] = LRUCache(maxsize=4, ttl=10.0)
    cache.put("k", 1, now=0.0)
    assert cache.get("k", now=9.9) == 1
    assert cache.get("k", now=10.0) is None


def test_memoize_caches_none_correctly():
    """A cached ``None`` must not read as a miss."""
    calls = []

    @memoize(maxsize=8)
    def sometimes_none(value):
        calls.append(value)
        return None if value else "x"

    sometimes_none(0)
    sometimes_none(0)
    assert calls == [0]


# ---------------------------------------------------------------- rate limits
def test_token_bucket_refills_over_time():
    bucket = TokenBucket(capacity=2, rate=2, now=0.0)
    assert bucket.consume(0.0) and bucket.consume(0.0)
    assert not bucket.consume(0.0)
    assert bucket.consume(0.5)                     # 0.5s at 2/s = one token
    assert bucket.retry_after(0.5) == pytest.approx(0.5)


def test_bucket_registry_keeps_keys_apart():
    registry = BucketRegistry(capacity=1, rate=1, maxkeys=8)
    assert registry.check("alice", 0.0)[0] is True
    assert registry.check("alice", 0.0)[0] is False
    assert registry.check("bob", 0.0)[0] is True   # separate budget


# -------------------------------------------------------------------- backoff
def test_backoff_is_bounded_and_jittered():
    policy = BackoffPolicy(base=0.1, factor=2.0, max_delay=1.0, max_attempts=6)
    rng = random.Random(0)
    delays = [policy.delay_for(attempt, rng) for attempt in range(1, 7)]
    assert delays[0] == 0.0                        # the first attempt is free
    assert all(0.0 <= delay <= 1.0 for delay in delays)
    assert len(set(delays[1:])) > 1                # jitter, not a fixed ramp


async def test_retry_stops_when_the_predicate_says_no(clock):
    attempts = []

    async def operation(attempt):
        attempts.append(attempt)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        await retry_async(
            operation,
            BackoffPolicy(max_attempts=5),
            should_retry=lambda exc: not isinstance(exc, ValueError),
            clock=clock,
        )
    assert attempts == [1]


# ---------------------------------------------------------------------- ids
def test_ids_stay_monotonic_across_a_sequence_rollover(clock):
    ids = IdGenerator(node_id=3, clock=clock)
    generated = [ids.next_id() for _ in range(5_000)]
    assert generated == sorted(generated)
    assert len(set(generated)) == len(generated)


def test_id_carries_its_creation_time(clock):
    ids = IdGenerator(node_id=1, clock=clock)
    identifier = ids.next_id()
    assert abs(IdGenerator.timestamp_ms(identifier) - clock.now() * 1000) < 2


# ------------------------------------------------------------------- storage
def order(order_id, account, symbol, qty, status=OrderStatus.OPEN):
    return Order(
        id=order_id, account_id=account, symbol=symbol, side=Side.BUY,
        type=OrderType.LIMIT, tif=TimeInForce.GTC, qty_units=qty, price_units=100,
        status=status,
    )


@pytest.fixture
def repo():
    store: MemoryRepository[Order] = MemoryRepository(
        "order",
        lambda row: row.id,
        {"account": lambda row: row.account_id, "symbol": lambda row: row.symbol},
    )
    for index in range(6):
        symbol = "BTC-USD" if index % 3 else "ETH-USD"
        store.add(order(f"o{index}", f"a{index % 2}", symbol, 100 + index))
    return store


def test_index_lookup_returns_only_matching_rows(repo):
    assert {row.id for row in repo.find_by("account", "a0")} == {"o0", "o2", "o4"}


def test_undeclared_index_is_an_error_not_a_silent_scan(repo):
    with pytest.raises(KeyError, match="no index"):
        repo.find_by("status", OrderStatus.OPEN)


def test_indexes_are_maintained_on_delete(repo):
    repo.delete("o0")
    assert {row.id for row in repo.find_by("account", "a0")} == {"o2", "o4"}
    assert repo.get("o0") is None


def test_optimistic_versioning_detects_a_lost_update(repo):
    row = repo.get("o1")
    repo.update(row, expected_version=1)
    with pytest.raises(Conflict):
        repo.update(row, expected_version=99)


def test_missing_row_raises_not_found(repo):
    with pytest.raises(NotFound):
        repo.require("nope")


def test_query_filters_sorts_and_limits(repo):
    query = (
        Query[Order]()
        .eq("symbol", "BTC-USD")
        .where("qty_units", "ge", 101)
        .order_by("qty_units", descending=True)
        .limit(2)
    )
    result = repo.query(query)
    assert [row.qty_units for row in result] == [105, 104]


def test_query_compiles_once_and_reruns(repo):
    execute = Query[Order]().eq("account_id", "a1").compile()
    assert len(execute(repo)) == len(execute(repo)) == 3


def test_unknown_operator_is_rejected_at_build_time():
    with pytest.raises(ValueError, match="unsupported operator"):
        Query().where("id", "regex", ".*")


# ----------------------------------------------------------------- event log
def test_event_log_replays_from_a_sequence(clock):
    log = EventLog(clock, Registry(), retention=10)
    for index in range(5):
        log.append("trade.executed", n=index)
    replayed = log.replay(since=2)
    assert [record.sequence for record in replayed] == [3, 4, 5]


def test_slow_consumers_are_dropped_not_waited_for(clock):
    """The writer must never be blocked by a consumer that fell behind."""
    log = EventLog(clock, Registry(), retention=100)
    subscription = log.subscribe("slow", ["trade.executed"], maxsize=2)
    for index in range(5):
        log.append("trade.executed", n=index)
    assert subscription.lag == 2
    assert subscription.dropped == 3
    assert log.sequence == 5  # every append still succeeded


def test_topic_filtering(clock):
    log = EventLog(clock, Registry())
    subscription = log.subscribe("trades", ["trade.executed"])
    log.append("order.placed", n=1)
    log.append("trade.executed", n=2)
    assert subscription.lag == 1


# ---------------------------------------------------------------------- pool
async def test_pool_caps_concurrency_and_reuses_resources():
    created = []

    async def factory():
        name = f"conn{len(created)}"
        created.append(name)
        return name

    pool: ResourcePool[str] = ResourcePool(factory, max_size=2)

    async def use():
        async with pool.borrow() as resource:
            await asyncio.sleep(0.01)
            return resource

    results = await asyncio.gather(*[use() for _ in range(6)])
    assert len(created) == 2
    assert set(results) <= set(created)
    assert pool.stats()["in_use"] == 0
    await pool.close()


async def test_pool_acquisition_times_out_without_leaking_a_slot():
    async def factory():
        return "conn"

    pool: ResourcePool[str] = ResourcePool(factory, max_size=1)

    async def hold():
        async with pool.borrow():
            await asyncio.sleep(0.2)

    holder = asyncio.ensure_future(hold())
    await asyncio.sleep(0.01)
    with pytest.raises(ServiceUnavailable):
        await pool.acquire(timeout=0.02)
    await holder
    assert pool.stats() == {"in_use": 0, "idle": 1, "waiters": 0}
    await pool.close()


async def test_failed_use_retires_the_resource():
    async def factory():
        return object()

    pool: ResourcePool[object] = ResourcePool(factory, max_size=2)
    with pytest.raises(RuntimeError):
        async with pool.borrow():
            raise RuntimeError("mid-request failure")
    # The resource's state is unknown, so it must not go back in the pool.
    assert pool.stats()["idle"] == 0
    await pool.close()


# -------------------------------------------------------------------- workers
async def test_work_queue_retries_then_dead_letters(clock):
    attempts = []

    async def handler(job):
        attempts.append(job.attempts)
        raise RuntimeError("always fails")

    queue = WorkQueue(
        "test", handler, registry=Registry(), clock=clock, concurrency=1, max_attempts=3
    )
    await queue.start()
    queue.submit("job", {})
    await queue.drain(timeout=2.0)
    await queue.stop()

    assert attempts == [1, 2, 3]
    assert len(queue.dead_letters()) == 1
    assert "always fails" in queue.dead_letters()[0]["error"]


async def test_work_queue_drops_rather_than_growing_without_bound(clock):
    async def handler(job):
        await asyncio.sleep(10)

    queue = WorkQueue("tiny", handler, registry=Registry(), clock=clock, concurrency=1, maxsize=2)
    accepted = [queue.submit("job", index) for index in range(5)]
    assert accepted.count(True) == 2
    assert accepted.count(False) == 3


async def test_scheduler_skips_overlapping_runs(clock):
    running = asyncio.Event()

    async def slow_job():
        running.set()
        await asyncio.sleep(0.5)

    scheduler = Scheduler(clock, Registry(), tick=0.01)
    job = scheduler.add("slow", 0.01, slow_job)
    await scheduler.start()
    clock.advance(1.0)
    await asyncio.sleep(0.05)
    clock.advance(1.0)
    await asyncio.sleep(0.05)
    await scheduler.stop()

    assert running.is_set()
    assert job.skipped >= 1     # it never started a second copy


async def test_scheduler_survives_a_failing_job(clock):
    calls = []

    def failing():
        calls.append(1)
        raise RuntimeError("boom")

    scheduler = Scheduler(clock, Registry(), tick=0.01)
    job = scheduler.add("bad", 0.01, failing)
    await scheduler.start()
    for _ in range(3):
        clock.advance(1.0)
        await asyncio.sleep(0.03)
    await scheduler.stop()

    assert len(calls) >= 2      # still scheduled after failing
    assert job.failures == len(calls)


# ------------------------------------------------------------ circuit breaker
def test_breaker_opens_after_repeated_failures(clock):
    breaker = CircuitBreaker(clock, Registry(), failure_threshold=3, cooldown=1.0)
    for _ in range(3):
        breaker.before_call()
        breaker.on_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpen):
        breaker.before_call()


def test_breaker_probes_after_the_cooldown_and_closes_on_success(clock):
    breaker = CircuitBreaker(clock, Registry(), failure_threshold=1, cooldown=1.0)
    breaker.before_call()
    breaker.on_failure()

    clock.advance(1.5)
    breaker.before_call()               # allowed through as a probe
    assert breaker.state == "half_open"
    breaker.on_success()
    assert breaker.state == "closed"


def test_a_failing_probe_reopens_the_breaker(clock):
    breaker = CircuitBreaker(clock, Registry(), failure_threshold=1, cooldown=1.0)
    breaker.before_call()
    breaker.on_failure()
    clock.advance(1.5)
    breaker.before_call()
    breaker.on_failure()
    assert breaker.state == "open"


# ------------------------------------------------------------------ telemetry
def test_spans_nest_and_share_a_trace_id(clock):
    tracer = Tracer(IdGenerator(1, clock), Registry())
    with tracer.span("outer") as outer:
        assert current_trace_id() == outer.trace_id
        with tracer.span("inner") as inner:
            assert inner.parent_id == outer.span_id
            assert inner.trace_id == outer.trace_id
    assert current_trace_id() is None       # the context was restored
    assert {span["name"] for span in tracer.recent()} == {"outer", "inner"}


def test_a_span_records_failure_without_swallowing_it(clock):
    tracer = Tracer(IdGenerator(1, clock), Registry())
    with pytest.raises(ValueError), tracer.span("doomed"):
        raise ValueError("boom")
    recorded = tracer.recent()[0]
    assert recorded["status"] == "error"
    assert recorded["attributes"]["error.type"] == "ValueError"


def test_histogram_buckets_and_quantiles():
    registry = Registry()
    histogram = registry.histogram("test_seconds", "test")
    for value in [0.001] * 90 + [1.0] * 10:
        histogram.observe(value)
    assert histogram.count == 100
    assert histogram.quantile(0.5) == 0.001
    assert histogram.quantile(0.99) == 1.0


def test_prometheus_output_is_cumulative():
    registry = Registry()
    histogram = registry.histogram("h_seconds", "help", route="/x")
    for value in (0.0001, 0.01, 1.0):
        histogram.observe(value)
    lines = [line for line in registry.render_prometheus().splitlines() if "_bucket" in line]
    counts = [int(line.rsplit(" ", 1)[1]) for line in lines]
    assert counts == sorted(counts)         # buckets only ever go up
    assert counts[-1] == 3                  # +Inf holds everything
