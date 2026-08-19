"""In-process tracing.

A span is a name, a duration and a bag of attributes, linked to a parent by id.
Propagation uses :mod:`contextvars`, which is what makes it work across
``await`` boundaries without threading a context object through every call.

Finished spans land in a bounded ring buffer.  That buffer is not a tracing
backend - it is a debugging aid you can dump from the admin endpoint after a
slow request, without standing up a collector.
"""

from __future__ import annotations

import contextvars
from collections import deque
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .metrics import Registry

_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "marketd_current_span", default=None
)


@dataclass(slots=True)
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    started: float
    duration: float = 0.0
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)

    def set(self, **attributes: Any) -> Span:
        """Attach attributes.  Returns self so calls chain inside a ``with``."""
        self.attributes.update(attributes)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "duration_ms": round(self.duration * 1000, 3),
            "status": self.status,
            "attributes": self.attributes,
        }


class _SpanContext:
    """The object returned by ``tracer.span(...)``.

    Written as an explicit context manager rather than
    ``@contextlib.contextmanager`` because generator-based managers allocate a
    generator frame per use, and spans are opened several times per request.
    """

    __slots__ = ("_tracer", "_span", "_token")

    def __init__(self, tracer: Tracer, span: Span) -> None:
        self._tracer = tracer
        self._span = span
        self._token: contextvars.Token | None = None

    def __enter__(self) -> Span:
        self._token = _current_span.set(self._span)
        return self._span

    def __exit__(self, exc_type, exc, tb) -> bool:
        span = self._span
        span.duration = perf_counter() - span.started
        if exc_type is not None:
            span.status = "error"
            span.attributes.setdefault("error.type", exc_type.__name__)
        if self._token is not None:
            _current_span.reset(self._token)
        self._tracer.finish(span)
        return False  # never swallow


class Tracer:
    """Creates spans and keeps the most recent ``buffer_size`` of them."""

    __slots__ = ("_ids", "_registry", "_finished", "_histogram_cache", "sample_rate")

    def __init__(self, ids, registry: Registry, buffer_size: int = 256) -> None:
        self._ids = ids
        self._registry = registry
        self._finished: deque[Span] = deque(maxlen=buffer_size)
        self._histogram_cache: dict[str, Any] = {}

    def span(self, name: str, **attributes: Any) -> _SpanContext:
        parent = _current_span.get()
        span_id = self._ids.next_token()
        span = Span(
            trace_id=parent.trace_id if parent is not None else span_id,
            span_id=span_id,
            parent_id=parent.span_id if parent is not None else None,
            name=name,
            started=perf_counter(),
            attributes=attributes,
        )
        return _SpanContext(self, span)

    # --- HOT PATH ---------------------------------------------------------
    def finish(self, span: Span) -> None:
        """Record a completed span.

        The per-name histogram is memoised in a dict: resolving it through the
        registry would re-sort the label tuple on every span.
        """
        histogram = self._histogram_cache.get(span.name)
        if histogram is None:
            histogram = self._registry.histogram(
                "span_duration_seconds", "span wall time", span=span.name
            )
            self._histogram_cache[span.name] = histogram
        histogram.observe(span.duration)
        self._finished.append(span)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        spans = list(self._finished)[-limit:]
        return [s.to_dict() for s in reversed(spans)]


def current_span() -> Span | None:
    return _current_span.get()


def current_trace_id() -> str | None:
    span = _current_span.get()
    return span.trace_id if span is not None else None
