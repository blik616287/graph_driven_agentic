"""Metrics, tracing and structured logging.

All three are pull-based and in-process: there is no agent to run and no
network egress.  ``GET /metrics`` renders the registry in Prometheus text
format, which is enough to drive a dashboard or a load-test assertion.
"""

from .metrics import Counter, Gauge, Histogram, Registry
from .tracing import Span, Tracer, current_trace_id

__all__ = ["Counter", "Gauge", "Histogram", "Registry", "Span", "Tracer", "current_trace_id"]
