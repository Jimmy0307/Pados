# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass

from pados.worker.registry import capability_registry
from pados.worker.schemas import (
    RISK_ORDER,
    CapabilityId,
    ErrorCode,
    RiskLevel,
    WorkerAvailability,
    WorkerSpec,
    WorkerType,
)

ROUTING_PRIORITY = [
    WorkerType.DETERMINISTIC_TOOL,
    WorkerType.OLLAMA,
    WorkerType.CLAUDE_CODE,
    WorkerType.CODEX_CLI,
]


@dataclass(frozen=True)
class RoutingDecision:
    selected_worker_id: str | None
    status: str
    error_code: str | None
    explanation: str
    considered: list[dict[str, object]]


def _risk_ok(requested: RiskLevel, ceiling: RiskLevel) -> bool:
    return RISK_ORDER[requested] <= RISK_ORDER[ceiling]


def select_worker(
    workers: dict[str, WorkerSpec],
    *,
    capability_id: CapabilityId,
    risk_level: RiskLevel,
    allowed_worker_types: frozenset[WorkerType] | None = None,
) -> RoutingDecision:
    capabilities = capability_registry()
    capability = capabilities.get(capability_id)
    if capability is None:
        return RoutingDecision(None, "BLOCKED", ErrorCode.CAPABILITY_MISMATCH.value, "unknown capability", [])

    considered: list[dict[str, object]] = []
    ordered = sorted(
        workers.values(),
        key=lambda worker: ROUTING_PRIORITY.index(worker.worker_type)
        if worker.worker_type in ROUTING_PRIORITY
        else 99,
    )
    for worker in ordered:
        # A caller may narrow candidates to worker types it can execute; ranking is unchanged.
        executable = allowed_worker_types is None or worker.worker_type in allowed_worker_types
        fit = (
            executable
            and capability_id in worker.capabilities
            and worker.worker_type in capability.allowed_worker_types
        )
        available = worker.availability == WorkerAvailability.AVAILABLE
        risk = _risk_ok(risk_level, worker.risk_ceiling)
        considered.append(
            {
                "worker_id": worker.worker_id,
                "worker_type": worker.worker_type.value,
                "capability_fit": fit,
                "adapter_executable": executable,
                "available": available,
                "risk_compatible": risk,
                "determinism": worker.worker_type == WorkerType.DETERMINISTIC_TOOL,
                "reason": worker.reason,
            }
        )
        if fit and available and risk:
            return RoutingDecision(
                worker.worker_id,
                "ROUTED",
                None,
                (
                    f"Selected {worker.display_name}: capability fit, availability, "
                    f"risk ceiling {worker.risk_ceiling.value}, deterministic priority applied."
                ),
                considered,
            )
    unavailable_fit = any(item["capability_fit"] and not item["available"] for item in considered)
    error = ErrorCode.WORKER_UNAVAILABLE.value if unavailable_fit else ErrorCode.NO_ELIGIBLE_WORKER.value
    return RoutingDecision(
        None,
        "BLOCKED",
        error,
        "No eligible worker satisfied capability, availability, risk and policy constraints.",
        considered,
    )
