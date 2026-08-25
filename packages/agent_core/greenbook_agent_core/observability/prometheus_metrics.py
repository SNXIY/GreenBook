"""Minimal Prometheus-compatible metrics registry.

Labels are bounded to low-cardinality dimensions (outcome, semantic_action,
retry_classification, route) — never user/task/conversation/operation ids or
free-text.  Histograms use fixed buckets so latency is comparable across runs.
This is intentionally dependency-free (no opentelemetry/prometheus client).
"""

from __future__ import annotations

import threading
from typing import Any

# Fixed histogram buckets in seconds, chosen to expose LLM vs queue vs Java
# vs reconciliation latency without explosion.
DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 120.0)


class _Metric:
    def __init__(self, name: str, help_: str, label_names: tuple[str, ...]) -> None:
        self.name = name
        self.help = help_
        self.label_names = label_names
        self._lock = threading.RLock()


class Counter(_Metric):
    """A labeled counter."""

    def __init__(self, name: str, help_: str, label_names: tuple[str, ...] = ()) -> None:
        super().__init__(name, help_, label_names)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = tuple(str(labels.get(name, "")) for name in self.label_names)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def collect(self) -> list[tuple[str, tuple[str, ...], float]]:
        with self._lock:
            return [(self.name, key, value) for key, value in self._values.items()]


class Histogram(_Metric):
    """A labeled histogram with fixed buckets (seconds)."""

    def __init__(self, name: str, help_: str, label_names: tuple[str, ...] = ()) -> None:
        super().__init__(name, help_, label_names)
        self._buckets = DEFAULT_BUCKETS
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(str(labels.get(name, "")) for name in self.label_names)
        value = max(0.0, float(value))
        with self._lock:
            counts = self._counts.setdefault(key, [0] * (len(self._buckets) + 1))
            for i, bound in enumerate(self._buckets):
                if value <= bound:
                    counts[i] += 1
            counts[-1] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value

    def collect(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for key, counts in self._counts.items():
                out.append({
                    "labels": key,
                    "buckets": self._buckets,
                    "counts": list(counts),
                    "sum": self._sums.get(key, 0.0),
                })
            return out


class MetricsRegistry:
    """Bounded registry of labeled counters + histograms + Prometheus text render."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.RLock()

    def counter(self, name: str, help_: str, label_names: tuple[str, ...] = ()) -> Counter:
        with self._lock:
            metric = self._counters.setdefault(name, Counter(name, help_, label_names))
            return metric

    def histogram(self, name: str, help_: str, label_names: tuple[str, ...] = ()) -> Histogram:
        with self._lock:
            metric = self._histograms.setdefault(name, Histogram(name, help_, label_names))
            return metric

    def render(self) -> str:
        lines: list[str] = []
        for counter in self._counters.values():
            lines.append(f"# HELP {counter.name} {counter.help}")
            lines.append(f"# TYPE {counter.name} counter")
            for name, labels, value in counter.collect():
                lines.append(f"{name}{_labels(counter.label_names, labels)} {_fmt(value)}")
        for hist in self._histograms.values():
            lines.append(f"# HELP {hist.name} {hist.help}")
            lines.append(f"# TYPE {hist.name} histogram")
            for item in hist.collect():
                label_str = _labels(hist.label_names, item["labels"])
                cumulative = 0
                for bound, count in zip(item["buckets"], item["counts"], strict=False):
                    cumulative += count
                    bucket_labels = _bucket_labels(label_str, bound)
                    lines.append(f"{hist.name}_bucket {bucket_labels} {cumulative}")
                inf_labels = _bucket_labels(label_str, "+Inf")
                lines.append(f"{hist.name}_bucket {inf_labels} {item['counts'][-1]}")
                lines.append(f"{hist.name}_sum{label_str} {_fmt(item['sum'])}")
                lines.append(f"{hist.name}_count{label_str} {item['counts'][-1]}")
        return "\n".join(lines) + "\n"


def _labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    pairs = ",".join(f'{name}="{value}"' for name, value in zip(names, values, strict=False))
    return "{" + pairs + "}"


def _bucket_labels(label_str: str, le: str) -> str:
    if not label_str:
        return f'{{le="{le}"}}'
    return f'{{le="{le}",{label_str[1:-1]}}}'


def _fmt(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)
