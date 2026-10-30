# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [42]: a follow-up is RECORDED/accepted only under the whole-success invariant.

``failure is None`` is not success. A model-valid follow-up that is partial, non-AVAILABLE, a state response
without a session, or an assessment response without an assessment is recorded as ``PROVIDER_FAILED`` with a
normalized failure code, is not accepted into the loop, and never updates the advisory projection.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loop_learning_support import LEARNING_ADAPTER, LEARNING_CAPABILITY, contract, grant, learning_request

from pados.capability_loop.adapters.openmaic_http import OPENMAIC_PROVIDER_ID
from pados.capability_loop.contracts import Confidence, FailureCode, ProviderStatus
from pados.capability_loop.learning import LearningService
from pados.capability_loop.learning_contracts import (
    LearningAssessment,
    LearningOutcome,
    LearningSession,
    LearningSessionStatus,
    MasteryLevel,
    ModelRouting,
)
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.capability_loop_runtime.binding import InMemorySessionBindingAuthority
from pados.enterprise.classification import DataClassification

PROVIDER = OPENMAIC_PROVIDER_ID
SESSION = "job-1"
GAP = "gap:cad_edit"


def _session(progress: int = 10) -> LearningSession:
    return LearningSession(provider_id=PROVIDER, session_ref=SESSION, status=LearningSessionStatus.IN_PROGRESS,
                           model_routing=ModelRouting.EXTERNAL_PROVIDER_CONTROLLED, materials_bound=0,
                           progress_percent=progress)


def _assessment() -> LearningAssessment:
    return LearningAssessment(provider_id=PROVIDER, session_ref=SESSION, capability_gap_ref=GAP,
                              human_mastery=MasteryLevel.IN_PROGRESS, confidence=Confidence.OBSERVED,
                              evidence_digest="e" * 64)


def _outcome(**overrides: Any) -> LearningOutcome:
    values: dict[str, Any] = {"provider_id": PROVIDER, "status": ProviderStatus.AVAILABLE, "failure": None,
                              "session": _session(), "assessment": None, "partial": False}
    values.update(overrides)
    return LearningOutcome(**values)


class _Scripted:
    def __init__(self, followup: LearningOutcome) -> None:
        self._followup = followup

    @property
    def provider_id(self) -> str:
        return PROVIDER

    def health(self) -> Any:
        raise NotImplementedError

    def create_session(self, request: Any) -> LearningOutcome:
        return _outcome()

    def session_state(self, session_ref: str) -> LearningOutcome:
        return self._followup

    def assessment(self, session_ref: str, capability_gap_ref: str) -> LearningOutcome:
        return self._followup


def _created(tmp_path: Path, followup: LearningOutcome) -> tuple[LearningService, LoopStore]:
    store = LoopStore(tmp_path / "loop.json")
    service = LearningService(_Scripted(followup), store, capability=LEARNING_CAPABILITY,
                              adapter=LEARNING_ADAPTER, authority=InMemorySessionBindingAuthority())
    created = service.create_session(learning_request(), contract=contract(), grant=grant(),
                                     classification=DataClassification.INTERNAL)
    assert created.result is OperationResult.RECORDED
    return service, store


INCOMPLETE_STATE = {
    "partial": (_outcome(session=_session(90), partial=True), FailureCode.PARTIAL_RESULT),
    "degraded_without_failure": (_outcome(session=_session(90), status=ProviderStatus.DEGRADED),
                                 FailureCode.MALFORMED_RESPONSE),
    "unavailable_without_failure": (_outcome(session=_session(90), status=ProviderStatus.UNAVAILABLE),
                                    FailureCode.MALFORMED_RESPONSE),
    "state_without_session": (_outcome(session=None), FailureCode.MALFORMED_RESPONSE),
}


@pytest.mark.parametrize("name", sorted(INCOMPLETE_STATE))
def test_incomplete_state_followup_is_not_accepted(tmp_path: Path, name: str) -> None:
    outcome, expected = INCOMPLETE_STATE[name]
    service, store = _created(tmp_path, outcome)
    before = store.progress()
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is expected
    assert receipt.accepted_into_loop is False
    assert receipt.evidence_digests == ()
    assert store.progress() == before  # the advisory projection is untouched


INCOMPLETE_ASSESSMENT = {
    "assessment_missing": (_outcome(assessment=None), FailureCode.MALFORMED_RESPONSE),
    "assessment_partial": (_outcome(assessment=_assessment(), partial=True), FailureCode.PARTIAL_RESULT),
    "assessment_degraded": (_outcome(assessment=_assessment(), status=ProviderStatus.DEGRADED),
                            FailureCode.MALFORMED_RESPONSE),
}


@pytest.mark.parametrize("name", sorted(INCOMPLETE_ASSESSMENT))
def test_incomplete_assessment_followup_is_not_accepted(tmp_path: Path, name: str) -> None:
    outcome, expected = INCOMPLETE_ASSESSMENT[name]
    service, store = _created(tmp_path, outcome)
    before = store.progress()
    receipt = service.assessment(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is expected
    assert receipt.accepted_into_loop is False
    assert receipt.evidence_digests == ()
    assert store.progress() == before


def test_explicit_provider_failure_code_is_preserved(tmp_path: Path) -> None:
    service, _ = _created(tmp_path, _outcome(session=None, status=ProviderStatus.DEGRADED,
                                             failure=FailureCode.TIMEOUT))
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.TIMEOUT


def test_whole_success_followups_are_recorded(tmp_path: Path) -> None:
    service, store = _created(tmp_path, _outcome(session=_session(55), assessment=_assessment()))
    state = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert state.result is OperationResult.RECORDED and state.accepted_into_loop is True
    assert store.progress()[SESSION]["progress_percent"] == 55
    assessed = service.assessment(session_ref=SESSION, contract=contract(), grant=grant())
    assert assessed.result is OperationResult.RECORDED
    assert assessed.evidence_digests == ("e" * 64,)


def test_assessment_only_answer_without_session_is_recorded(tmp_path: Path) -> None:
    service, store = _created(tmp_path, _outcome(session=None, assessment=_assessment()))
    before = store.progress()
    receipt = service.assessment(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED
    assert receipt.evidence_digests == ("e" * 64,)
    assert store.progress() == before  # no session in the answer: nothing to project


def test_partial_creation_is_recorded_as_partial_result(tmp_path: Path) -> None:
    class _PartialCreate(_Scripted):
        def create_session(self, request: Any) -> LearningOutcome:
            return _outcome(partial=True)

    authority = InMemorySessionBindingAuthority()
    service = LearningService(_PartialCreate(_outcome()), LoopStore(tmp_path / "loop.json"),
                              capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER, authority=authority)
    receipt = service.create_session(learning_request(), contract=contract(), grant=grant(),
                                     classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.PARTIAL_RESULT
    assert authority.resolve(PROVIDER, SESSION) is None
