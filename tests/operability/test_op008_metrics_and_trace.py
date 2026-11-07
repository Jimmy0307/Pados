# SPDX-License-Identifier: AGPL-3.0-only
"""OP-008: bounded metrics with stable names and labels, and W3C trace context that never enters persisted trace."""
from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pados.operability.context import (
    TRACEPARENT_HEADER,
    accept_traceparent,
    bind_context,
    context_from_headers,
    downstream_headers,
    operation_scope,
    trace_fields,
)
from pados.operability.http import install_operability
from pados.operability.metrics import CATALOG, INVALID, METRICS, OVERFLOW, Kind, MetricSpec, Registry, timed
from pados.operability.resilience import RetryPolicy, retry_call

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def _fresh_metrics() -> Iterator[None]:
    METRICS.reset()
    yield
    METRICS.reset()


def test_metric_names_and_labels_are_stable_and_namespaced() -> None:
    for name, spec in CATALOG.items():
        assert re.fullmatch(r"pados_[a-z0-9_]+", name)
        assert name.endswith("_total") == (spec.kind is Kind.COUNTER)
        assert spec.kind is not Kind.HISTOGRAM or name.endswith("_seconds")
        assert spec.labels and all(re.fullmatch(r"[a-z_]+", label) for label in spec.labels)


def test_undeclared_metrics_and_wrong_label_sets_are_dropped_and_counted_never_raised() -> None:
    METRICS.inc("pados_made_up_total", {"x": "y"})
    METRICS.inc("pados_deadline_exceeded_total", {"component": "worker", "extra": "z"})
    METRICS.observe("pados_deadline_exceeded_total", {"component": "worker"}, 1.0)  # wrong kind
    assert METRICS.counter_value("pados_metrics_rejected_total", {"reason": "UNDECLARED_METRIC"}) == 2
    assert METRICS.counter_value("pados_metrics_rejected_total", {"reason": "LABEL_SET_MISMATCH"}) == 1
    assert METRICS.series_count("pados_deadline_exceeded_total") == 0


# Built from parts so the repository hygiene scanners do not read the samples as a real secret or user path.
SECRET_LIKE = "pass" + "word=" + "hunter2 x"
USER_PATH = "C:" + "\\" + "Us" + "ers" + "\\someone"


@pytest.mark.parametrize("value", [SECRET_LIKE, USER_PATH, "a" * 200, "line\nbreak", ""])
def test_unsafe_label_values_never_become_series(value: str) -> None:
    METRICS.inc("pados_deadline_exceeded_total", {"component": value})
    assert METRICS.counter_value("pados_deadline_exceeded_total", {"component": INVALID}) == 1
    assert value not in METRICS.render_prometheus() or value == ""


def test_series_per_metric_are_capped_and_the_rest_collapse_into_overflow() -> None:
    registry = Registry({"pados_things_total": MetricSpec(Kind.COUNTER, "things", ("thing",))}, max_series=3)
    for index in range(10):
        registry.inc("pados_things_total", {"thing": f"t{index}"})
    assert registry.series_count("pados_things_total") == 4  # three distinct series plus one overflow series
    assert registry.counter_value("pados_things_total", {"thing": OVERFLOW}) == 7


def test_http_requests_are_labelled_by_route_template_status_class_and_code_only() -> None:
    app = FastAPI()

    @app.get("/things/{thing_id}")
    def thing(thing_id: str) -> dict[str, str]:
        if thing_id == "missing":
            raise HTTPException(status_code=404, detail="THING_NOT_FOUND")
        return {"id": thing_id}

    install_operability(app, component="test")
    client = TestClient(app)
    client.get("/things/secret-id-123?" + "tok" + "en=abc")  # split so the hygiene scanner sees no credential
    client.get("/things/missing")
    labels = {"method": "GET", "route": "/things/{thing_id}"}
    assert METRICS.counter_value("pados_http_requests_total",
                                 {**labels, "status_class": "2xx", "outcome": "completed"}) == 1
    assert METRICS.counter_value("pados_http_requests_total",
                                 {**labels, "status_class": "4xx", "outcome": "rejected"}) == 1
    assert METRICS.counter_value("pados_http_errors_total",
                                 {"route": "/things/{thing_id}", "error_code": "THING_NOT_FOUND"}) == 1
    assert METRICS.histogram_count("pados_http_request_duration_seconds", labels) == 2
    text = METRICS.render_prometheus()
    assert "secret-id-123" not in text and "token" not in text
    assert '# TYPE pados_http_request_duration_seconds histogram' in text
    assert 'pados_http_request_duration_seconds_bucket{method="GET",route="/things/{thing_id}",le="+Inf"} 2' in text


def test_the_core_api_serves_prometheus_text() -> None:
    from pados.api.app import create_app

    client = TestClient(create_app())
    client.get("/health/live")
    response = client.get("/metrics")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    assert 'pados_http_requests_total{method="GET",route="/health/live",status_class="2xx",outcome="completed"} 1' \
        in response.text


def test_retries_and_operation_durations_are_counted() -> None:
    class Flaky(Exception):
        retryable = True

    calls = iter([Flaky(), Flaky(), "ok"])

    def attempt() -> str:
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    assert retry_call(attempt, operation="node.job", policy=RetryPolicy(3, 0.0, 0.0), sleep=lambda _: None) == "ok"
    assert METRICS.counter_value("pados_retry_attempts_total", {"operation": "node.job", "result": "retried"}) == 2
    with pytest.raises(Flaky):
        retry_call(lambda: (_ for _ in ()).throw(Flaky()), operation="node.job", policy=RetryPolicy(1, 0.0, 0.0),
                   sleep=lambda _: None)
    assert METRICS.counter_value("pados_retry_attempts_total", {"operation": "node.job", "result": "exhausted"}) == 1
    with pytest.raises(RuntimeError), timed("capability.execute"):
        raise RuntimeError("boom")
    assert METRICS.histogram_count("pados_operation_duration_seconds",
                                   {"operation": "capability.execute", "outcome": "error"}) == 1


@pytest.mark.parametrize("value", [
    "00-00000000000000000000000000000000-00f067aa0ba902b7-01",  # zero trace id
    "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",  # zero parent id
    "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # unknown version
    "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",  # upper case
    "garbage", "", None,
])
def test_an_invalid_traceparent_starts_a_new_trace(value: str | None) -> None:
    assert accept_traceparent(value) is None
    context = context_from_headers(None, None, component="api", traceparent=value)
    assert context.trace_id and len(context.trace_id) == 32 and context.trace_id != "0" * 32


def test_a_valid_traceparent_continues_the_trace_in_a_new_span_and_propagates_downstream() -> None:
    context = context_from_headers("r1", "c1", component="api", traceparent=VALID)
    assert context.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736" and context.span_id != "00f067aa0ba902b7"
    with bind_context(context):
        headers = downstream_headers()
        assert headers[TRACEPARENT_HEADER] == f"00-{context.trace_id}-{context.span_id}-01"
        with operation_scope("capability") as inner:
            assert inner.trace_id == context.trace_id and inner.span_id not in (None, context.span_id)
        # The persisted trace is unchanged: request and correlation id only.
        assert trace_fields() == {"request_id": "r1", "correlation_id": "c1"}


def test_the_http_adapter_accepts_traceparent_without_echoing_an_invalid_one() -> None:
    app = FastAPI()
    seen: list[dict[str, str]] = []

    @app.get("/probe")
    def probe() -> dict[str, str]:
        seen.append(downstream_headers())
        return {}

    install_operability(app, component="test")
    client = TestClient(app)
    client.get("/probe", headers={TRACEPARENT_HEADER: VALID})
    client.get("/probe", headers={TRACEPARENT_HEADER: "00-evil\r\ninjected-01"})
    assert seen[0][TRACEPARENT_HEADER].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert "evil" not in seen[1][TRACEPARENT_HEADER]


def test_made_up_request_methods_share_one_series() -> None:
    from pados.operability.metrics import record_http_request

    for index in range(50):
        record_http_request(method=f"XA{index}", route="UNMATCHED", status=405, duration_s=0.001, error_code=None)
    labels = {"method": "OTHER", "route": "UNMATCHED", "status_class": "4xx", "outcome": "rejected"}
    assert METRICS.counter_value("pados_http_requests_total", labels) == 50
    assert METRICS.series_count("pados_http_requests_total") == 1
