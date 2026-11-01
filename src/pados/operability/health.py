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


ProbeResult = tuple[Any, float, str | None]


class SharedProbe:
    """Per-endpoint probe state: at most one probe thread in flight, and a finished result reused for ``cache_s``.

    While a probe hangs (a database that accepts connections but never answers), later callers wait on the same run
    instead of starting another thread, so polling or hammering an unauthenticated readiness endpoint leaves at most
    one blocked thread behind. A joining caller's latency counts from the start of the shared run.
    """

    def __init__(self, cache_s: float = 2.0) -> None:
        self.cache_s = cache_s
        self._lock = threading.Lock()
        self._pending: tuple[threading.Event, list[Any], float] | None = None
        self._last: tuple[float, ProbeResult] | None = None
        self.started_runs = 0

    def run(self, probe: Callable[[], Any], timeout_s: float) -> ProbeResult:
        with self._lock:
            if self._last is not None and time.monotonic() - self._last[0] < self.cache_s:
                return self._last[1]
            if self._pending is None:
                self._pending = self._start(probe)
            done, box, started = self._pending
        if not done.wait(timeout_s):
            return None, round((time.perf_counter() - started) * 1000, 3), "PROBE_TIMEOUT"
        (ok, value), latency = box[0], box[1]
        result: ProbeResult = (value, latency, None) if ok else (None, latency, "PROBE_FAILED")
        with self._lock:
            self._last = (time.monotonic(), result)
        return result

    def _start(self, probe: Callable[[], Any]) -> tuple[threading.Event, list[Any], float]:
        done, started = threading.Event(), time.perf_counter()
        box: list[Any] = []  # [(ok, value), latency_ms] once the run finishes

        def work() -> None:
            try:
                box.append((True, probe()))
            except Exception:  # noqa: BLE001 - the probe's failure is reported as a code, never re-raised or echoed
                box.append((False, None))
            box.append(round((time.perf_counter() - started) * 1000, 3))
            with self._lock:
                self._pending = None
            done.set()

        self.started_runs += 1
        threading.Thread(target=work, name="pados-health-probe", daemon=True).start()
        return done, box, started


def bounded(probe: Callable[[], Any], timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
            shared: SharedProbe | None = None) -> ProbeResult:
    """Run ``probe`` in a daemon thread with a deadline: (value, latency_ms, error code or None).

    A probe that does not finish in time leaves its thread behind (daemon, it cannot block shutdown) and reports
    ``PROBE_TIMEOUT``; a probe that raises reports ``PROBE_FAILED`` and never its message. Pass the endpoint's
    ``SharedProbe`` so concurrent callers share one run instead of each starting a thread.
    """
    return (shared or SharedProbe(cache_s=0.0)).run(probe, timeout_s)


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
