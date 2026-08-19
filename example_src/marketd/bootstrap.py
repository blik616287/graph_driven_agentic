"""Composition root.

Every dependency is constructed here, once, and passed explicitly.  No module
reaches for a global, no component constructs its own collaborators, and nothing
imports a singleton.  The payoff is that a test can build the whole service with
a :class:`~marketd.util.clock.ManualClock` and a fresh registry in one call, and
that the wiring is readable top to bottom.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import Any

from . import __version__
from .api.app import create_app
from .api.auth import ApiKeyStore
from .api.pagination import CursorCodec
from .api.router import Router
from .config import Settings
from .core.events import EventBus
from .core.ledger import Ledger
from .core.matching import MatchingEngine
from .core.models import Instrument, Order, Trade, default_instruments
from .core.risk import RiskEngine
from .http.server import HTTPServer
from .services.accounts import AccountService
from .services.market_data import MarketDataService
from .services.orders import OrderService
from .storage.eventlog import EventLog
from .storage.memstore import MemoryRepository
from .telemetry.logging import configure_logging, get_logger
from .telemetry.metrics import Registry
from .telemetry.tracing import Tracer
from .util.clock import SYSTEM_CLOCK, Clock
from .util.idgen import IdGenerator
from .workers.post_trade import PostTradeWorker
from .workers.queue import WorkQueue
from .workers.scheduler import Scheduler

_logger = get_logger("bootstrap")


@dataclass(slots=True)
class Container:
    """Everything the service is made of, in one object."""

    settings: Settings
    clock: Clock
    registry: Registry
    tracer: Tracer
    ids: IdGenerator
    instruments: dict[str, Instrument]
    event_log: EventLog
    bus: EventBus
    ledger: Ledger
    engine: MatchingEngine
    risk: RiskEngine
    accounts_repo: MemoryRepository
    orders_repo: MemoryRepository
    trades_repo: MemoryRepository
    api_keys: ApiKeyStore
    cursor_codec: CursorCodec
    router: Router
    account_service: AccountService
    order_service: OrderService
    market_data_service: MarketDataService
    work_queue: WorkQueue
    post_trade: PostTradeWorker
    scheduler: Scheduler
    version: str = __version__

    async def start_workers(self) -> None:
        await self.work_queue.start()
        await self.post_trade.start()
        await self.scheduler.start()

    async def stop_workers(self) -> None:
        await self.scheduler.stop()
        await self.work_queue.stop()
        await self.post_trade.stop()
        await self.bus.drain()

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "accounts": self.accounts_repo.count(),
            "orders": self.orders_repo.count(),
            "trades": self.trades_repo.count(),
            "books": self.engine.stats(),
            "ledger_balanced": not self.ledger.verify(),
        }


def build_container(settings: Settings, clock: Clock = SYSTEM_CLOCK) -> Container:
    """Construct the service graph.  Pure - no I/O, no tasks started."""
    registry = Registry()
    ids = IdGenerator(settings.node_id, clock)
    tracer = Tracer(ids, registry, buffer_size=settings.trace_buffer)
    instruments = default_instruments()

    event_log = EventLog(clock, registry)
    bus = EventBus(clock, registry)
    ledger = Ledger(ids, clock, registry)
    engine = MatchingEngine(instruments, registry)
    risk = RiskEngine(ledger, registry, taker_fee_bps=settings.taker_fee_bps)

    # Indexes are declared up front: every lookup the service performs has one.
    accounts_repo = MemoryRepository("account", lambda account: account.id)
    orders_repo: MemoryRepository[Order] = MemoryRepository(
        "order",
        lambda order: order.id,
        {
            "account": lambda order: order.account_id,
            "symbol": lambda order: order.symbol,
            # Composite key: a client order id is only unique within an account.
            "client_order_id": lambda order: (
                (order.account_id, order.client_order_id) if order.client_order_id else None
            ),
        },
    )
    trades_repo: MemoryRepository[Trade] = MemoryRepository(
        "trade", lambda trade: trade.id, {"symbol": lambda trade: trade.symbol}
    )

    api_keys = ApiKeyStore(
        settings.api_secret,
        ids,
        clock,
        registry,
        cache_size=settings.auth_cache_size,
        cache_ttl=settings.auth_cache_ttl,
    )
    cursor_codec = CursorCodec(settings.api_secret)
    router = Router(cache_size=settings.route_cache_size)

    account_service = AccountService(
        accounts=accounts_repo,
        ledger=ledger,
        keys=api_keys,
        ids=ids,
        clock=clock,
        bus=bus,
        event_log=event_log,
        registry=registry,
    )
    market_data_service = MarketDataService(
        instruments=instruments,
        engine=engine,
        clock=clock,
        registry=registry,
        snapshot_ttl=settings.book_snapshot_ttl,
    )
    order_service = OrderService(
        instruments=instruments,
        engine=engine,
        ledger=ledger,
        risk=risk,
        orders=orders_repo,
        trades=trades_repo,
        ids=ids,
        clock=clock,
        bus=bus,
        event_log=event_log,
        tracer=tracer,
        registry=registry,
        taker_fee_bps=settings.taker_fee_bps,
        maker_fee_bps=settings.maker_fee_bps,
    )

    post_trade = PostTradeWorker(
        event_log=event_log,
        market_data=market_data_service,
        trades=trades_repo,
        registry=registry,
    )

    async def handle_job(job) -> None:
        """The generic queue's only job type today: statement generation."""
        if job.name == "account.statement":
            account_id = job.payload["account_id"]
            balances = ledger.balances_for(account_id)
            event_log.append(
                "statement.generated", account_id=account_id, assets=len(balances)
            )

    work_queue = WorkQueue(
        "background",
        handle_job,
        registry=registry,
        clock=clock,
        concurrency=settings.worker_concurrency,
        maxsize=settings.worker_queue_size,
        max_attempts=settings.worker_max_attempts,
    )

    scheduler = Scheduler(clock, registry)

    container = Container(
        settings=settings,
        clock=clock,
        registry=registry,
        tracer=tracer,
        ids=ids,
        instruments=instruments,
        event_log=event_log,
        bus=bus,
        ledger=ledger,
        engine=engine,
        risk=risk,
        accounts_repo=accounts_repo,
        orders_repo=orders_repo,
        trades_repo=trades_repo,
        api_keys=api_keys,
        cursor_codec=cursor_codec,
        router=router,
        account_service=account_service,
        order_service=order_service,
        market_data_service=market_data_service,
        work_queue=work_queue,
        post_trade=post_trade,
        scheduler=scheduler,
    )

    _register_periodic_jobs(container)
    return container


def _register_periodic_jobs(container: Container) -> None:
    """Recurring housekeeping.

    The ledger audit is the important one: a venue whose books stop balancing
    should find out in seconds, from its own alarm, rather than from a customer.
    """
    ledger, registry, log = container.ledger, container.registry, container.event_log
    drift_gauge = registry.gauge("ledger_drift_assets", "assets whose balances do not net to zero")

    def audit_ledger() -> None:
        drift = ledger.verify()
        drift_gauge.set(len(drift))
        if drift:
            _logger.error(
                "ledger does not balance",
                extra={"drift": {asset: str(units) for asset, units in drift.items()}},
            )
            log.append("ledger.drift", assets=list(drift))

    def sweep_caches() -> None:
        log.append("cache.stats", routes=container.router.cache_stats()["hit_ratio"])

    container.scheduler.add("ledger_audit", 5.0, audit_ledger)
    container.scheduler.add("cache_sweep", 30.0, sweep_caches)


async def serve(settings: Settings | None = None, clock: Clock = SYSTEM_CLOCK) -> None:
    """Run until SIGINT/SIGTERM, then shut down in dependency order."""
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    container = build_container(settings, clock)
    app = create_app(container)
    server = HTTPServer(app, settings, container.registry, clock)

    await container.start_workers()
    await server.start()
    _logger.info("marketd ready", extra={"port": server.port, "version": container.version})

    stop = asyncio.get_running_loop().create_future()

    def request_stop(signum: int) -> None:
        if not stop.done():
            _logger.info("shutdown requested", extra={"signal": signum})
            stop.set_result(None)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    try:
        await stop
    finally:
        # Stop accepting first, then let the workers finish what the last
        # requests queued.  The reverse order drops work that was already
        # acknowledged.
        await server.close()
        await container.stop_workers()
        _logger.info("shutdown complete", extra=container.snapshot())
