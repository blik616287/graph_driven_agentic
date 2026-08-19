"""Token bucket rate limiting.

The bucket holds no timer and schedules nothing.  Tokens are refilled lazily on
read, from the elapsed monotonic time, which makes the cost of a limit check a
subtraction and a compare - cheap enough to run on every inbound request and
again on every order.
"""

from __future__ import annotations

from .lru import LRUCache


class TokenBucket:
    """Allow ``rate`` operations per second, bursting up to ``capacity``."""

    __slots__ = ("capacity", "rate", "_tokens", "_updated")

    def __init__(self, capacity: float, rate: float, now: float = 0.0) -> None:
        if capacity <= 0 or rate <= 0:
            raise ValueError("capacity and rate must be positive")
        self.capacity = float(capacity)
        self.rate = float(rate)
        self._tokens = float(capacity)
        self._updated = now

    # --- HOT PATH ---------------------------------------------------------
    def consume(self, now: float, amount: float = 1.0) -> bool:
        """Take ``amount`` tokens if available.  Never blocks."""
        elapsed = now - self._updated
        if elapsed > 0.0:
            self._updated = now
            tokens = self._tokens + elapsed * self.rate
            self._tokens = tokens if tokens < self.capacity else self.capacity
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False

    def retry_after(self, now: float, amount: float = 1.0) -> float:
        """Seconds until ``amount`` tokens would be available."""
        deficit = amount - self._tokens
        if deficit <= 0.0:
            return 0.0
        return deficit / self.rate

    @property
    def tokens(self) -> float:
        return self._tokens


class BucketRegistry:
    """One bucket per key, with LRU eviction so idle keys cannot leak memory.

    Evicting a bucket resets its state, which is a deliberate trade: a caller
    quiet for long enough to fall out of the cache has, by definition, been
    refilling to capacity anyway.
    """

    __slots__ = ("capacity", "rate", "_buckets")

    def __init__(self, capacity: float, rate: float, maxkeys: int = 8192) -> None:
        self.capacity = capacity
        self.rate = rate
        self._buckets: LRUCache[str, TokenBucket] = LRUCache(maxsize=maxkeys)

    def bucket(self, key: str, now: float) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self.capacity, self.rate, now)
            self._buckets.put(key, bucket)
        return bucket

    # --- HOT PATH ---------------------------------------------------------
    def check(self, key: str, now: float, amount: float = 1.0) -> tuple[bool, float, float]:
        """Return ``(allowed, remaining_tokens, retry_after_seconds)``."""
        bucket = self.bucket(key, now)
        allowed = bucket.consume(now, amount)
        return allowed, bucket.tokens, 0.0 if allowed else bucket.retry_after(now, amount)

    def stats(self) -> dict[str, float]:
        return self._buckets.stats()
