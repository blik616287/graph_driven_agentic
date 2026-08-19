"""Post-trade processing.

Tails the event log and does the work that must happen after a trade but must
not happen *during* one: updating the tape and candles, and accruing per-account
volume for fee tiering.

Reading from the log rather than subscribing to the bus is deliberate.  The log
has sequence numbers, so a consumer that falls behind or restarts knows exactly
where it was.  A pub/sub subscriber that misses a message has no way to find
out.
"""

from __future__ import annotations

import asyncio

from ..core.models import Trade
from ..storage.eventlog import EventLog, LogRecord
from ..telemetry.logging import get_logger
from ..telemetry.metrics import Registry

_logger = get_logger("workers.post_trade")

TOPICS = ["trade.executed", "order.placed", "order.cancelled"]


class PostTradeWorker:
    """One task, one cursor, at-least-once delivery."""

    __slots__ = ("_log", "_market_data", "_trades", "_registry", "_task",
                 "_subscription", "_volume", "_m_processed", "_processed", "_cursor")

    def __init__(self, *, event_log: EventLog, market_data, trades, registry: Registry) -> None:
        self._log = event_log
        self._market_data = market_data
        self._trades = trades
        self._registry = registry
        self._task: asyncio.Task | None = None
        self._subscription = None
        self._volume: dict[str, int] = {}
        self._processed = 0
        self._cursor = 0
        self._m_processed = registry.counter("post_trade_records_total", "log records processed")

    async def start(self) -> None:
        self._subscription = self._log.subscribe("post_trade", TOPICS, maxsize=8192)
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        assert self._subscription is not None
        while True:
            record = await self._subscription.get()
            try:
                self._handle(record)
            except Exception:
                # A malformed record must not kill the consumer: log it, count
                # it, keep the cursor moving.
                _logger.exception("post-trade handler failed", extra={"seq": record.sequence})
            finally:
                self._cursor = record.sequence
                self._processed += 1
                self._m_processed.inc()

    def _handle(self, record: LogRecord) -> None:
        if record.topic != "trade.executed":
            return
        trade = self._trades.get(record.payload["trade_id"])
        if trade is None:
            return
        self._market_data.record_trade(trade)
        self._accrue_volume(trade)

    def _accrue_volume(self, trade: Trade) -> None:
        """Rolling traded quantity per account, the input to fee tiering."""
        for account_id in (trade.buyer_account_id, trade.seller_account_id):
            self._volume[account_id] = self._volume.get(account_id, 0) + trade.qty_units

    def volume_for(self, account_id: str) -> int:
        return self._volume.get(account_id, 0)

    async def idle(self, timeout: float = 2.0) -> bool:
        """Wait until the consumer has caught up with the log.

        Tests and graceful shutdown both need this; polling is fine because it
        only ever runs at a boundary, never on the request path.
        """
        deadline = timeout
        while deadline > 0:
            if self._subscription is not None and self._subscription.lag == 0:
                await asyncio.sleep(0)
                if self._subscription.lag == 0:
                    return True
            await asyncio.sleep(0.005)
            deadline -= 0.005
        return False

    def stats(self) -> dict[str, int]:
        return {
            "processed": self._processed,
            "cursor": self._cursor,
            "lag": self._subscription.lag if self._subscription else 0,
            "dropped": self._subscription.dropped if self._subscription else 0,
            "accounts_tracked": len(self._volume),
        }

    async def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
