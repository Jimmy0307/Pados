# SPDX-License-Identifier: AGPL-3.0-only
"""Learning service: gate -> learning provider -> normalized session/assessment -> chained receipt.

The P7.2 vertical slice. It authorizes through the learning gate, calls the provider only when allowed,
records one ``OperationReceipt`` per operation (create session, poll state, assessment), and stores the
normalized session / assessment projection. It never promotes a capability and never writes PADOS semantic
state; OpenMAIC's own model choice is recorded as an external boundary, not as PADOS routing.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pados.capability.model import AdapterDescriptor, CapabilityDescriptor
from pados.capability_loop.gate import GateCode, GateDecision, evaluate_learning
from pados.capability_loop.learning_contracts import (
    LearningOutcome,
    LearningProvider,
    LearningRequest,
)
from pados.capability_loop.receipts import OperationKind, OperationReceipt, OperationResult
from pados.capability_loop.store import LoopStore
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash


class LearningService:
    def __init__(self, provider: LearningProvider, store: LoopStore, *,
                 capability: CapabilityDescriptor, adapter: AdapterDescriptor,
                 network_policy: NetworkRequirement = NetworkRequirement.LOCAL_ONLY) -> None:
        self._provider = provider
        self._store = store
        self._capability = capability
        self._adapter = adapter
        self._network_policy = network_policy

    def create_session(self, request: LearningRequest, *, contract: Mapping[str, Any],
                       grant: Mapping[str, Any], classification: DataClassification,
                       registered_mcp_capabilities: frozenset[str] = frozenset()) -> OperationReceipt:
        decision = evaluate_learning(
            project_id=request.project_id, work_contract_id=request.work_contract_id,
            request_id=request.request_id, classification=classification, contract=contract, grant=grant,
            capability=self._capability, adapter=self._adapter, network_policy=self._network_policy,
            adapter_available=lambda: True, registered_mcp_capabilities=registered_mcp_capabilities)
        request_digest = canonical_hash(request.model_dump(mode="json"))
        if not decision.allowed:
            return self._refusal(OperationKind.LEARN_CREATE_SESSION, request.project_id,
                                 request.capability_gap_ref, request_digest, decision)
        outcome = self._provider.create_session(request)
        return self._record(OperationKind.LEARN_CREATE_SESSION, request.project_id,
                            request.capability_gap_ref, request_digest, decision, outcome)

    def session_state(self, *, project_id: str, capability_gap_ref: str, session_ref: str) -> OperationReceipt:
        request_digest = canonical_hash({"op": "state", "session": session_ref, "project": project_id})
        outcome = self._provider.session_state(session_ref)
        return self._record(OperationKind.LEARN_SESSION_STATE, project_id, capability_gap_ref,
                            request_digest, None, outcome)

    def assessment(self, *, project_id: str, capability_gap_ref: str, session_ref: str) -> OperationReceipt:
        request_digest = canonical_hash({"op": "assessment", "session": session_ref, "gap": capability_gap_ref})
        outcome = self._provider.assessment(session_ref, capability_gap_ref)
        return self._record(OperationKind.LEARN_ASSESSMENT, project_id, capability_gap_ref,
                            request_digest, None, outcome)

    # -- recording ----------------------------------------------------------
    def _refusal(self, kind: OperationKind, project_id: str, capability_gap_ref: str,
                 request_digest: str, decision: GateDecision) -> OperationReceipt:
        return self._store.append({
            "operation": kind, "project_id": project_id, "request_digest": request_digest,
            "provider_id": self._provider.provider_id, "capability_id": _gap_capability(capability_gap_ref),
            "policy_code": decision.code, "policy_decision_hash": decision.policy_decision_hash,
            "result": OperationResult.REFUSED_BY_POLICY, "failure": None, "evidence_digests": (),
            "limitations": (), "accepted_into_loop": False, "state_before": None, "state_after": None})

    def _record(self, kind: OperationKind, project_id: str, capability_gap_ref: str, request_digest: str,
                decision: GateDecision | None, outcome: LearningOutcome) -> OperationReceipt:
        failed = outcome.failure is not None
        limitations: tuple[str, ...] = ()
        progress: tuple[str, dict[str, Any]] | None = None
        if outcome.session is not None:
            limitations = outcome.session.limitations
        if outcome.assessment is not None:
            limitations = tuple(dict.fromkeys(limitations + outcome.assessment.limitations))[:16]
        if not failed and outcome.session is not None:
            progress = (outcome.session.session_ref, _progress_row(project_id, capability_gap_ref, outcome))
        evidence_digests = (outcome.assessment.evidence_digest,) if outcome.assessment is not None else ()
        return self._store.append({
            "operation": kind, "project_id": project_id, "request_digest": request_digest,
            "provider_id": self._provider.provider_id, "capability_id": _gap_capability(capability_gap_ref),
            "policy_code": decision.code if decision is not None else GateCode.ALLOWED,
            "policy_decision_hash": decision.policy_decision_hash if decision is not None else None,
            "result": OperationResult.PROVIDER_FAILED if failed else OperationResult.RECORDED,
            "failure": outcome.failure, "evidence_digests": evidence_digests, "limitations": limitations,
            "accepted_into_loop": not failed, "state_before": None, "state_after": None},
            progress=None if failed else progress)


def _gap_capability(capability_gap_ref: str) -> str:
    """A gap ref like ``gap:cad_edit`` maps to a capability-id-shaped code for the receipt."""
    tail = capability_gap_ref.split(":")[-1].split(".")[-1]
    code = "".join(ch if ch.isalnum() else "_" for ch in tail).upper().strip("_")
    if not code or not code[0].isalpha():
        code = f"GAP_{code}" if code else "GAP"
    return code[:63] if len(code) >= 3 else f"{code}_GAP"


def _progress_row(project_id: str, capability_gap_ref: str, outcome: LearningOutcome) -> dict[str, Any]:
    session = outcome.session
    assert session is not None
    row: dict[str, Any] = {
        "project_id": project_id, "capability_gap_ref": capability_gap_ref,
        "provider_id": outcome.provider_id, "session_status": session.status.value,
        "model_routing": session.model_routing.value, "progress_percent": session.progress_percent}
    if outcome.assessment is not None:
        row["human_mastery"] = outcome.assessment.human_mastery.value
        row["grants_capability"] = outcome.assessment.grants_capability
    return row
