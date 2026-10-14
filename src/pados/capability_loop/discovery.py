# SPDX-License-Identifier: AGPL-3.0-only
"""Discovery service: gate -> provider -> normalized evidence -> candidate -> receipt.

This is the P7.1 vertical slice. It authorizes through the PADOS discovery gate, calls the provider only
when allowed, turns the normalized outcome into an inert ``CapabilityCandidate``, and records one
``OperationReceipt`` for every attempt (including policy refusals and provider failures). It never
promotes a candidate: a discovered capability lands in DISCOVERED, VALIDATION_REQUIRED or BLOCKED, and
``VALIDATED`` is unreachable here.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pados.capability.model import AdapterDescriptor, CapabilityDescriptor
from pados.capability_loop.contracts import (
    CapabilityCandidate,
    CapabilityDiscoveryProvider,
    CapabilityState,
    DiscoveryOutcome,
    DiscoveryRequest,
)
from pados.capability_loop.gate import GateCode, GateDecision, TargetRegistration, evaluate_discovery
from pados.capability_loop.receipts import OperationKind, OperationReceipt, OperationResult
from pados.capability_loop.store import LoopStore
from pados.connectivity.requirements import NetworkRequirement
from pados.execution.context import canonical_hash


def _candidate_state(outcome: DiscoveryOutcome) -> tuple[CapabilityState, str]:
    if outcome.failure is not None:
        return CapabilityState.BLOCKED, "PROVIDER_FAILED"
    if not outcome.capability_supported:
        return CapabilityState.BLOCKED, "CAPABILITY_NOT_SUPPORTED"
    if outcome.limitations:
        return CapabilityState.VALIDATION_REQUIRED, "DISCOVERED_WITH_LIMITATIONS"
    return CapabilityState.DISCOVERED, "DISCOVERED"


class DiscoveryService:
    def __init__(self, provider: CapabilityDiscoveryProvider, store: LoopStore, *,
                 capability: CapabilityDescriptor, adapter: AdapterDescriptor,
                 network_policy: NetworkRequirement = NetworkRequirement.LOCAL_ONLY,
                 candidate_id: Callable[[DiscoveryRequest], str] | None = None) -> None:
        self._provider = provider
        self._store = store
        self._capability = capability
        self._adapter = adapter
        self._network_policy = network_policy
        self._candidate_id = candidate_id or self._default_candidate_id

    @staticmethod
    def _default_candidate_id(request: DiscoveryRequest) -> str:
        digest = canonical_hash({"capability": request.required_capability, "target": request.target_ref,
                                 "project": request.project_id})
        return f"candidate:{digest[:24]}"

    def discover(self, request: DiscoveryRequest, *, target: TargetRegistration | None,
                 contract: Mapping[str, Any], grant: Mapping[str, Any],
                 registered_mcp_capabilities: frozenset[str] = frozenset()) -> OperationReceipt:
        # Authorization is independent of liveness: the gate authorizes, and the provider then returns a
        # normalized failure if the runtime is absent, unhealthy or incompatible (Core stays available).
        decision = evaluate_discovery(
            request, target=target, contract=contract, grant=grant, capability=self._capability,
            adapter=self._adapter, network_policy=self._network_policy,
            adapter_available=lambda: True,
            registered_mcp_capabilities=registered_mcp_capabilities)
        request_digest = canonical_hash(request.model_dump(mode="json"))
        if not decision.allowed:
            return self._record_refusal(request, decision, request_digest)
        outcome = self._provider.discover(request, target_path=decision.target_path or "")
        return self._record_outcome(request, decision, request_digest, outcome)

    def _record_refusal(self, request: DiscoveryRequest, decision: GateDecision,
                        request_digest: str) -> OperationReceipt:
        return self._store.append({
            "operation": OperationKind.DISCOVER, "project_id": request.project_id,
            "request_digest": request_digest, "provider_id": self._provider.provider_id,
            "capability_id": request.required_capability, "policy_code": decision.code,
            "policy_decision_hash": decision.policy_decision_hash, "result": OperationResult.REFUSED_BY_POLICY,
            "failure": None, "evidence_digests": (), "limitations": (), "accepted_into_loop": False,
            "state_before": None, "state_after": None})

    def _record_outcome(self, request: DiscoveryRequest, decision: GateDecision, request_digest: str,
                        outcome: DiscoveryOutcome) -> OperationReceipt:
        state, reason = _candidate_state(outcome)
        evidence_digests = tuple(item.evidence_digest for item in outcome.evidence)
        candidate = CapabilityCandidate(
            candidate_id=self._candidate_id(request), required_capability=request.required_capability,
            provider_id=self._provider.provider_id, state=state, reason_code=reason,
            evidence=outcome.evidence, limitations=outcome.limitations)
        provider_failed = outcome.failure is not None
        return self._store.append({
            "operation": OperationKind.DISCOVER, "project_id": request.project_id,
            "request_digest": request_digest, "provider_id": self._provider.provider_id,
            "capability_id": request.required_capability, "runtime_version": outcome.runtime_version,
            "policy_code": GateCode.ALLOWED, "policy_decision_hash": decision.policy_decision_hash,
            "result": OperationResult.PROVIDER_FAILED if provider_failed else OperationResult.RECORDED,
            "failure": outcome.failure, "evidence_digests": evidence_digests,
            "limitations": outcome.limitations, "accepted_into_loop": not provider_failed,
            "state_before": None, "state_after": None if provider_failed else state},
            candidate=None if provider_failed else candidate)
