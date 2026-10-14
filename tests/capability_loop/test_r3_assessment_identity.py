# SPDX-License-Identifier: AGPL-3.0-only
"""Round-3 Finding 4: an assessment's own identity must match the authorized binding.

A well-typed response can carry a matching session object but an assessment attributed to a different
session or capability gap. Such a response is rejected before its evidence/mastery is recorded against the
bound progress row.
"""
from __future__ import annotations

from pathlib import Path

from loop_learning_support import LEARNING_ADAPTER, LEARNING_CAPABILITY, binding_authority, contract, grant, learning_request

from pados.capability_loop.contracts import Confidence, ProviderHealth, ProviderStatus
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
SESSION = "sess-1"


def _session() -> LearningSession:
    return LearningSession(provider_id=PROVIDER, session_ref=SESSION, status=LearningSessionStatus.READY,
                           model_routing=ModelRouting.EXTERNAL_PROVIDER_CONTROLLED, materials_bound=0,
                           progress_percent=100)


def _assessment(gap: str) -> LearningAssessment:
    return LearningAssessment(provider_id=PROVIDER, session_ref=SESSION, capability_gap_ref=gap,
                              human_mastery=MasteryLevel.PASSED, evidence_digest="c" * 64,
                              confidence=Confidence.OBSERVED)


class _StubProvider:
    provider_id = PROVIDER

    def __init__(self, assessment_gap: str) -> None:
        self._assessment_gap = assessment_gap

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)

    def create_session(self, request: LearningRequest) -> LearningOutcome:
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, session=_session())

    def session_state(self, session_ref: str) -> LearningOutcome:
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, session=_session())

    def assessment(self, session_ref: str, capability_gap_ref: str) -> LearningOutcome:
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE,
                               session=_session(), assessment=_assessment(self._assessment_gap))


def _service(store: LoopStore, provider: _StubProvider, authority: object) -> LearningService:
    return LearningService(provider, store, capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER,
                           authority=authority)


def _create(store: LoopStore, provider: _StubProvider, authority: object) -> None:
    # binding.capability_gap_ref is the request's gap (gap:cad_edit from the fixture).
    assert _service(store, provider, authority).create_session(
        learning_request(), contract=contract(), grant=grant(),
        classification=DataClassification.INTERNAL).result is OperationResult.RECORDED


def test_assessment_for_another_gap_is_rejected(tmp_path: Path) -> None:
    provider = _StubProvider(assessment_gap="gap:other")  # not the bound gap:cad_edit
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    _create(store, provider, auth)
    receipt = _service(store, provider, auth).assessment(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    # The mis-attributed evidence is not recorded against the bound progress row.
    assert "human_mastery" not in store.progress().get(SESSION, {})


def test_assessment_for_the_bound_gap_is_accepted(tmp_path: Path) -> None:
    provider = _StubProvider(assessment_gap="gap:cad_edit")  # the bound gap
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    _create(store, provider, auth)
    receipt = _service(store, provider, auth).assessment(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED
    assert store.progress()[SESSION]["human_mastery"] == MasteryLevel.PASSED.value
