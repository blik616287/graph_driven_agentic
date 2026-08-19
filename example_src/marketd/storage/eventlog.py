"""An append-only log with fan-out to consumers.

This is the seam between the synchronous request path and everything that
happens afterwards.  Writers append and move on; consumers each hold their own
cursor and read at their own speed.

The retention window is a ring buffer, and slow consumers are *dropped*, not
waited for.  Blocking the writer to protect a consumer is how one slow
analytics job takes down order entry.  A dropped consumer learns about it
(``lag`` goes positive, ``dropped`` counts up) and can resubscribe from the
oldest retained sequence.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..telemetry.metrics import Registry


@dataclass(frozen=True, slots=True)
class LogRecord:
    sequence: int
    ts: float
    topic: str
    payload: dict[str, Any]


class Subscription:
    """One consumer's view of the log."""

    __slots__ = ("name", "topics", "queue", "dropped", "_log")

    def __init__(self, log: EventLog, name: str, topics: frozenset[str] | None, maxsize: int):
        self.name = name
        self.topics = topics
        self.queue: asyncio.Queue[LogRecord] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._log = log

    def wants(self, topic: str) -> bool:
        return self.topics is None or topic in self.topics

    async def get(self) -> LogRecord:
        return await self.queue.get()

    def close(self) -> None:
        self._log.unsubscribe(self)

    @property
    def lag(self) -> int:
        return self.queue.qsize()


class EventLog:
    """Ordered, bounded, replayable."""

    __slots__ = ("_records", "_sequence", "_subscriptions", "_clock", "_m_appended", "_m_dropped")

    def __init__(self, clock, registry: Registry, retention: int = 10_000) -> None:
        self._records: deque[LogRecord] = deque(maxlen=retention)
        self._sequence = 0
        self._subscriptions: list[Subscription] = []
        self._clock = clock
        self._m_appended = registry.counter("log_records_total", "records appended")
        self._m_dropped = registry.counter(
            "log_dropped_total", "records dropped for slow consumers"
        )

    # --- HOT PATH ---------------------------------------------------------
    def append(self, topic: str, **payload: Any) -> LogRecord:
        """Append and fan out.  Never blocks, never raises on a full consumer."""
        self._sequence += 1
        record = LogRecord(
            sequence=self._sequence, ts=self._clock.now(), topic=topic, payload=payload
        )
        self._records.append(record)
        self._m_appended.inc()
        for subscription in self._subscriptions:
            if not subscription.wants(topic):
                continue
            try:
                subscription.queue.put_nowait(record)
            except asyncio.QueueFull:
                subscription.dropped += 1
                self._m_dropped.inc()
        return record

    def subscribe(
        self, name: str, topics: Iterator[str] | list[str] | None = None, maxsize: int = 1024
    ) -> Subscription:
        subscription = Subscription(
            self, name, frozenset(topics) if topics is not None else None, maxsize
        )
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def replay(self, since: int = 0, topic: str | None = None, limit: int = 100) -> list[LogRecord]:
        """Records after ``since``.  The window is bounded by retention."""
        out: list[LogRecord] = []
        for record in self._records:
            if record.sequence <= since:
                continue
            if topic is not None and record.topic != topic:
                continue
            out.append(record)
            if len(out) >= limit:
                break
        return out

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def oldest_sequence(self) -> int:
        return self._records[0].sequence if self._records else 0

    def stats(self) -> dict[str, Any]:
        return {
            "sequence": self._sequence,
            "retained": len(self._records),
            "oldest_sequence": self.oldest_sequence,
            "subscribers": [
                {"name": s.name, "lag": s.lag, "dropped": s.dropped} for s in self._subscriptions
            ],
        }
