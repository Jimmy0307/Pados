# SPDX-License-Identifier: AGPL-3.0-only
"""OP-003: one versioned health schema; liveness never claims dependencies; readiness fails on required ones only.

Probes are bounded and non-mutating, and health output never contains credentials, connection strings or exception
text. ``/ready`` keeps its existing decision and reports it in the same schema.
"""
from __future__ import annotations

import functools
import hashlib
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pados.operability import health as health_contract

CONTROL_VALUE = "pados-test-control-" + "value"
SECRET_TEXT = "pass" + "word=hunter2 host=db.internal.example user=pados"  # split so the file holds no secret pattern


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pados.config import get_settings

    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    monkeypatch.setenv("PADOS_CHAT_PROVIDER", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _assessment(*, operational: bool = True, proof: bool = True, provider: bool = True,
                product_configured: bool = True, reasons: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"dimensions": {"operational_usable": operational, "proof_usable": proof,
                           "object_root_usable": product_configured, "product_loop_configured": product_configured,
                           "provider_configured": provider},
            "reasons": list(reasons), "provider_live_uat": "NOT_APPLICABLE", "ready": not reasons}


def _client(monkeypatch: pytest.MonkeyPatch, assessment: Any, **settings_env: str) -> TestClient:
    for key, value in settings_env.items():
        monkeypatch.setenv(key, value)
    from pados.api import app as app_module
    from pados.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(app_module, "system_health", assessment if callable(assessment) else lambda _s: assessment)
    return TestClient(app_module.create_app())


def _checks(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in body["checks"]}


def test_healthy_core_is_ready_and_unconfigured_optional_lanes_degrade_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _assessment(proof=False, provider=False, product_configured=False,
                                              reasons=("PROOF_DB_UNCONFIGURED", "PRODUCT_LOOP_UNCONFIGURED",
                                                       "PROVIDER_UNAVAILABLE")))
    response = client.get("/health/ready")
    body = response.json()
    assert response.status_code == 200 and body["status"] == "OK"
    assert body["schema_version"] == "pados.health.v1" and body["kind"] == "readiness"
    checks = _checks(body)
    assert checks["operational_database"] == {**checks["operational_database"], "required": True, "status": "OK"}
    assert {checks[n]["status"] for n in ("proof_database", "product_loop_configuration", "provider_routing",
                                          "chat_provider")} == {"NOT_CONFIGURED"}
    assert all(check["required"] is False for name, check in checks.items() if name != "operational_database")


def test_a_required_database_failure_is_not_ready_with_a_safe_code(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _assessment(operational=False, reasons=("OPERATIONAL_DB_UNAVAILABLE",)))
    response = client.get("/health/ready")
    assert response.status_code == 503 and response.json()["status"] == "NOT_READY"
    database = _checks(response.json())["operational_database"]
    assert (database["status"], database["reason"]) == ("FAILED", "OPERATIONAL_DB_UNAVAILABLE")


def test_an_optional_degraded_dependency_keeps_the_service_ready_but_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _assessment(), PADOS_CHAT_PROVIDER="mock")
    response = client.get("/health/ready")
    assert response.status_code == 200 and response.json()["status"] == "DEGRADED"
    assert _checks(response.json())["chat_provider"]["reason"] == "MOCK_CHAT_PROVIDER_SELECTED"


def test_a_slow_probe_is_bounded_and_reported_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(_settings: Any) -> dict[str, Any]:
        time.sleep(2.0)
        return _assessment()

    client = _client(monkeypatch, slow)
    monkeypatch.setattr(health_contract, "bounded", functools.partial(health_contract.bounded, timeout_s=0.2))
    started = time.perf_counter()
    response = client.get("/health/ready")
    assert time.perf_counter() - started < 1.5
    assert response.status_code == 503
    assert _checks(response.json())["operational_database"]["reason"] == "PROBE_TIMEOUT"


def test_a_failing_probe_reports_a_code_and_never_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_settings: Any) -> dict[str, Any]:
        raise RuntimeError(SECRET_TEXT)

    response = _client(monkeypatch, broken).get("/health/ready")
    assert response.status_code == 503
    assert _checks(response.json())["operational_database"]["reason"] == "PROBE_FAILED"
    assert "hunter2" not in response.text and "db.internal" not in response.text


def test_liveness_never_touches_a_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_run(_settings: Any) -> dict[str, Any]:
        raise AssertionError("liveness must not probe dependencies")

    from pados.api import app as app_module

    monkeypatch.setattr(app_module, "check_database", must_not_run)
    response = _client(monkeypatch, must_not_run).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {**response.json(), "kind": "liveness", "status": "ALIVE", "checks": []}


def test_the_legacy_health_summary_never_echoes_driver_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(_settings: Any) -> dict[str, str]:
        raise RuntimeError(SECRET_TEXT)

    from pados.api import app as app_module

    monkeypatch.setattr(app_module, "check_database", failing)
    response = _client(monkeypatch, _assessment()).get("/health")
    assert response.status_code == 200 and response.json()["status"] == "degraded"
    assert response.json()["database"] == {"status": "error", "error": "DATABASE_UNAVAILABLE"}
    assert "hunter2" not in response.text and "db.internal" not in response.text


def test_product_loop_lanes_are_required_only_when_it_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    degraded_proof = _assessment(proof=False, reasons=("PROOF_DB_UNAVAILABLE",))
    disabled = _client(monkeypatch, degraded_proof).get("/health/ready")
    assert disabled.status_code == 200  # the proof lane is optional while the Product Loop is disabled
    enabled = _client(monkeypatch, degraded_proof, PADOS_PRODUCT_LOOP_ENABLED="true").get("/health/ready")
    assert enabled.status_code == 503
    assert _checks(enabled.json())["proof_database"] == {**_checks(enabled.json())["proof_database"],
                                                         "required": True, "status": "FAILED",
                                                         "reason": "PROOF_DB_UNAVAILABLE"}


@pytest.mark.parametrize("ready", [True, False])
def test_ready_keeps_its_decision_and_reports_it_in_the_same_schema(monkeypatch: pytest.MonkeyPatch,
                                                                     ready: bool) -> None:
    from pados.api import app as app_module

    monkeypatch.setattr(app_module, "check_database", lambda _settings: {"status": "ok"})
    assessment = _assessment() if ready else _assessment(provider=False, reasons=("PROVIDER_UNAVAILABLE",))
    response = _client(monkeypatch, assessment).get("/ready")
    assert response.status_code == (200 if ready else 503)
    body = response.json() if ready else response.json()["detail"]
    assert body["health"]["schema_version"] == "pados.health.v1"
    assert (body["health"]["status"] == "NOT_READY") is (not ready)
    assert "hunter2" not in json.dumps(body)
