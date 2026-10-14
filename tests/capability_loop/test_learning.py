# SPDX-License-Identifier: AGPL-3.0-only
"""P7.2 vertical slice: gate -> OpenMAIC adapter -> normalized session/assessment -> chained receipt."""
from __future__ import annotations

from pathlib import Path

from loop_learning_support import (
    ENDPOINT,
    HEALTH_OK,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    FakeHttp,
    binding_authority,
    contract,
    generate_ok,
    grant,
    learning_request,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.contracts import FailureCode, ProviderStatus
from pados.capability_loop.gate import GateCode
from pados.capability_loop.learning import LearningService
from pados.capability_loop.learning_contracts import (
    LearningSessionStatus,
    MasteryLevel,
    ModelRouting,
)
from pados.capability_loop.receipts import OperationResult, verify_chain
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification


def _provider(http: FakeHttp, *, endpoint: str | None = ENDPOINT) -> OpenMaicLearningProvider:
    return OpenMaicLearningProvider(endpoint, http)


def _service(store: LoopStore, provider: OpenMaicLearningProvider) -> LearningService:
    return LearningService(provider, store, capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER, authority=binding_authority())


def test_create_session_records_external_routing_and_chained_receipt(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(step="researching", progress=20)})
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, _provider(http)).create_session(
        learning_request(), contract=contract(), grant=grant(), classification=DataClassification.INTERNAL)
    assert receipt.policy_code == GateCode.ALLOWED and receipt.result is OperationResult.RECORDED
    assert receipt.accepted_into_loop and receipt.semantic_authority == "NONE"
    progress = store.progress()
    assert "job-123" in progress
    assert progress["job-123"]["model_routing"] == ModelRouting.EXTERNAL_PROVIDER_CONTROLLED.value
    assert progress["job-123"]["session_status"] == LearningSessionStatus.IN_PROGRESS.value
    assert verify_chain(store.receipts())


def test_missing_grant_refused_before_provider(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok()})
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, _provider(http)).create_session(
        learning_request(), contract=contract(), grant=grant(capabilities=[]),
        classification=DataClassification.INTERNAL)
    assert receipt.policy_code == GateCode.GRANT_REFUSED
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert ("POST", "/api/generate-classroom") not in http.calls


def test_classification_above_capability_ceiling_refused(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok()})
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, _provider(http)).create_session(
        learning_request(), contract=contract(), grant=grant(),
        classification=DataClassification.RESTRICTED)
    assert receipt.policy_code == "CLASSIFICATION_EXCEEDS_CAPABILITY"
    assert ("POST", "/api/generate-classroom") not in http.calls


def test_non_loopback_endpoint_fails_closed(tmp_path: Path) -> None:
    http = FakeHttp({})
    provider = OpenMaicLearningProvider("http://example.com:3000", http)
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).create_session(
        learning_request(), contract=contract(), grant=grant(), classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.UNAUTHORIZED
    assert http.calls == []


def test_provider_not_configured_is_isolated(tmp_path: Path) -> None:
    provider = OpenMaicLearningProvider(None, FakeHttp({}))
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).create_session(
        learning_request(), contract=contract(), grant=grant(), classification=DataClassification.INTERNAL)
    assert receipt.failure is FailureCode.PROVIDER_NOT_CONFIGURED
    assert not receipt.accepted_into_loop
    assert verify_chain(store.receipts())


def test_connection_error_is_normalized(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK}, raise_on="generate-classroom")
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, _provider(http)).create_session(
        learning_request(), contract=contract(), grant=grant(), classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.MALFORMED_RESPONSE


def test_assessment_reports_unassessed_with_external_limitation(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(step="researching", progress=20),
                     ("GET", "/api/generate-classroom/job-123"): (200, {"jobId": "job-123", "step": "completed",
                                                                        "progress": 100})})
    store = LoopStore(tmp_path / "loop.json")
    service = _service(store, _provider(http))
    # The session (and its governed binding) must exist before a follow-up assessment is authorized.
    service.create_session(learning_request(), contract=contract(), grant=grant(),
                           classification=DataClassification.INTERNAL)
    receipt = service.assessment(session_ref="job-123", contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED
    assert "OPENMAIC_PROGRESS_API_EXTERNAL" in receipt.limitations
    progress = store.progress()
    assert progress["job-123"]["human_mastery"] == MasteryLevel.UNASSESSED.value
    assert progress["job-123"]["grants_capability"] is False


def test_health_available_on_loopback_ok(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK})
    health = _provider(http).health()
    assert health.status is ProviderStatus.AVAILABLE and health.runtime_version == "1.2.0"


def test_health_unavailable_without_configuration() -> None:
    health = OpenMaicLearningProvider(None, FakeHttp({})).health()
    assert health.status is ProviderStatus.UNAVAILABLE
