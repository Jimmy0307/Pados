# SPDX-License-Identifier: AGPL-3.0-only
"""Round-4 Finding 3: a creation that returns no session always records a normalized failure.

A well-typed outcome with ``session=None`` and ``failure=None`` is itself malformed; the creation-failure
receipt must carry MALFORMED_RESPONSE rather than a null failure, so callers always have a normalized reason.
"""
from __future__ import annotations

from pathlib import Path

from loop_learning_support import LEARNING_ADAPTER, LEARNING_CAPABILITY, contract, grant, learning_request

from pados.capability_loop.contracts import FailureCode, ProviderHealth, ProviderStatus
from pados.capability_loop.learning import LearningService
from pados.capability_loop.learning_contracts import LearningOutcome, LearningRequest
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification


class _NoSessionProvider:
    provider_id = "openmaic.adaptive_learning"

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)

    def create_session(self, request: LearningRequest) -> LearningOutcome:
        # Well-typed but incomplete: no session and no explicit failure.
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)

    def session_state(self, session_ref: str) -> LearningOutcome:  # pragma: no cover - unused here
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)

    def assessment(self, session_ref: str, capability_gap_ref: str) -> LearningOutcome:  # pragma: no cover
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)


def test_no_session_creation_records_malformed_response(tmp_path: Path) -> None:
    store = LoopStore(tmp_path / "loop.json")
    service = LearningService(_NoSessionProvider(), store, capability=LEARNING_CAPABILITY,
                              adapter=LEARNING_ADAPTER)
    receipt = service.create_session(learning_request(), contract=contract(), grant=grant(),
                                     classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.MALFORMED_RESPONSE
    assert not receipt.accepted_into_loop and not store.progress()
