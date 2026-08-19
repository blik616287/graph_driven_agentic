"""An in-process publish/subscribe bus.

The bus is how the request path stays short.  Placing an order does the work
that must be transactional - risk, match, settle - and publishes what happened.
Candles, statements and notifications are all built by subscribers, off the
critical path.

Two delivery modes, and the difference matters:

``publish``       awaits every subscriber.  Use when the caller must not
                  proceed until the effects have landed.
``publish_nowait``  schedules delivery on the event loop and returns.  Use on
                  the request path.  Failures are counted, never raised into
                  the publisher - a broken analytics subscriber must not fail
                  someone's order.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..telemetry.metrics import Registry

WILDCARD = "*"


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    ts: float
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ts": self.ts, "seq": self.sequence, **self.payload}


Handler = Callable[[Event], Any]


class EventBus:
    __slots__ = ("_subscribers", "_is_async", "_clock", "_sequence", "_logger",
                 "_m_published", "_m_failed", "_tasks")

    def __init__(self, clock, registry: Registry) -> None:
        self._subscribers: dict[str, list[Handler]] = {}
        # Whether a handler is a coroutine function is fixed at subscribe time;
        # caching it avoids an ``inspect`` call per delivery.
        self._is_async: dict[int, bool] = {}
        self._clock = clock
        self._sequence = 0
        self._tasks: set[asyncio.Task] = set()
        self._m_published = registry.counter("events_published_total", "events published")
        self._m_failed = registry.counter("event_handler_errors_total", "subscriber failures")
        from ..telemetry.logging import get_logger

        self._logger = get_logger("events")

    def subscribe(self, name: str, handler: Handler) -> Callable[[], None]:
        """Register ``handler`` for ``name`` (or ``"*"``).  Returns an unsubscribe."""
        self._subscribers.setdefault(name, []).append(handler)
        self._is_async[id(handler)] = inspect.iscoroutinefunction(handler)

        def unsubscribe() -> None:
            handlers = self._subscribers.get(name)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    def emit(self, name: str, **payload: Any) -> Event:
        self._sequence += 1
        return Event(name=name, ts=self._clock.now(), payload=payload, sequence=self._sequence)

    async def publish(self, event: Event) -> None:
        """Deliver to every matching subscriber, isolating failures."""
        self._m_published.inc()
        for handler in self._handlers_for(event.name):
            try:
                if self._is_async[id(handler)]:
                    await handler(event)
                else:
                    handler(event)
            except Exception:
                self._m_failed.inc()
                self._logger.exception("event handler failed", extra={"event": event.name})

    # --- HOT PATH ---------------------------------------------------------
    def publish_nowait(self, event: Event) -> None:
        """Fire and forget.  Safe to call with no running loop."""
        self._m_published.inc()
        sync_handlers = []
        async_handlers = []
        for handler in self._handlers_for(event.name):
            (async_handlers if self._is_async[id(handler)] else sync_handlers).append(handler)

        for handler in sync_handlers:
            try:
                handler(event)
            except Exception:
                self._m_failed.inc()
                self._logger.exception("event handler failed", extra={"event": event.name})

        if not async_handlers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (a unit test): sync handlers already ran
        for handler in async_handlers:
            # Keep a strong reference: the event loop only holds a weak one, and
            # a task that gets collected mid-flight vanishes silently.
            task = loop.create_task(self._deliver(handler, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _deliver(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            self._m_failed.inc()
            self._logger.exception("event handler failed", extra={"event": event.name})

    def _handlers_for(self, name: str) -> list[Handler]:
        exact = self._subscribers.get(name)
        wildcard = self._subscribers.get(WILDCARD)
        if not wildcard:
            return exact or []
        if not exact:
            return wildcard
        return exact + wildcard

    async def drain(self) -> None:
        """Await every in-flight delivery.  Used on shutdown and in tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def subscriber_count(self) -> int:
        return sum(len(handlers) for handlers in self._subscribers.values())
