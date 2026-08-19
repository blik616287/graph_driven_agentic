"""Time, behind an interface.

Every component takes a ``Clock`` instead of calling :mod:`time` directly.  The
payoff is in the tests: :class:`ManualClock` makes rate limiters, backoff and
candle bucketing deterministic without a single ``sleep``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """Wall time for stamping, monotonic time for measuring."""

    def now(self) -> float:
        """Unix timestamp in seconds.  Use for anything persisted."""

    def monotonic(self) -> float:
        """Monotonic seconds.  Use for durations, timeouts and rate limits."""

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The production clock.  Stateless, so a module-level instance is fine."""

    __slots__ = ()

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class ManualClock:
    """A clock that only moves when you tell it to."""

    __slots__ = ("_now", "_mono")

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)  # yield so other tasks observe the jump


SYSTEM_CLOCK = SystemClock()
