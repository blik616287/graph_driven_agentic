"""Structured logging.

One line of JSON per event, with the active trace id injected by a filter so
call sites never have to pass it.  A filter is the right hook for this: it runs
inside the logging machinery, after the level check, so suppressed records cost
nothing.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from .tracing import current_span

_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class TraceFilter(logging.Filter):
    """Copy the active span's ids onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        span = current_span()
        record.trace_id = span.trace_id if span is not None else None  # type: ignore[attr-defined]
        record.span_id = span.span_id if span is not None else None  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON object.

    Any keyword passed via ``extra=`` is promoted to a top-level field, which is
    what makes the logs queryable without regexes.
    """

    def format(self, record: logging.LogRecord) -> str:
        from ..util import jsonx

        payload: dict[str, Any] = {
            "ts": round(record.created, 6),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = getattr(record, "trace_id", None)
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = getattr(record, "span_id", None)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in ("trace_id", "span_id"):
                payload[key] = value
        return jsonx.dumps_str(payload)


def configure_logging(level: str = "INFO", stream=None) -> None:
    """Install the JSON handler on the root logger.  Idempotent."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceFilter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"marketd.{name}")
