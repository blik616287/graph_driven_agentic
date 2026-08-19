"""A bounded work queue with retries and a dead-letter list.

Three properties this gets right that a bare ``asyncio.Queue`` does not:

**Bounded, with an explicit overflow policy.**  An unbounded queue turns a
traffic spike into an out-of-memory kill.  Here, submitting to a full queue
fails immediately and the caller decides - which for a background job means
dropping it and counting it.

**Retries with backoff, then a dead letter.**  A job that fails three times is
not going to succeed on the fourth in the next millisecond.  Failed jobs land
in a bounded dead-letter list where they can be inspected.

**Shutdown that finishes the work.**  ``drain`` waits for the queue to empty and
for in-flight jobs to finish before the consumers are cancelled.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..telemetry.logging import get_logger
from ..telemetry.metrics import Registry
from ..util.backoff import BackoffPolicy

_logger = get_logger("workers.queue")


@dataclass(slots=True)
class Job:
    name: str
    payload: Any
    attempts: int = 0
    last_error: str = ""
    enqueued_at: float = 0.0


JobHandler = Callable[[Job], Awaitable[None]]


class WorkQueue:
    def __init__(
        self,
        name: str,
        handler: JobHandler,
        *,
        registry: Registry,
        clock,
        concurrency: int = 4,
        maxsize: int = 4096,
        max_attempts: int = 3,
        policy: BackoffPolicy | None = None,
    ) -> None:
        self.name = name
        self._handler = handler
        self._clock = clock
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._policy = policy or BackoffPolicy(base=0.05, max_delay=1.0, max_attempts=max_attempts)
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=maxsize)
        self._consumers: list[asyncio.Task] = []
        self._dead_letters: deque[Job] = deque(maxlen=100)
        self._rng = random.Random(0xC0FFEE)
        self._running = False
        self._m_submitted = registry.counter("jobs_submitted_total", "jobs enqueued", queue=name)
        self._m_done = registry.counter("jobs_completed_total", "jobs completed", queue=name)
        self._m_failed = registry.counter("jobs_failed_total", "jobs dead-lettered", queue=name)
        self._m_dropped = registry.counter(
            "jobs_dropped_total", "jobs dropped, queue full", queue=name
        )
        self._m_retried = registry.counter("jobs_retried_total", "job attempts retried", queue=name)
        self._g_depth = registry.gauge("job_queue_depth", "jobs waiting", queue=name)
        self._h_latency = registry.histogram("job_seconds", "job handler duration", queue=name)

    def submit(self, name: str, payload: Any) -> bool:
        """Enqueue without blocking.  ``False`` means the queue was full."""
        job = Job(name=name, payload=payload, enqueued_at=self._clock.monotonic())
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self._m_dropped.inc()
            _logger.warning(
                "work queue full, dropping job", extra={"queue": self.name, "job": name}
            )
            return False
        self._m_submitted.inc()
        self._g_depth.set(self._queue.qsize())
        return True

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._consumers = [
            asyncio.get_running_loop().create_task(self._consume(index))
            for index in range(self._concurrency)
        ]

    async def _consume(self, index: int) -> None:
        while True:
            job = await self._queue.get()
            self._g_depth.set(self._queue.qsize())
            try:
                await self._run(job)
            finally:
                self._queue.task_done()

    async def _run(self, job: Job) -> None:
        from time import perf_counter

        while True:
            job.attempts += 1
            started = perf_counter()
            try:
                await self._handler(job)
                self._h_latency.observe(perf_counter() - started)
                self._m_done.inc()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._h_latency.observe(perf_counter() - started)
                job.last_error = f"{type(exc).__name__}: {exc}"
                if job.attempts >= self._max_attempts:
                    self._m_failed.inc()
                    self._dead_letters.append(job)
                    _logger.error(
                        "job dead-lettered",
                        extra={"queue": self.name, "job": job.name, "error": job.last_error},
                    )
                    return
                self._m_retried.inc()
                delay = self._policy.delay_for(job.attempts + 1, self._rng)
                _logger.warning(
                    "job failed, retrying",
                    extra={
                        "queue": self.name, "job": job.name,
                        "attempt": job.attempts, "delay": round(delay, 3),
                    },
                )
                await asyncio.sleep(delay)

    async def drain(self, timeout: float = 5.0) -> bool:
        """Wait for the queue to empty.  ``False`` if it did not in time."""
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self, timeout: float = 5.0) -> None:
        await self.drain(timeout)
        for task in self._consumers:
            task.cancel()
        if self._consumers:
            await asyncio.gather(*self._consumers, return_exceptions=True)
        self._consumers.clear()
        self._running = False

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def dead_letters(self) -> list[dict[str, Any]]:
        return [
            {"name": job.name, "attempts": job.attempts, "error": job.last_error}
            for job in self._dead_letters
        ]
