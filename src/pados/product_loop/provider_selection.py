# SPDX-License-Identifier: AGPL-3.0-only
"""Product-loop provider selection over the existing PADOS Worker Router.

The Worker Router remains the single ranking authority. This module only
narrows its candidates to worker types that have a product-loop Maker adapter,
and applies an explicit operator preference (``PADOS_PRODUCT_LOOP_PROVIDER``).
Selection never authorizes work: the governed WorkContract, declared scope and
semantic CAS still decide what executes and what is accepted.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pados.worker.router import select_worker
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerSpec, WorkerType

# Worker types with a product-loop Maker adapter. Adding Claude Code or a local
# inference adapter later means registering it here; routing is unchanged.
PRODUCT_LOOP_ADAPTERS: dict[WorkerType, str] = {WorkerType.CODEX_CLI: "PADOS_CODEX_CLI"}
PREFERENCES: dict[str, WorkerType | None] = {
    "AUTO": None,
    "CODEX": WorkerType.CODEX_CLI,
    "CLAUDE_CODE": WorkerType.CLAUDE_CODE,
}


@dataclass(frozen=True)
class ProviderSelection:
    preference: str
    worker: WorkerSpec | None
    provider_id: str | None
    error_code: str | None
    explanation: str

    @property
    def selected(self) -> bool:
        return self.worker is not None and self.error_code is None

    def diagnostics(self) -> dict[str, str | None]:
        return {
            "preference": self.preference,
            "selected_worker_id": self.worker.worker_id if self.worker else None,
            "selected_worker_type": self.worker.worker_type.value if self.worker else None,
            "provider_id": self.provider_id,
            "error_code": self.error_code,
            "explanation": self.explanation,
        }


def select_product_loop_provider(preference: str, workers: Mapping[str, WorkerSpec]) -> ProviderSelection:
    normalized = (preference or "AUTO").strip().upper()
    if normalized not in PREFERENCES:
        return ProviderSelection(normalized, None, None, "PRODUCT_LOOP_PROVIDER_INVALID",
                                 f"unsupported preference; expected one of {sorted(PREFERENCES)}")
    pinned = PREFERENCES[normalized]
    if pinned is not None and pinned not in PRODUCT_LOOP_ADAPTERS:
        return ProviderSelection(normalized, None, None, "PRODUCT_LOOP_PROVIDER_ADAPTER_UNAVAILABLE",
                                 f"{pinned.value} has no product-loop Maker adapter")
    allowed = frozenset({pinned}) if pinned is not None else frozenset(PRODUCT_LOOP_ADAPTERS)
    decision = select_worker(dict(workers), capability_id=CapabilityId.MODIFY_CODE,
                             risk_level=RiskLevel.LOW, allowed_worker_types=allowed)
    if decision.selected_worker_id is None:
        return ProviderSelection(normalized, None, None,
                                 "PRODUCT_LOOP_PROVIDER_UNAVAILABLE" if pinned else (decision.error_code or "PRODUCT_LOOP_PROVIDER_UNAVAILABLE"),
                                 decision.explanation)
    worker = workers[decision.selected_worker_id]
    return ProviderSelection(normalized, worker, PRODUCT_LOOP_ADAPTERS[worker.worker_type], None, decision.explanation)
