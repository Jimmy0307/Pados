# SPDX-License-Identifier: AGPL-3.0-only
"""Findings 1 & 5: the gate must bind the request's target ref and capability id to what it authorizes.

A caller must not be able to name one target (``request.target_ref``) while the operator registration the
gate inspects, and the resulting receipt/candidate describe, is a different target; nor claim one capability
id in the request while the gate authorizes another. Both mismatches fail closed before the provider is
called and before any candidate is recorded.
"""
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
    target,
)

from pados.capability_loop.adapters.rea_mcp import ReaDiscoveryProvider
from pados.capability_loop.contracts import DiscoveryRequest
from pados.capability_loop.discovery import DiscoveryService
from pados.capability_loop.gate import GateCode
from pados.capability_loop.receipts import OperationResult
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


# -- Finding 1: target reference binding -----------------------------------
def test_matching_target_ref_is_allowed(tmp_path: Path) -> None:
    transport = FakeTransport(ok_results())
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, ReaDiscoveryProvider(lambda: transport)).discover(
        _request(target_ref="target:app"), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.ALLOWED


def test_mismatched_target_ref_is_refused_before_provider(tmp_path: Path) -> None:
    # The registration the gate was handed describes a different target than the request names.
    transport = FakeTransport(ok_results())
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, ReaDiscoveryProvider(lambda: transport)).discover(
        _request(target_ref="target:other"), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.TARGET_REFERENCE_MISMATCH
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert transport.calls == []  # provider never called on a target mismatch
    assert not store.candidates()  # no candidate may claim a different target


# -- Finding 5: capability identity binding --------------------------------
def test_matching_capability_id_is_allowed(tmp_path: Path) -> None:
    transport = FakeTransport(ok_results())
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, ReaDiscoveryProvider(lambda: transport)).discover(
        _request(required_capability=CAPABILITY_ID), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.ALLOWED


def test_forged_capability_id_is_refused_before_provider(tmp_path: Path) -> None:
    # The request claims OCR_TEXT, but the gate authorizes the BINARY_INSPECTION descriptor/grant.
    transport = FakeTransport(ok_results())
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, ReaDiscoveryProvider(lambda: transport)).discover(
        _request(required_capability="OCR_TEXT"), target=target(tmp_path), contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.CAPABILITY_IDENTITY_MISMATCH
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert transport.calls == []  # provider never called
    assert not store.candidates()  # no candidate may use a caller-forged capability id
