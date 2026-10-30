# SPDX-License-Identifier: AGPL-3.0-only
"""Round-15 regression: creation assessment identity must match the new authoritative binding."""
from __future__ import annotations

from pathlib import Path

import pytest

from loop_learning_support import (
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    binding_authority,
    contract,
    grant,
    learning_request,
)

from pados.capability_loop.contracts import Confidence, FailureCode, ProviderHealth, ProviderStatus
from pados.capability_loop.learning import LearningService
from pados.capability_loop.learning_contracts import (
    LearningAssessment,
    LearningOutcome,
    LearningRequest,
    LearningSession,
    LearningSessionStatus,
    MasteryLevel,
    ModelRouting,
)
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

PROVIDER = "stub.learning"
SESSION = "sess-r15"
GAP = "gap:cad_edit"


def _session() -> LearningSession:
    return LearningSession(
        provider_id=PROVIDER,
        session_ref=SESSION,
        status=LearningSessionStatus.READY,
        model_routing=ModelRouting.EXTERNAL_PROVIDER_CONTROLLED,
        materials_bound=0,
        progress_percent=100,
    )


def _assessment(*, provider_id: str = PROVIDER, session_ref: str = SESSION,
                capability_gap_ref: str = GAP) -> LearningAssessment:
    return LearningAssessment(
        provider_id=provider_id,
        session_ref=session_ref,
        capability_gap_ref=capability_gap_ref,
        human_mastery=MasteryLevel.PASSED,
        evidence_digest="d" * 64,
        confidence=Confidence.OBSERVED,
    )


class _CreationAssessmentProvider:
    provider_id = PROVIDER

    def __init__(self, assessment: LearningAssessment) -> None:
        self._assessment = assessment

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)

    def create_session(self, request: LearningRequest) -> LearningOutcome:
        return LearningOutcome(
            provider_id=self.provider_id,
            status=ProviderStatus.AVAILABLE,
            session=_session(),
            assessment=self._assessment,
        )

    def session_state(self, session_ref: str) -> LearningOutcome:
        raise AssertionError("follow-up must not be called")

    def assessment(self, session_ref: str, capability_gap_ref: str) -> LearningOutcome:
        raise AssertionError("follow-up must not be called")


@pytest.mark.parametrize(
    "assessment",
    [
        _assessment(provider_id="other.learning"),
        _assessment(session_ref="sess-other"),
        _assessment(capability_gap_ref="gap:other"),
    ],
)
def test_creation_assessment_identity_mismatch_is_refused_before_binding(
    tmp_path: Path, assessment: LearningAssessment
) -> None:
    store = LoopStore(tmp_path / "loop.json")
    authority = binding_authority()
    service = LearningService(
        _CreationAssessmentProvider(assessment),
        store,
        capability=LEARNING_CAPABILITY,
        adapter=LEARNING_ADAPTER,
        authority=authority,
    )

    receipt = service.create_session(
        learning_request(),
        contract=contract(),
        grant=grant(),
        classification=DataClassification.INTERNAL,
    )

    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.MALFORMED_RESPONSE
    assert authority.resolve(PROVIDER, SESSION) is None
    assert store.progress() == {}
