# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 9: a discovery result is only successful under an explicit, whole invariant.

A candidate is accepted into the loop only when ``status == AVAILABLE and failure is None and not partial``.
Malformed status/failure combinations are rejected at the model level; a partial AVAILABLE answer is a
non-acceptance at the service level. ``failure is None`` alone is never treated as success.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from loop_support import (
    DISCOVERY_ADAPTER,
    DISCOVERY_CAPABILITY,
    PROJECT,
    WORK,
    contract,
    grant,
    target,
)
from pydantic import ValidationError

from pados.capability_loop.contracts import (
    Confidence,
    DiscoveryEvidence,
    DiscoveryOutcome,
    DiscoveryRequest,
    FailureCode,
    ProviderHealth,
    ProviderStatus,
)
from pados.capability_loop.discovery import DiscoveryService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

PROVIDER_ID = "rea.capability_discovery"


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(request_id="req-1", project_id=PROJECT, work_contract_id=WORK, goal_ref="goal-1",
                            required_capability="BINARY_INSPECTION", target_ref="target:app",
                            classification=DataClassification.INTERNAL)


def _evidence() -> DiscoveryEvidence:
    return DiscoveryEvidence(evidence_digest="a" * 64, operation="INSPECT_RUNTIME_CATALOG",
                             confidence=Confidence.OBSERVED)


class _StubProvider:
    provider_id = PROVIDER_ID

    def __init__(self, outcome: DiscoveryOutcome) -> None:
        self._outcome = outcome

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)

    def discover(self, request: DiscoveryRequest, *, target_path: str) -> DiscoveryOutcome:
        return self._outcome


# -- Model-level invariant: malformed status/failure combinations are impossible to build --
@pytest.mark.parametrize("status", [ProviderStatus.UNAVAILABLE, ProviderStatus.DEGRADED,
                                     ProviderStatus.UNAUTHORIZED])
def test_non_available_status_without_failure_is_rejected(status: ProviderStatus) -> None:
    with pytest.raises(ValidationError):
        DiscoveryOutcome(provider_id=PROVIDER_ID, status=status, failure=None)


def test_available_status_with_failure_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DiscoveryOutcome(provider_id=PROVIDER_ID, status=ProviderStatus.AVAILABLE,
                         failure=FailureCode.TIMEOUT)


# -- Service-level: a partial AVAILABLE answer is not accepted --
def test_partial_available_outcome_is_not_accepted(tmp_path: Path) -> None:
    outcome = DiscoveryOutcome(provider_id=PROVIDER_ID, status=ProviderStatus.AVAILABLE, failure=None,
                               evidence=(_evidence(),), capability_supported=True, partial=True)
    store = LoopStore(tmp_path / "loop.json")
    service = DiscoveryService(_StubProvider(outcome), store, capability=DISCOVERY_CAPABILITY,
                               adapter=DISCOVERY_ADAPTER)
    receipt = service.discover(_request(), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.PARTIAL_RESULT
    assert not receipt.accepted_into_loop and receipt.state_after is None
    assert not store.candidates()  # a partial answer never creates a candidate


def test_fully_successful_outcome_is_accepted(tmp_path: Path) -> None:
    outcome = DiscoveryOutcome(provider_id=PROVIDER_ID, status=ProviderStatus.AVAILABLE, failure=None,
                               evidence=(_evidence(),), capability_supported=True, partial=False)
    store = LoopStore(tmp_path / "loop.json")
    service = DiscoveryService(_StubProvider(outcome), store, capability=DISCOVERY_CAPABILITY,
                               adapter=DISCOVERY_ADAPTER)
    receipt = service.discover(_request(), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED and receipt.accepted_into_loop
    assert len(store.candidates()) == 1
