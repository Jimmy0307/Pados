# SPDX-License-Identifier: AGPL-3.0-only
"""P7.1 vertical slice: gate -> REA adapter -> candidate -> chained receipt, and fail-closed isolation."""
from __future__ import annotations

from pathlib import Path

from loop_support import (
    CAPABILITY_ID,
    DISCOVERY_ADAPTER,
    DISCOVERY_CAPABILITY,
    PROJECT,
    WORK,
    FakeTransport,
    contract,
    grant,
    ok_results,
    session_document,
    target,
)

from pados.capability_loop.adapters.rea_mcp import ReaDiscoveryProvider
from pados.capability_loop.contracts import CapabilityState, DiscoveryRequest, FailureCode, ProviderStatus
from pados.capability_loop.discovery import DiscoveryService
from pados.capability_loop.gate import GateCode
from pados.capability_loop.receipts import OperationResult, verify_chain
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification


def _request(**overrides: object) -> DiscoveryRequest:
    values = dict(request_id="req-1", project_id=PROJECT, work_contract_id=WORK, goal_ref="goal-1",
                  required_capability=CAPABILITY_ID, target_ref="target:app",
                  classification=DataClassification.INTERNAL)
    values.update(overrides)
    return DiscoveryRequest(**values)  # type: ignore[arg-type]


def _service(store: LoopStore, provider: ReaDiscoveryProvider) -> DiscoveryService:
    return DiscoveryService(provider, store, capability=DISCOVERY_CAPABILITY, adapter=DISCOVERY_ADAPTER)


def test_authorized_discovery_records_candidate_and_chained_receipt(tmp_path: Path) -> None:
    transport = FakeTransport(ok_results())
    provider = ReaDiscoveryProvider(lambda: transport)
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).discover(
        _request(), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.ALLOWED
    assert receipt.result is OperationResult.RECORDED
    assert receipt.accepted_into_loop and receipt.state_after is CapabilityState.DISCOVERED
    assert receipt.semantic_authority == "NONE"
    assert receipt.evidence_digests and len(receipt.evidence_digests[0]) == 64
    candidates = store.candidates()
    assert len(candidates) == 1
    candidate = next(iter(candidates.values()))
    assert candidate.state is CapabilityState.DISCOVERED and candidate.auto_promote is False
    assert candidate.evidence[0].untrusted_content is True
    assert verify_chain(store.receipts())
    assert transport.closed


def test_unregistered_target_is_refused_before_the_provider_is_called(tmp_path: Path) -> None:
    transport = FakeTransport(ok_results())
    provider = ReaDiscoveryProvider(lambda: transport)
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).discover(
        _request(), target=None, contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.TARGET_NOT_REGISTERED
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert not receipt.accepted_into_loop and not store.candidates()
    assert transport.calls == []  # provider never contacted


def test_classification_above_target_ceiling_is_refused(tmp_path: Path) -> None:
    transport = FakeTransport(ok_results())
    provider = ReaDiscoveryProvider(lambda: transport)
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).discover(
        _request(classification=DataClassification.RESTRICTED),
        target=target(tmp_path, classification=DataClassification.CONFIDENTIAL),
        contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.CLASSIFICATION_EXCEEDS_TARGET
    assert transport.calls == []


def test_missing_grant_is_refused(tmp_path: Path) -> None:
    transport = FakeTransport(ok_results())
    provider = ReaDiscoveryProvider(lambda: transport)
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).discover(
        _request(), target=target(tmp_path), contract=contract(),
        grant=grant(capabilities=[]))
    assert receipt.policy_code == GateCode.GRANT_REFUSED
    assert transport.calls == []


def test_credential_named_target_is_refused(tmp_path: Path) -> None:
    transport = FakeTransport(ok_results())
    provider = ReaDiscoveryProvider(lambda: transport)
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).discover(
        _request(), target=target(tmp_path, name="id_rsa"), contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.CREDENTIAL_EXPOSURE
    assert transport.calls == []


def test_provider_absent_is_isolated_as_a_normalized_failure(tmp_path: Path) -> None:
    provider = ReaDiscoveryProvider(None)  # REA not configured
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).discover(
        _request(), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.PROVIDER_NOT_CONFIGURED
    assert not receipt.accepted_into_loop and receipt.state_after is None
    assert not store.candidates()
    assert verify_chain(store.receipts())


def test_provider_timeout_is_isolated(tmp_path: Path) -> None:
    transport = FakeTransport(ok_results(), raise_on="binary_session", error_code="MCP_TIMEOUT")
    provider = ReaDiscoveryProvider(lambda: transport)
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).discover(
        _request(), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.TIMEOUT
    assert transport.closed


def test_capability_not_supported_blocks(tmp_path: Path) -> None:
    doc = session_document(available=False, reason="provider_missing")
    transport = FakeTransport(ok_results(doc))
    provider = ReaDiscoveryProvider(lambda: transport)
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, provider).discover(
        _request(), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.state_after is CapabilityState.BLOCKED
    candidate = next(iter(store.candidates().values()))
    assert candidate.state is CapabilityState.BLOCKED
    assert "REA_PROVIDER_MISSING" in candidate.limitations


def test_health_reports_unavailable_without_configuration(tmp_path: Path) -> None:
    provider = ReaDiscoveryProvider(None)
    health = provider.health()
    assert health.status is ProviderStatus.UNAVAILABLE
    assert health.code == FailureCode.PROVIDER_NOT_CONFIGURED.value


def test_two_discoveries_chain(tmp_path: Path) -> None:
    provider = ReaDiscoveryProvider(lambda: FakeTransport(ok_results()))
    store = LoopStore(tmp_path / "loop.json")
    service = _service(store, provider)
    service.discover(_request(), target=target(tmp_path), contract=contract(), grant=grant())
    service.discover(_request(request_id="req-2"), target=target(tmp_path), contract=contract(), grant=grant())
    receipts = store.receipts()
    assert len(receipts) == 2 and verify_chain(receipts)
