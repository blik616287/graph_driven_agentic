"""Periodic jobs.

A scheduler is easy to write badly.  The traps, and how each is handled:

* **Drift.**  ``sleep(interval)`` after a job that took 200ms means the job runs
  every ``interval + 200ms``, and the error accumulates.  The next run is
  computed from the schedule, not from "now".
* **Overlap.**  A job that runs longer than its interval must not start again
  while it is still running.  Runs are skipped, and the skip is counted.
* **A crash must not stop the schedule.**  Exceptions are logged and the job
  stays scheduled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..telemetry.logging import get_logger
from ..telemetry.metrics import Registry

_logger = get_logger("workers.scheduler")


@dataclass(slots=True)
class ScheduledJob:
    name: str
    interval: float
    fn: Callable[[], Awaitable[None] | None]
    next_run: float = 0.0
    runs: int = 0
    failures: int = 0
    skipped: int = 0
    running: bool = False


class Scheduler:
    def __init__(self, clock, registry: Registry, tick: float = 0.25) -> None:
        self._clock = clock
        self._tick = tick
        self._jobs: list[ScheduledJob] = []
        self._task: asyncio.Task | None = None
        self._m_runs = registry.counter("scheduled_runs_total", "scheduled job executions")
        self._m_failures = registry.counter("scheduled_failures_total", "scheduled job failures")

    def every(self, interval: float, name: str) -> Callable:
        """Decorator form: ``@scheduler.every(60, "sweep")``."""

        def register(fn):
            self.add(name, interval, fn)
            return fn

        return register

    def add(self, name: str, interval: float, fn) -> ScheduledJob:
        job = ScheduledJob(
            name=name, interval=interval, fn=fn, next_run=self._clock.monotonic() + interval
        )
        self._jobs.append(job)
        return job

    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            now = self._clock.monotonic()
            for job in self._jobs:
                if now < job.next_run:
                    continue
                if job.running:
                    job.skipped += 1
                    # Re-arm from the schedule, not from now, so a slow job does
                    # not permanently shift its own cadence.
                    job.next_run += job.interval
                    continue
                job.next_run += job.interval
                if job.next_run < now:
                    # Fell far behind (a long pause, a suspended process):
                    # resync instead of firing a burst of catch-up runs.
                    job.next_run = now + job.interval
                asyncio.get_running_loop().create_task(self._invoke(job))

    async def _invoke(self, job: ScheduledJob) -> None:
        job.running = True
        try:
            result = job.fn()
            if asyncio.iscoroutine(result):
                await result
            job.runs += 1
            self._m_runs.inc()
        except asyncio.CancelledError:
            raise
        except Exception:
            job.failures += 1
            self._m_failures.inc()
            _logger.exception("scheduled job failed", extra={"job": job.name})
        finally:
            job.running = False

    def stats(self) -> list[dict[str, float]]:
        return [
            {
                "name": job.name,
                "interval": job.interval,
                "runs": job.runs,
                "failures": job.failures,
                "skipped": job.skipped,
            }
            for job in self._jobs
        ]

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
