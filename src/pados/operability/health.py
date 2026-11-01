# SPDX-License-Identifier: AGPL-3.0-only
"""One versioned health schema for liveness and readiness (OP-003, ``pados.health.v1``).

* Liveness says the process serves requests. It never claims a database, provider or any other dependency.
* Readiness runs bounded, non-mutating probes. Any required dependency that is not OK makes the service ``NOT_READY``
  (HTTP 503); an optional dependency that is degraded or failed makes it ``DEGRADED`` (still 200); an optional
  dependency that is simply not configured degrades nothing.
* Every check carries a stable reason code and a latency. Credentials, connection strings and exception text never
  appear.

Health reports observe; they never authorize, schedule or accept anything.
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HEALTH_SCHEMA = "pados.health.v1"
DEFAULT_PROBE_TIMEOUT_S = 5.0


class CheckStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class HealthStatus(StrEnum):
    ALIVE = "ALIVE"
    OK = "OK"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"


class DependencyCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    required: bool
    status: CheckStatus
    reason: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
    latency_ms: float | None = None


class HealthReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.health.v1"] = "pados.health.v1"
    kind: Literal["liveness", "readiness"]
    service: str
    version: str
    status: HealthStatus
    checks: tuple[DependencyCheck, ...] = ()

    @property
    def http_status(self) -> int:
        return 503 if self.status is HealthStatus.NOT_READY else 200


def liveness(service: str, version: str) -> HealthReport:
    return HealthReport(kind="liveness", service=service, version=version, status=HealthStatus.ALIVE)


def readiness(service: str, version: str, checks: tuple[DependencyCheck, ...]) -> HealthReport:
    if any(check.required and check.status is not CheckStatus.OK for check in checks):
        status = HealthStatus.NOT_READY
    elif any(not check.required and check.status in (CheckStatus.DEGRADED, CheckStatus.FAILED) for check in checks):
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.OK
    return HealthReport(kind="readiness", service=service, version=version, status=status, checks=checks)


def bounded(probe: Callable[[], Any], timeout_s: float = DEFAULT_PROBE_TIMEOUT_S) -> tuple[Any, float, str | None]:
    """Run ``probe`` in a daemon thread with a deadline: (value, latency_ms, error code or None).

    A probe that does not finish in time leaves its thread behind (daemon, it cannot block shutdown) and reports
    ``PROBE_TIMEOUT``; a probe that raises reports ``PROBE_FAILED`` and never its message.
    """
    results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            results.put((True, probe()))
        except Exception:  # noqa: BLE001 - the probe's failure is reported as a code, never re-raised or echoed
            results.put((False, None))

    started = time.perf_counter()
    threading.Thread(target=run, name="pados-health-probe", daemon=True).start()
    try:
        ok, value = results.get(timeout=timeout_s)
    except queue.Empty:
        return None, round((time.perf_counter() - started) * 1000, 3), "PROBE_TIMEOUT"
    latency = round((time.perf_counter() - started) * 1000, 3)
    return (value, latency, None) if ok else (None, latency, "PROBE_FAILED")


def checks_from_system_health(assessment: Mapping[str, Any] | None, *, product_required: bool,
                              latency_ms: float | None, probe_error: str | None) -> tuple[DependencyCheck, ...]:
    """Map the operations ``system_health`` assessment onto v1 checks without changing what it measures.

    The operational database is always required. The Product Loop dependencies (proof database, object root, product
    configuration, provider routing) are required only when ``product_required``; otherwise they are reported as
    optional. A probe that timed out or failed fails the required database check with its code.
    """
    if assessment is None:
        return (DependencyCheck(name="operational_database", required=True, status=CheckStatus.FAILED,
                                reason=probe_error or "PROBE_FAILED", latency_ms=latency_ms),)
    reasons = set(assessment.get("reasons", ()))
    dims = assessment.get("dimensions", {})

    def check(name: str, required: bool, ok: bool, failure: str | None, *, unconfigured: bool = False,
              degraded: bool = False) -> DependencyCheck:
        if ok:
            return DependencyCheck(name=name, required=required, status=CheckStatus.OK, latency_ms=latency_ms)
        if unconfigured:
            status = CheckStatus.NOT_CONFIGURED
        else:
            status = CheckStatus.DEGRADED if degraded and not required else CheckStatus.FAILED
        return DependencyCheck(name=name, required=required, status=status, reason=failure, latency_ms=latency_ms)

    operational_failure = next((r for r in ("OPERATIONAL_DB_UNAVAILABLE", "OPERATIONAL_MIGRATION_STALE")
                                if r in reasons), None)
    proof_failure = next((r for r in ("PROOF_DB_UNCONFIGURED", "PROOF_DB_NOT_SEPARATE", "PROOF_DB_UNAVAILABLE",
                                      "PROOF_MIGRATION_STALE") if r in reasons), None)
    return (
        check("operational_database", True, bool(dims.get("operational_usable")) and operational_failure is None,
              operational_failure or "OPERATIONAL_DB_UNAVAILABLE"),
        check("proof_database", product_required, proof_failure is None and bool(dims.get("proof_usable")),
              proof_failure or "PROOF_DB_UNAVAILABLE", unconfigured=proof_failure == "PROOF_DB_UNCONFIGURED"),
        check("product_object_root", product_required, bool(dims.get("object_root_usable")), "OBJECT_ROOT_UNUSABLE",
              unconfigured=not bool(dims.get("product_loop_configured"))),
        check("product_loop_configuration", product_required, bool(dims.get("product_loop_configured")),
              "PRODUCT_LOOP_UNCONFIGURED", unconfigured=True),
        # Unused while the Product Loop is disabled: an absent provider is then not configured, not degraded.
        check("provider_routing", product_required, bool(dims.get("provider_configured")), "PROVIDER_UNAVAILABLE",
              unconfigured=not product_required),
    )


def chat_provider_check(mode: str) -> DependencyCheck:
    """Optional: the configured chat provider mode (configuration only; a live call is separate evidence)."""
    if mode == "CONFIGURED":
        return DependencyCheck(name="chat_provider", required=False, status=CheckStatus.OK)
    if mode == "MOCK_CONTRACT_TEST_ONLY":
        return DependencyCheck(name="chat_provider", required=False, status=CheckStatus.DEGRADED,
                               reason="MOCK_CHAT_PROVIDER_SELECTED")
    return DependencyCheck(name="chat_provider", required=False, status=CheckStatus.NOT_CONFIGURED,
                           reason="CHAT_PROVIDER_UNCONFIGURED")
