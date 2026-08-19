"""Retry policy with full jitter.

"Full jitter" - a uniform draw from ``[0, computed_delay]`` rather than the
delay itself - is what stops a fleet of clients that failed together from
retrying together.  The deterministic ceiling is still exponential; only the
draw inside it is random.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from .clock import SYSTEM_CLOCK, Clock

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base: float = 0.05
    factor: float = 2.0
    max_delay: float = 2.0
    max_attempts: int = 4
    jitter: bool = True

    def delay_for(self, attempt: int, rng: random.Random) -> float:
        """Delay before attempt ``attempt`` (1-based, so attempt 1 is free)."""
        if attempt <= 1:
            return 0.0
        ceiling = min(self.max_delay, self.base * (self.factor ** (attempt - 2)))
        return rng.uniform(0.0, ceiling) if self.jitter else ceiling


async def retry_async(
    operation: Callable[[int], Awaitable[T]],
    policy: BackoffPolicy,
    *,
    should_retry: Callable[[BaseException], bool],
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    clock: Clock = SYSTEM_CLOCK,
    rng: random.Random | None = None,
) -> T:
    """Call ``operation(attempt)`` until it succeeds or the policy is spent.

    The predicate is injected rather than a tuple of exception types because
    "retryable" here is a property of the *error instance* (a 503 is, a 422 is
    not) and both arrive as the same class.
    """
    rng = rng or random.Random()
    last: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation(attempt)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if not should_retry(exc) or attempt == policy.max_attempts:
                raise
            last = exc
            delay = policy.delay_for(attempt + 1, rng)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await clock.sleep(delay)
    raise AssertionError("unreachable") from last  # pragma: no cover
