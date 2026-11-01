# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded in-process service metrics with stable names and labels (OP-008).

Every metric is declared in ``CATALOG`` with its kind, help text and exact label names; the names are the contract
dashboards and alerts will bind to, so they change only with a new name. Recording never raises into the caller:

* an undeclared metric or a wrong label set is dropped and counted in ``pados_metrics_rejected_total``;
* a label value outside a short safe alphabet becomes ``__invalid__`` (so ids, paths, secrets or free text never
  become series);
* each metric holds at most ``MAX_SERIES`` series; further label combinations collapse into one ``__overflow__``
  series.

Metrics are observations only. They are never read back by authorization, approval, scheduling, idempotency or
receipt code. The process exposes them (``render_prometheus``); exporting them elsewhere is a deployment concern.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

MAX_SERIES = 256
INVALID = "__invalid__"
OVERFLOW = "__overflow__"
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/{}-]{1,96}$")
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


class Kind(StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"


@dataclass(frozen=True)
class MetricSpec:
    kind: Kind
    help: str
    labels: tuple[str, ...]


CATALOG: Mapping[str, MetricSpec] = {
    "pados_http_requests_total": MetricSpec(
        Kind.COUNTER, "HTTP requests by route template, status class and outcome.",
        ("method", "route", "status_class", "outcome")),
    "pados_http_request_duration_seconds": MetricSpec(
        Kind.HISTOGRAM, "HTTP request duration by route template.", ("method", "route")),
    "pados_http_errors_total": MetricSpec(
        Kind.COUNTER, "HTTP error responses by stable error code.", ("route", "error_code")),
    "pados_dependency_failures_total": MetricSpec(
        Kind.COUNTER, "Dependency failures (database, compute node, model provider).", ("dependency", "error_code")),
    "pados_retry_attempts_total": MetricSpec(
        Kind.COUNTER, "Bounded retries by operation: retried or exhausted.", ("operation", "result")),
    "pados_deadline_exceeded_total": MetricSpec(
        Kind.COUNTER, "Work refused or stopped because the parent deadline ran out.", ("component",)),
    "pados_operation_duration_seconds": MetricSpec(
        Kind.HISTOGRAM, "Duration of a named internal operation by outcome.", ("operation", "outcome")),
    "pados_metrics_rejected_total": MetricSpec(
        Kind.COUNTER, "Metric recordings dropped because the name or label set is not declared.", ("reason",)),
}


def _status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= status <= 599 else "other"


class _Histogram:
    __slots__ = ("buckets", "count", "total")

    def __init__(self) -> None:
        self.buckets = [0] * len(DURATION_BUCKETS)
        self.count = 0
        self.total = 0.0

    def observe(self, value: float) -> None:
        for index, bound in enumerate(DURATION_BUCKETS):
            if value <= bound:
                self.buckets[index] += 1
        self.count += 1
        self.total += value


class Registry:
    def __init__(self, catalog: Mapping[str, MetricSpec] = CATALOG, max_series: int = MAX_SERIES) -> None:
        self._catalog = catalog
        self._max_series = max_series
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[str, ...], float]] = {}
        self._histograms: dict[str, dict[tuple[str, ...], _Histogram]] = {}

    def _key(self, name: str, labels: Mapping[str, str], kind: Kind) -> tuple[str, ...] | None:
        spec = self._catalog.get(name)
        if spec is None or spec.kind is not kind:
            self._reject("UNDECLARED_METRIC")
            return None
        if set(labels) != set(spec.labels):
            self._reject("LABEL_SET_MISMATCH")
            return None
        return tuple(value if _SAFE_VALUE.fullmatch(value := str(labels[label])) else INVALID
                     for label in spec.labels)

    def _bounded(self, series: Mapping[tuple[str, ...], object], key: tuple[str, ...]) -> tuple[str, ...]:
        if key in series or len(series) < self._max_series:
            return key
        return (OVERFLOW,) * len(key)

    def _reject(self, reason: str) -> None:
        with self._lock:
            series = self._counters.setdefault("pados_metrics_rejected_total", {})
            series[(reason,)] = series.get((reason,), 0.0) + 1

    def inc(self, name: str, labels: Mapping[str, str], amount: float = 1.0) -> None:
        key = self._key(name, labels, Kind.COUNTER)
        if key is None or amount < 0:
            return
        with self._lock:
            series = self._counters.setdefault(name, {})
            key = self._bounded(series, key)
            series[key] = series.get(key, 0.0) + amount

    def observe(self, name: str, labels: Mapping[str, str], value: float) -> None:
        key = self._key(name, labels, Kind.HISTOGRAM)
        if key is None or value < 0:
            return
        with self._lock:
            series = self._histograms.setdefault(name, {})
            key = self._bounded(series, key)
            series.setdefault(key, _Histogram()).observe(value)

    def counter_value(self, name: str, labels: Mapping[str, str]) -> float:
        spec = self._catalog[name]
        with self._lock:
            return self._counters.get(name, {}).get(tuple(str(labels[label]) for label in spec.labels), 0.0)

    def histogram_count(self, name: str, labels: Mapping[str, str]) -> int:
        spec = self._catalog[name]
        with self._lock:
            histogram = self._histograms.get(name, {}).get(tuple(str(labels[label]) for label in spec.labels))
            return 0 if histogram is None else histogram.count

    def series_count(self, name: str) -> int:
        with self._lock:
            return len(self._counters.get(name, {})) + len(self._histograms.get(name, {}))

    def render_prometheus(self) -> str:
        """The Prometheus text exposition format (0.0.4) for every declared metric."""
        lines: list[str] = []
        with self._lock:
            for name, spec in self._catalog.items():
                lines += [f"# HELP {name} {spec.help}", f"# TYPE {name} {spec.kind.value}"]
                if spec.kind is Kind.COUNTER:
                    for key, value in sorted(self._counters.get(name, {}).items()):
                        lines.append(f"{name}{_labels(spec.labels, key)} {value:g}")
                    continue
                for key, histogram in sorted(self._histograms.get(name, {}).items()):
                    for bound, count in zip(DURATION_BUCKETS, histogram.buckets, strict=True):
                        lines.append(f"{name}_bucket{_labels(spec.labels, key, le=f'{bound:g}')} {count}")
                    lines.append(f"{name}_bucket{_labels(spec.labels, key, le='+Inf')} {histogram.count}")
                    lines.append(f"{name}_sum{_labels(spec.labels, key)} {histogram.total:.6f}")
                    lines.append(f"{name}_count{_labels(spec.labels, key)} {histogram.count}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


def _labels(names: tuple[str, ...], values: tuple[str, ...], *, le: str | None = None) -> str:
    # Values already match the safe alphabet (no quote, backslash or newline), so no escaping is needed.
    pairs = [f'{name}="{value}"' for name, value in zip(names, values, strict=True)]
    if le is not None:
        pairs.append(f'le="{le}"')
    return "{" + ",".join(pairs) + "}"


METRICS = Registry()


def record_http_request(*, method: str, route: str, status: int, duration_s: float, error_code: str | None) -> None:
    outcome = "completed" if status < 400 else "rejected" if status < 500 else "failed"
    METRICS.inc("pados_http_requests_total", {"method": method.upper(), "route": route,
                                              "status_class": _status_class(status), "outcome": outcome})
    METRICS.observe("pados_http_request_duration_seconds", {"method": method.upper(), "route": route}, duration_s)
    if error_code:
        METRICS.inc("pados_http_errors_total", {"route": route, "error_code": error_code})


@contextmanager
def timed(operation: str) -> Iterator[None]:
    """Record ``operation``'s duration with outcome ok or error; the exception is re-raised unchanged."""
    started = time.perf_counter()
    outcome = "error"
    try:
        yield
        outcome = "ok"
    finally:
        METRICS.observe("pados_operation_duration_seconds", {"operation": operation, "outcome": outcome},
                        time.perf_counter() - started)
