# SPDX-License-Identifier: AGPL-3.0-only
"""Structured, content-free observability hooks.

Events use OpenTelemetry-style dotted attribute names so a future OTel
exporter can map them 1:1 onto span/metric attributes. Only allowlisted
scalar attributes are accepted: prompts, queries, document text and secret
material cannot be logged through this API.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from pados.logging_config import get_logger

EVENTS = frozenset({
    "capability.selected", "authorization.decision", "retrieval.completed",
    "ocr.completed", "extraction.completed", "rerank.completed", "provider.completed",
    "evidence.packaged", "execution.outcome", "validation.outcome",
    # Local Compute Node V1 (additive)
    "node.probe", "node.selected",
})
ATTRIBUTES = frozenset({
    "capability_id", "capability_type", "decision", "reason", "project_id", "tenant_id",
    "principal_ref", "latency_ms", "evidence_count", "withheld_count", "classification",
    "backend_id", "reranker_id", "extractor_id", "provider_id", "execution_class", "outcome",
    "status", "run_id", "package_hash", "input_count", "strategy", "mode",
    # Local Compute Node V1 (additive)
    "node_id", "model_id", "node_state", "queue_delay_ms", "job_id", "verification",
})
_MAX_TEXT = 128
Sink = Callable[[dict[str, Any]], None]
_sinks: list[Sink] = []


def add_sink(sink: Sink) -> Callable[[], None]:
    _sinks.append(sink)
    return lambda: _sinks.remove(sink)


def emit(event: str, **attributes: Any) -> dict[str, Any]:
    if event not in EVENTS:
        raise ValueError("OBSERVABILITY_EVENT_UNKNOWN")
    record: dict[str, Any] = {"event": f"pados.enterprise.{event}"}
    for key, value in sorted(attributes.items()):
        if key not in ATTRIBUTES:
            raise ValueError(f"OBSERVABILITY_ATTRIBUTE_NOT_ALLOWED:{key}")
        if value is None:
            continue
        if isinstance(value, str) and len(value) > _MAX_TEXT:
            raise ValueError(f"OBSERVABILITY_ATTRIBUTE_TOO_LONG:{key}")
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError(f"OBSERVABILITY_ATTRIBUTE_NOT_SCALAR:{key}")
        record[f"pados.{key}"] = value
    get_logger("pados.enterprise").info(record["event"], **{k: v for k, v in record.items() if k != "event"})
    for sink in _sinks.copy():
        sink(dict(record))
    return record


@contextmanager
def timed() -> Iterator[Callable[[], float]]:
    start = time.perf_counter()
    yield lambda: round((time.perf_counter() - start) * 1000, 3)
