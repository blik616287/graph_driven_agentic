"""A generic async resource pool.

Used by the client SDK for socket connections, but written against a factory so
it works for anything expensive to create and safe to reuse.

The parts people get wrong:

* **Waiters need a queue, not a loop.**  Polling for a free resource turns
  contention into CPU burn.  A future per waiter wakes exactly one, in order.
* **A returned resource must be checked before reuse.**  A pooled connection
  the peer closed while it sat idle looks fine until you write to it.
* **Acquisition must be cancellable.**  If a caller times out while queued, its
  waiter has to come off the queue, or the pool leaks slots.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from ..errors import ServiceUnavailable
from ..telemetry.metrics import Registry

R = TypeVar("R")


class ResourcePool(Generic[R]):
    def __init__(
        self,
        factory: Callable[[], Awaitable[R]],
        *,
        max_size: int = 10,
        is_alive: Callable[[R], bool] | None = None,
        close: Callable[[R], Awaitable[None]] | None = None,
        registry: Registry | None = None,
        name: str = "pool",
    ) -> None:
        self._factory = factory
        self._max_size = max_size
        self._is_alive = is_alive or (lambda _resource: True)
        self._close = close
        self._idle: deque[R] = deque()
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._in_use = 0
        self._closed = False
        registry = registry or Registry()
        self._m_acquired = registry.counter(
            "pool_acquired_total", "resources handed out", pool=name
        )
        self._m_created = registry.counter("pool_created_total", "resources constructed", pool=name)
        self._m_waited = registry.counter(
            "pool_waits_total", "acquisitions that had to queue", pool=name
        )
        self._g_size = registry.gauge("pool_size", "resources currently checked out", pool=name)

    @property
    def size(self) -> int:
        return self._in_use + len(self._idle)

    async def acquire(self, timeout: float = 5.0) -> R:
        if self._closed:
            raise ServiceUnavailable("connection pool is closed")

        while True:
            while self._idle:
                resource = self._idle.popleft()
                if self._is_alive(resource):
                    self._checkout()
                    return resource
                await self._discard(resource)

            if self.size < self._max_size:
                self._in_use += 1          # reserve the slot before awaiting
                self._g_size.set(self._in_use)
                try:
                    resource = await self._factory()
                except BaseException:
                    self._in_use -= 1      # creation failed: give the slot back
                    self._g_size.set(self._in_use)
                    self._wake_one()
                    raise
                self._m_created.inc()
                self._m_acquired.inc()
                return resource

            # Pool is saturated: queue behind the other waiters.
            self._m_waited.inc()
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Take our waiter off the queue.  Skipping this leaks a slot:
                # a later release() would hand the resource to a future nobody
                # is awaiting any more.
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                elif waiter.done() and not waiter.cancelled():
                    # We were woken and then timed out in the same tick - the
                    # resource is ours and unused, so pass it to the next waiter.
                    self._wake_one()
                raise ServiceUnavailable(
                    f"timed out waiting {timeout}s for a pooled resource"
                ) from None

    def release(self, resource: R, *, reusable: bool = True) -> None:
        """Return a resource.  ``reusable=False`` retires it instead."""
        self._in_use -= 1
        self._g_size.set(self._in_use)
        if self._closed or not reusable or not self._is_alive(resource):
            if self._close is not None:
                asyncio.ensure_future(self._discard_quiet(resource))
        else:
            self._idle.append(resource)
        self._wake_one()

    def _checkout(self) -> None:
        self._in_use += 1
        self._g_size.set(self._in_use)
        self._m_acquired.inc()

    def _wake_one(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    async def _discard(self, resource: R) -> None:
        if self._close is not None:
            await self._close(resource)

    async def _discard_quiet(self, resource: R) -> None:
        try:
            await self._discard(resource)
        except Exception:
            pass

    async def close(self) -> None:
        self._closed = True
        idle, self._idle = self._idle, deque()
        for resource in idle:
            await self._discard_quiet(resource)
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(ServiceUnavailable("pool closed"))
        self._waiters.clear()

    def stats(self) -> dict[str, int]:
        return {"in_use": self._in_use, "idle": len(self._idle), "waiters": len(self._waiters)}


class _PooledResource(Generic[R]):
    """``async with pool.borrow() as resource:`` - release is guaranteed."""

    __slots__ = ("_pool", "_timeout", "_resource", "_reusable")

    def __init__(self, pool: ResourcePool[R], timeout: float) -> None:
        self._pool = pool
        self._timeout = timeout
        self._resource: R | None = None
        self._reusable = True

    async def __aenter__(self) -> R:
        self._resource = await self._pool.acquire(self._timeout)
        return self._resource

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._resource is not None:
            # An exception mid-use means the resource's state is unknown; a
            # half-written HTTP request must never be handed to the next caller.
            self._pool.release(self._resource, reusable=exc_type is None)
        return False


def borrow(pool: ResourcePool[R], timeout: float = 5.0) -> _PooledResource[R]:
    return _PooledResource(pool, timeout)


ResourcePool.borrow = lambda self, timeout=5.0: _PooledResource(self, timeout)  # type: ignore[attr-defined]
