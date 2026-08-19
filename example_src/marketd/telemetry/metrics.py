"""A minimal Prometheus-shaped metrics registry.

Design notes worth stealing:

* A metric handle is resolved **once**, at wiring time, and stored on the
  object that uses it.  Looking a metric up by name and label values on every
  observation is the single most common way metrics libraries end up costing
  more than the code they measure.
* Histograms use fixed bucket bounds and :func:`bisect.bisect_left`, so an
  observation is one binary search over a short list plus two increments.
* Everything is plain Python ints/floats on a single thread.  No locks.
"""

from __future__ import annotations

from bisect import bisect_left
from typing import Any

# Latency buckets in seconds: dense where request latencies actually live,
# sparse in the tail.  Bucket choice is a product decision, not a default.
DEFAULT_LATENCY_BUCKETS: tuple[float, ...] = (
    0.000_05, 0.000_1, 0.000_25, 0.000_5, 0.001, 0.0025, 0.005,
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
)

_LabelKey = tuple[tuple[str, str], ...]


class Counter:
    """Monotonically increasing value."""

    __slots__ = ("name", "labels", "value")

    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.labels = labels
        self.value = 0.0

    # --- HOT PATH ---------------------------------------------------------
    def inc(self, amount: float = 1.0) -> None:
        self.value += amount


class Gauge:
    """Value that can go up or down (queue depth, open orders, connections)."""

    __slots__ = ("name", "labels", "value")

    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.labels = labels
        self.value = 0.0

    def set(self, value: float) -> None:
        self.value = value

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount


class Histogram:
    """Cumulative histogram with pre-declared bounds."""

    __slots__ = ("name", "labels", "bounds", "counts", "sum", "count")

    def __init__(
        self,
        name: str,
        labels: dict[str, str],
        bounds: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS,
    ) -> None:
        self.name = name
        self.labels = labels
        self.bounds = bounds
        self.counts = [0] * (len(bounds) + 1)  # last slot is +Inf
        self.sum = 0.0
        self.count = 0

    # --- HOT PATH ---------------------------------------------------------
    def observe(self, value: float) -> None:
        self.counts[bisect_left(self.bounds, value)] += 1
        self.sum += value
        self.count += 1

    def quantile(self, q: float) -> float:
        """Interpolation-free quantile estimate from the bucket counts.

        Returns the upper bound of the bucket containing the requested rank,
        which is exactly what a Prometheus ``histogram_quantile`` would report
        at bucket resolution - accurate enough for alerting, never quote it as
        a precise latency.
        """
        if self.count == 0:
            return 0.0
        target = q * self.count
        seen = 0
        for index, bucket_count in enumerate(self.counts):
            seen += bucket_count
            if seen >= target:
                return self.bounds[index] if index < len(self.bounds) else float("inf")
        return float("inf")

    @property
    def mean(self) -> float:
        return self.sum / self.count if self.count else 0.0


class Timer:
    """Context manager that records elapsed monotonic seconds into a histogram.

    Holds the histogram directly, so entering it costs one ``perf_counter``.
    """

    __slots__ = ("_histogram", "_started")

    def __init__(self, histogram: Histogram) -> None:
        self._histogram = histogram
        self._started = 0.0

    def __enter__(self) -> Timer:
        from time import perf_counter

        self._started = perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        from time import perf_counter

        self._histogram.observe(perf_counter() - self._started)
        return


class Registry:
    """Owns every metric instance and renders them on demand."""

    __slots__ = ("_metrics", "_help")

    def __init__(self) -> None:
        self._metrics: dict[tuple[str, _LabelKey], Any] = {}
        self._help: dict[str, tuple[str, str]] = {}  # name -> (type, help text)

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> tuple[str, _LabelKey]:
        return name, tuple(sorted(labels.items()))

    def counter(self, name: str, help: str = "", **labels: str) -> Counter:
        return self._get_or_create(Counter, "counter", name, help, labels)

    def gauge(self, name: str, help: str = "", **labels: str) -> Gauge:
        return self._get_or_create(Gauge, "gauge", name, help, labels)

    def histogram(
        self,
        name: str,
        help: str = "",
        bounds: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS,
        **labels: str,
    ) -> Histogram:
        key = self._key(name, labels)
        existing = self._metrics.get(key)
        if existing is None:
            existing = Histogram(name, labels, bounds)
            self._metrics[key] = existing
            self._help.setdefault(name, ("histogram", help))
        return existing

    def timer(self, name: str, help: str = "", **labels: str) -> Timer:
        return Timer(self.histogram(name, help, **labels))

    def _get_or_create(self, cls, kind: str, name: str, help: str, labels: dict[str, str]):
        key = self._key(name, labels)
        existing = self._metrics.get(key)
        if existing is None:
            existing = cls(name, labels)
            self._metrics[key] = existing
            self._help.setdefault(name, (kind, help))
        return existing

    def snapshot(self) -> dict[str, Any]:
        """Structured dump, for the admin endpoint and for tests."""
        out: dict[str, Any] = {}
        for (name, _), metric in sorted(self._metrics.items()):
            entry = out.setdefault(name, [])
            if isinstance(metric, Histogram):
                entry.append(
                    {
                        "labels": metric.labels,
                        "count": metric.count,
                        "sum": round(metric.sum, 6),
                        "mean": round(metric.mean, 6),
                        "p50": metric.quantile(0.50),
                        "p95": metric.quantile(0.95),
                        "p99": metric.quantile(0.99),
                    }
                )
            else:
                entry.append({"labels": metric.labels, "value": metric.value})
        return out

    def render_prometheus(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        by_name: dict[str, list[Any]] = {}
        for (name, _), metric in self._metrics.items():
            by_name.setdefault(name, []).append(metric)

        for name in sorted(by_name):
            kind, help_text = self._help.get(name, ("untyped", ""))
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for metric in by_name[name]:
                labels = metric.labels
                if isinstance(metric, Histogram):
                    cumulative = 0
                    for index, bound in enumerate(metric.bounds):
                        cumulative += metric.counts[index]
                        lines.append(
                            f"{name}_bucket{_fmt_labels(labels, le=_fmt_float(bound))} {cumulative}"
                        )
                    cumulative += metric.counts[-1]
                    lines.append(f"{name}_bucket{_fmt_labels(labels, le='+Inf')} {cumulative}")
                    lines.append(f"{name}_sum{_fmt_labels(labels)} {metric.sum!r}")
                    lines.append(f"{name}_count{_fmt_labels(labels)} {metric.count}")
                else:
                    lines.append(f"{name}{_fmt_labels(labels)} {metric.value!r}")
        return "\n".join(lines) + "\n"


def _fmt_float(value: float) -> str:
    return repr(value)


def _fmt_labels(labels: dict[str, str], **extra: str) -> str:
    merged = {**labels, **extra}
    if not merged:
        return ""
    inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in merged.items())
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
