"""A bounded LRU cache and a decorator built on it.

``functools.lru_cache`` would do for the decorator, but not for the caches that
need eviction stats, manual invalidation or a TTL - route lookups, verified API
tokens and order book snapshots all need at least one of those.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

_MISS = object()


class LRUCache(Generic[K, V]):
    """Least-recently-used cache with optional per-entry TTL.

    Backed by an ``OrderedDict``: ``move_to_end`` and ``popitem(last=False)``
    are both O(1), which is what keeps :meth:`get` cheap enough to sit in front
    of request routing.
    """

    __slots__ = ("maxsize", "ttl", "_data", "_expiry", "hits", "misses", "evictions")

    def __init__(self, maxsize: int = 1024, ttl: float | None = None) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: OrderedDict[K, V] = OrderedDict()
        self._expiry: dict[K, float] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    # --- HOT PATH ---------------------------------------------------------
    def get(self, key: K, now: float = 0.0) -> V | None:
        data = self._data
        value = data.get(key, _MISS)
        if value is _MISS:
            self.misses += 1
            return None
        if self.ttl is not None and self._expiry[key] <= now:
            del data[key]
            del self._expiry[key]
            self.misses += 1
            return None
        data.move_to_end(key)
        self.hits += 1
        return value  # type: ignore[return-value]

    def put(self, key: K, value: V, now: float = 0.0) -> None:
        data = self._data
        if key in data:
            data.move_to_end(key)
        data[key] = value
        if self.ttl is not None:
            self._expiry[key] = now + self.ttl
        while len(data) > self.maxsize:
            evicted, _ = data.popitem(last=False)
            self._expiry.pop(evicted, None)
            self.evictions += 1

    def invalidate(self, key: K) -> bool:
        if self._data.pop(key, _MISS) is _MISS:
            return False
        self._expiry.pop(key, None)
        return True

    def clear(self) -> None:
        self._data.clear()
        self._expiry.clear()

    def stats(self) -> dict[str, float]:
        total = self.hits + self.misses
        return {
            "size": len(self._data),
            "maxsize": self.maxsize,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_ratio": (self.hits / total) if total else 0.0,
        }

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data


def memoize(maxsize: int = 256) -> Callable[[Callable[..., V]], Callable[..., V]]:
    """Cache a pure function on its positional arguments.

    Deliberately positional-only: building a key out of ``**kwargs`` costs more
    than most of the functions worth memoising here.
    """

    def decorate(fn: Callable[..., V]) -> Callable[..., V]:
        # Values are boxed in a 1-tuple so that a legitimately cached ``None``
        # is not mistaken for a miss.
        cache: LRUCache[tuple, tuple] = LRUCache(maxsize=maxsize)

        def wrapper(*args):  # type: ignore[no-untyped-def]
            box = cache.get(args)
            if box is not None:
                return box[0]
            value = fn(*args)
            cache.put(args, (value,))
            return value

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.cache = cache  # type: ignore[attr-defined]
        return wrapper

    return decorate
