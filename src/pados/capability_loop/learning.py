# SPDX-License-Identifier: AGPL-3.0-only
"""Learning service: gate -> learning provider -> normalized session/assessment -> chained receipt.

The P7.2 vertical slice. Every operation -- create session, poll state, assessment -- is authorized by the
PADOS learning gate before the provider is called, records one hash-chained ``OperationReceipt``, and stores
the normalized session / assessment projection. It never promotes a capability and never writes PADOS
semantic state; OpenMAIC's own model choice is recorded as an external boundary, not as PADOS routing.

Authorization is never delegated to the external session. At creation, the governed binding (session,
project, work contract, capability gap, classification, provider) is recorded, and its digest is anchored in
the hash-chained creation receipt. The mutable progress row is only an advisory projection: a follow-up
reconstructs the binding from it, then proves the binding is authentic by matching its digest to a creation
receipt in the (chain-verified) receipt log -- so a mutated row fails closed even with a valid current grant.
A follow-up then verifies the supplied contract is for the same project/work, re-runs the authoritative gate
against the *current* grant, and only then calls the provider. The provider must answer about the session
that was asked for: a response naming a different session_ref is rejected before any binding update. All of
this stays inside the capability_loop boundary; no Core model, receipt or authority is changed.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pados.capability.model import AdapterDescriptor, CapabilityDescriptor
from pados.capability_loop.contracts import FailureCode
from pados.capability_loop.gate import GateCode, GateDecision, evaluate_learning
from pados.capability_loop.learning_contracts import (
    LearningOutcome,
    LearningProvider,
    LearningRequest,
)
from pados.capability_loop.materials import (
    MaterialRegistration,
    MaterialResolutionCode,
    material_policy_code,
    resolve_materials,
)
from pados.capability_loop.receipts import (
    OperationKind,
    OperationReceipt,
    OperationResult,
    is_valid_receipt_id,
)
from pados.capability_loop.store import LoopStore
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification, parse_classification
from pados.execution.context import canonical_hash

_UNRESOLVED_CAPABILITY = "UNRESOLVED"  # receipt capability id when no session binding could be resolved
_SAFE_PROJECT_PLACEHOLDER = "unresolved-project"  # bounded, receipt-valid stand-in for a malformed project id


@dataclass(frozen=True)
class _SessionBinding:
    """The governed context recorded when a session is created; the authorization basis for follow-ups.

    Its ``digest`` is anchored in the hash-chained creation receipt, making the binding tamper-evident.
    """

    session_ref: str
    project_id: str
    work_contract_id: str
    capability_gap_ref: str
    classification: DataClassification
    provider_id: str

    @property
    def digest(self) -> str:
        return canonical_hash({
            "session_ref": self.session_ref, "project_id": self.project_id,
            "work_contract_id": self.work_contract_id, "capability_gap_ref": self.capability_gap_ref,
            "classification": self.classification.value, "provider_id": self.provider_id})


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
                       material_registrations: Mapping[str, MaterialRegistration] | None = None,
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
        # Bind every material to this project before the provider is called; fail closed on any mismatch.
        resolution = resolve_materials(request, material_registrations or {})
        if resolution.code is not MaterialResolutionCode.RESOLVED:
            return self._coded_refusal(OperationKind.LEARN_CREATE_SESSION, request.project_id,
                                       request.capability_gap_ref, request_digest,
                                       material_policy_code(resolution.code), decision.policy_decision_hash)
        outcome = self._provider.create_session(request)
        if outcome.failure is not None or outcome.session is None:
            return self._creation_failure(request, request_digest, decision, outcome)
        binding = _SessionBinding(outcome.session.session_ref, request.project_id, request.work_contract_id,
                                  request.capability_gap_ref, classification, self._provider.provider_id)
        return self._record(OperationKind.LEARN_CREATE_SESSION, binding, request_digest, decision, outcome)

    def session_state(self, *, session_ref: str, contract: Mapping[str, Any], grant: Mapping[str, Any],
                      registered_mcp_capabilities: frozenset[str] = frozenset()) -> OperationReceipt:
        return self._followup(OperationKind.LEARN_SESSION_STATE, session_ref, contract, grant,
                              registered_mcp_capabilities, lambda _b: self._provider.session_state(session_ref))

    def assessment(self, *, session_ref: str, contract: Mapping[str, Any], grant: Mapping[str, Any],
                   registered_mcp_capabilities: frozenset[str] = frozenset()) -> OperationReceipt:
        return self._followup(
            OperationKind.LEARN_ASSESSMENT, session_ref, contract, grant, registered_mcp_capabilities,
            lambda binding: self._provider.assessment(session_ref, binding.capability_gap_ref))

    # -- follow-up authorization --------------------------------------------
    def _followup(self, kind: OperationKind, session_ref: str, contract: Mapping[str, Any],
                  grant: Mapping[str, Any], registered_mcp_capabilities: frozenset[str],
                  call: Callable[[_SessionBinding], LearningOutcome]) -> OperationReceipt:
        request_digest = canonical_hash(
            {"op": kind.value, "session": session_ref, "project": contract.get("project_id")})
        binding, bind_code = self._resolve_binding(session_ref, contract)
        if bind_code is not None:
            project = _safe_project_id(binding.project_id if binding is not None else contract.get("project_id"))
            capability = _gap_capability(binding.capability_gap_ref) if binding is not None \
                else _UNRESOLVED_CAPABILITY
            return self._coded_refusal(kind, project, None, request_digest, bind_code, None,
                                       capability_override=capability)
        assert binding is not None
        decision = evaluate_learning(
            project_id=binding.project_id, work_contract_id=binding.work_contract_id,
            request_id=f"{kind.value.lower()}:{session_ref}", classification=binding.classification,
            contract=contract, grant=grant, capability=self._capability, adapter=self._adapter,
            network_policy=self._network_policy, adapter_available=lambda: True,
            registered_mcp_capabilities=registered_mcp_capabilities)
        if not decision.allowed:
            return self._refusal(kind, binding.project_id, binding.capability_gap_ref, request_digest, decision)
        outcome = call(binding)
        # The provider must answer about the session we asked for; a foreign session_ref never inherits this
        # binding's authorization. Reject before any store/binding update.
        if outcome.session is not None and outcome.session.session_ref != binding.session_ref:
            return self._record(kind, binding, request_digest, decision, outcome,
                                forced_failure=FailureCode.MALFORMED_RESPONSE)
        return self._record(kind, binding, request_digest, decision, outcome)

    def _resolve_binding(self, session_ref: str,
                         contract: Mapping[str, Any]) -> tuple[_SessionBinding | None, str | None]:
        row = self._store.progress().get(session_ref)
        if row is None:
            return None, GateCode.SESSION_NOT_BOUND
        try:
            binding = _SessionBinding(
                session_ref=session_ref,
                project_id=str(row["project_id"]), work_contract_id=str(row["work_contract_id"]),
                capability_gap_ref=str(row["capability_gap_ref"]),
                classification=parse_classification(row["classification"]),
                provider_id=str(row["provider_id"]))
        except (KeyError, ValueError):
            return None, GateCode.SESSION_NOT_BOUND  # a row without a complete governed binding is not usable
        # Tamper-evidence: the advisory row is authoritative only if its binding digest is anchored by a
        # creation receipt in the hash-chained log. A mutated row no longer matches, even with a valid grant.
        if binding.digest not in self._creation_binding_digests():
            return binding, GateCode.SESSION_BINDING_TAMPERED
        if binding.provider_id != self._provider.provider_id:
            return binding, GateCode.SESSION_PROVIDER_MISMATCH
        if (contract.get("project_id") != binding.project_id
                or contract.get("work_contract_id") != binding.work_contract_id):
            return binding, GateCode.SESSION_PROJECT_MISMATCH
        return binding, None

    def _creation_binding_digests(self) -> set[str]:
        """Binding digests anchored by creation receipts in the (chain-verified) receipt log."""
        return {receipt.binding_digest for receipt in self._store.receipts()
                if receipt.operation is OperationKind.LEARN_CREATE_SESSION and receipt.binding_digest is not None}

    # -- recording ----------------------------------------------------------
    def _refusal(self, kind: OperationKind, project_id: str, capability_gap_ref: str,
                 request_digest: str, decision: GateDecision) -> OperationReceipt:
        return self._coded_refusal(kind, _safe_project_id(project_id), capability_gap_ref, request_digest,
                                   decision.code, decision.policy_decision_hash)

    def _coded_refusal(self, kind: OperationKind, project_id: str | None, capability_gap_ref: str | None,
                       request_digest: str, policy_code: str, policy_decision_hash: str | None, *,
                       capability_override: str | None = None) -> OperationReceipt:
        capability_id = capability_override or (
            _gap_capability(capability_gap_ref) if capability_gap_ref is not None else _UNRESOLVED_CAPABILITY)
        return self._store.append({
            "operation": kind, "project_id": _safe_project_id(project_id), "request_digest": request_digest,
            "provider_id": self._provider.provider_id, "capability_id": capability_id,
            "policy_code": policy_code, "policy_decision_hash": policy_decision_hash,
            "result": OperationResult.REFUSED_BY_POLICY, "failure": None, "evidence_digests": (),
            "limitations": (), "accepted_into_loop": False, "state_before": None, "state_after": None})

    def _creation_failure(self, request: LearningRequest, request_digest: str, decision: GateDecision,
                          outcome: LearningOutcome) -> OperationReceipt:
        """The provider did not create a session: record a normalized failure, no binding, no progress."""
        return self._store.append({
            "operation": OperationKind.LEARN_CREATE_SESSION, "project_id": request.project_id,
            "request_digest": request_digest, "provider_id": self._provider.provider_id,
            "capability_id": _gap_capability(request.capability_gap_ref), "policy_code": decision.code,
            "policy_decision_hash": decision.policy_decision_hash,
            "result": OperationResult.PROVIDER_FAILED, "failure": outcome.failure, "evidence_digests": (),
            "limitations": (), "accepted_into_loop": False, "state_before": None, "state_after": None})

    def _record(self, kind: OperationKind, binding: _SessionBinding, request_digest: str,
                decision: GateDecision, outcome: LearningOutcome, *,
                forced_failure: FailureCode | None = None) -> OperationReceipt:
        failure = forced_failure if forced_failure is not None else outcome.failure
        failed = failure is not None
        limitations: tuple[str, ...] = ()
        if outcome.session is not None:
            limitations = outcome.session.limitations
        if outcome.assessment is not None:
            limitations = tuple(dict.fromkeys(limitations + outcome.assessment.limitations))[:16]
        progress: tuple[str, dict[str, Any]] | None = None
        if not failed and outcome.session is not None:
            # The progress row is keyed by the BOUND session_ref (never a provider-chosen foreign ref).
            progress = (binding.session_ref, _progress_row(binding, outcome))
        evidence_digests = (outcome.assessment.evidence_digest,) if (
            not failed and outcome.assessment is not None) else ()
        return self._store.append({
            "operation": kind, "project_id": binding.project_id, "request_digest": request_digest,
            "provider_id": self._provider.provider_id,
            "capability_id": _gap_capability(binding.capability_gap_ref),
            "policy_code": decision.code, "policy_decision_hash": decision.policy_decision_hash,
            "binding_digest": binding.digest,
            "result": OperationResult.PROVIDER_FAILED if failed else OperationResult.RECORDED,
            "failure": failure, "evidence_digests": evidence_digests, "limitations": limitations,
            "accepted_into_loop": not failed, "state_before": None, "state_after": None},
            progress=None if failed else progress)


def _safe_project_id(candidate: object) -> str:
    """The caller/row project id if it is a valid receipt identifier, else a bounded safe placeholder.

    A guessed-session request (or a tampered row) may carry a malformed project id; it must never reach
    OperationReceipt validation and raise -- the refusal receipt uses the placeholder instead.
    """
    return str(candidate) if is_valid_receipt_id(candidate) else _SAFE_PROJECT_PLACEHOLDER


def _gap_capability(capability_gap_ref: str) -> str:
    """A gap ref like ``gap:cad_edit`` maps to a capability-id-shaped code for the receipt."""
    tail = capability_gap_ref.split(":")[-1].split(".")[-1]
    code = "".join(ch if ch.isalnum() else "_" for ch in tail).upper().strip("_")
    if not code or not code[0].isalpha():
        code = f"GAP_{code}" if code else "GAP"
    return code[:63] if len(code) >= 3 else f"{code}_GAP"


def _progress_row(binding: _SessionBinding, outcome: LearningOutcome) -> dict[str, Any]:
    session = outcome.session
    assert session is not None
    # The row carries the governed binding so follow-up operations can re-authorize from it (advisory only;
    # its authenticity is proven against the creation receipt's binding digest, not trusted on its own).
    row: dict[str, Any] = {
        "project_id": binding.project_id, "work_contract_id": binding.work_contract_id,
        "capability_gap_ref": binding.capability_gap_ref, "classification": binding.classification.value,
        "provider_id": binding.provider_id, "session_status": session.status.value,
        "model_routing": session.model_routing.value, "progress_percent": session.progress_percent}
    if outcome.assessment is not None:
        row["human_mastery"] = outcome.assessment.human_mastery.value
        row["grants_capability"] = outcome.assessment.grants_capability
    return row


# Re-exported so callers (and tests) can reference the resolution vocabulary via the service module.
__all__ = ["LearningService", "MaterialResolutionCode"]
