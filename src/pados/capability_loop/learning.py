# SPDX-License-Identifier: AGPL-3.0-only
"""Learning service: gate -> learning provider -> normalized session/assessment -> chained receipt.

The P7.2 vertical slice. Every operation -- create session, poll state, assessment -- is authorized by the
PADOS learning gate before the provider is called, records one hash-chained ``OperationReceipt``, and stores
a normalized session / assessment projection. It never promotes a capability and never writes PADOS semantic
state; OpenMAIC's own model choice is recorded as an external boundary, not as PADOS routing.

Authorization of a follow-up rests on an authoritative, externally anchored ``SessionBindingAuthority``
(injected; see ``pados.capability_loop.binding_authority``), never on the mutable progress projection, a
caller-supplied project, or a provider-returned project. At creation the governed binding is recorded through
the authority and the session is accepted only if that bind succeeds. A follow-up resolves the original
governed context from the authority (integrity- and anchor-verified), re-runs the gate against the *current*
grant, calls the provider only then, and rejects any response whose session/assessment identity differs from
the binding. The ``progress`` row is an ADVISORY_PROJECTION_ONLY and is never an authorization source;
tampering it cannot change authorization. Every authority/anchor/binding failure fails closed -- there is no
fallback.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pados.capability.model import AdapterDescriptor, CapabilityDescriptor
from pados.capability_loop.binding_authority import (
    BindingAuthorityError,
    GovernedSessionBinding,
    SessionBindingAuthority,
)
from pados.capability_loop.contracts import FailureCode, ProviderStatus
from pados.capability_loop.gate import GateCode, GateDecision, evaluate_learning
from pados.capability_loop.learning_contracts import (
    LearningOutcome,
    LearningProvider,
    LearningRequest,
    LearningSessionStatus,
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
    is_valid_receipt_code,
    is_valid_receipt_id,
)
from pados.capability_loop.store import LoopStore
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash

_UNRESOLVED_CAPABILITY = "UNRESOLVED"  # receipt capability id when no binding could be resolved
_SAFE_PROJECT_PLACEHOLDER = "unresolved-project"  # bounded, receipt-valid stand-in for a malformed project id


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LearningService:
    def __init__(self, provider: LearningProvider, store: LoopStore, *,
                 capability: CapabilityDescriptor, adapter: AdapterDescriptor,
                 authority: SessionBindingAuthority,
                 network_policy: NetworkRequirement = NetworkRequirement.LOCAL_ONLY,
                 clock: Callable[[], str] = _utc_now_iso) -> None:
        self._provider = provider
        self._store = store
        self._capability = capability
        self._adapter = adapter
        self._authority = authority  # authoritative, externally anchored; the sole binding source of truth
        self._network_policy = network_policy
        self._clock = clock

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
        resolution = resolve_materials(request, material_registrations or {})
        if resolution.code is not MaterialResolutionCode.RESOLVED:
            return self._coded_refusal(OperationKind.LEARN_CREATE_SESSION, request.project_id,
                                       request.capability_gap_ref, request_digest,
                                       material_policy_code(resolution.code), decision.policy_decision_hash)
        outcome = self._provider.create_session(request)
        # Bind only under the whole-success invariant (AVAILABLE, no failure, complete, with a non-failed
        # session), the same bar discovery uses before accepting a candidate. LearningOutcome permits a
        # session alongside a DEGRADED/partial status with no failure; an explicitly failed or incomplete
        # answer must NOT create a durable authoritative binding that would authorize future calls.
        if (outcome.failure is not None or outcome.session is None or outcome.partial
                or outcome.status is not ProviderStatus.AVAILABLE
                or outcome.session.status is LearningSessionStatus.FAILED):
            return self._creation_failure(request, request_digest, decision, outcome)
        # Authoritatively bind the external session to this governed context BEFORE accepting it. If the
        # authority refuses (a conflicting prior binding, or an unavailable/mismatched anchor), the session is
        # not accepted and no follow-up is ever authorized against it.
        binding = self._governed_binding(request, outcome.session.session_ref, classification)
        if binding is None:
            return self._coded_refusal(OperationKind.LEARN_CREATE_SESSION, request.project_id,
                                       request.capability_gap_ref, request_digest,
                                       GateCode.SESSION_BIND_REFUSED, decision.policy_decision_hash)
        # Creation outcomes can already carry an assessment. Validate its provider/session/gap identity
        # against the would-be authoritative binding before authority.bind(), so foreign assessment evidence
        # can never make an otherwise valid session durable or enter the advisory projection.
        if not _outcome_matches_binding(outcome, binding):
            return self._creation_failure(request, request_digest, decision, outcome)
        try:
            self._authority.bind(binding)
        except BindingAuthorityError:
            return self._coded_refusal(OperationKind.LEARN_CREATE_SESSION, request.project_id,
                                       request.capability_gap_ref, request_digest,
                                       GateCode.SESSION_BIND_REFUSED, decision.policy_decision_hash)
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
                  call: Callable[[GovernedSessionBinding], LearningOutcome]) -> OperationReceipt:
        request_digest = canonical_hash(
            {"op": kind.value, "session": session_ref, "project": contract.get("project_id"),
             "work_contract": contract.get("work_contract_id")})
        caller_project = _safe_project_id(contract.get("project_id"))
        try:
            binding = self._authority.resolve(self._provider.provider_id, session_ref)
        except BindingAuthorityError:
            return self._coded_refusal(kind, caller_project, None, request_digest,
                                       GateCode.SESSION_BINDING_UNVERIFIABLE, None)
        if binding is None:
            return self._coded_refusal(kind, caller_project, None, request_digest,
                                       GateCode.SESSION_NOT_BOUND, None)
        gap_capability = _safe_capability_code(binding.capability_gap_ref)
        if binding.provider_id != self._provider.provider_id:
            return self._coded_refusal(kind, _safe_project_id(binding.project_id), None, request_digest,
                                       GateCode.SESSION_PROVIDER_MISMATCH, None, capability_override=gap_capability)
        if (contract.get("project_id") != binding.project_id
                or contract.get("work_contract_id") != binding.work_contract_id):
            return self._coded_refusal(kind, _safe_project_id(binding.project_id), None, request_digest,
                                       GateCode.SESSION_PROJECT_MISMATCH, None, capability_override=gap_capability)
        # The authoritative binding IS the commit: its presence means the session is committed. Authorization
        # never depends on the non-authoritative LoopStore receipt chain (deleting that operational evidence
        # must never change effective authority), so there is no loop-store commit gate here.
        decision = evaluate_learning(
            project_id=binding.project_id, work_contract_id=binding.work_contract_id,
            request_id=f"{kind.value.lower()}:{session_ref}", classification=binding.classification,
            contract=contract, grant=grant, capability=self._capability, adapter=self._adapter,
            network_policy=self._network_policy, adapter_available=lambda: True,
            registered_mcp_capabilities=registered_mcp_capabilities)
        if not decision.allowed:
            return self._refusal(kind, binding.project_id, binding.capability_gap_ref, request_digest, decision)
        outcome = call(binding)
        # The provider must answer about the bound session, and any assessment must carry the bound
        # session/gap/provider identity; otherwise reject before any projection update.
        forced = None if _outcome_matches_binding(outcome, binding) else FailureCode.MALFORMED_RESPONSE
        return self._record(kind, binding, request_digest, decision, outcome, forced_failure=forced)

    # -- recording ----------------------------------------------------------
    def _governed_binding(self, request: LearningRequest, external_session_ref: str,
                          classification: DataClassification) -> GovernedSessionBinding | None:
        try:
            return GovernedSessionBinding(
                provider_id=self._provider.provider_id, external_session_ref=external_session_ref,
                project_id=request.project_id, work_contract_id=request.work_contract_id,
                classification=classification, capability_gap_ref=request.capability_gap_ref,
                created_at=self._clock())
        except ValueError:
            return None  # a malformed external session ref can never be bound

    def _refusal(self, kind: OperationKind, project_id: str, capability_gap_ref: str,
                 request_digest: str, decision: GateDecision) -> OperationReceipt:
        return self._coded_refusal(kind, _safe_project_id(project_id), capability_gap_ref, request_digest,
                                   decision.code, decision.policy_decision_hash)

    def _coded_refusal(self, kind: OperationKind, project_id: str | None, capability_gap_ref: str | None,
                       request_digest: str, policy_code: str, policy_decision_hash: str | None, *,
                       capability_override: str | None = None) -> OperationReceipt:
        capability_id = capability_override or (
            _safe_capability_code(capability_gap_ref) if capability_gap_ref is not None
            else _UNRESOLVED_CAPABILITY)
        if not is_valid_receipt_code(capability_id):  # final guard against any malformed code reaching the model
            capability_id = _UNRESOLVED_CAPABILITY
        return self._store.append({
            "operation": kind, "project_id": _safe_project_id(project_id), "request_digest": request_digest,
            "provider_id": self._provider.provider_id, "capability_id": capability_id,
            "policy_code": policy_code, "policy_decision_hash": policy_decision_hash,
            "result": OperationResult.REFUSED_BY_POLICY, "failure": None, "evidence_digests": (),
            "limitations": (), "accepted_into_loop": False, "state_before": None, "state_after": None})

    def _creation_failure(self, request: LearningRequest, request_digest: str, decision: GateDecision,
                          outcome: LearningOutcome) -> OperationReceipt:
        """The provider did not create a session: record a normalized failure, no binding, no projection."""
        failure = outcome.failure if outcome.failure is not None else FailureCode.MALFORMED_RESPONSE
        return self._store.append({
            "operation": OperationKind.LEARN_CREATE_SESSION, "project_id": request.project_id,
            "request_digest": request_digest, "provider_id": self._provider.provider_id,
            "capability_id": _safe_capability_code(request.capability_gap_ref), "policy_code": decision.code,
            "policy_decision_hash": decision.policy_decision_hash,
            "result": OperationResult.PROVIDER_FAILED, "failure": failure, "evidence_digests": (),
            "limitations": (), "accepted_into_loop": False, "state_before": None, "state_after": None})

    def _record(self, kind: OperationKind, binding: GovernedSessionBinding, request_digest: str,
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
            # ADVISORY_PROJECTION_ONLY, keyed by the bound external session ref; never an authorization source.
            progress = (binding.external_session_ref, _progress_row(binding, outcome))
        evidence_digests = (outcome.assessment.evidence_digest,) if (
            not failed and outcome.assessment is not None) else ()
        return self._store.append({
            "operation": kind, "project_id": binding.project_id, "request_digest": request_digest,
            "provider_id": self._provider.provider_id,
            "capability_id": _safe_capability_code(binding.capability_gap_ref),
            "policy_code": decision.code, "policy_decision_hash": decision.policy_decision_hash,
            "binding_digest": binding.context_hash, "session_ref": binding.external_session_ref,
            "result": OperationResult.PROVIDER_FAILED if failed else OperationResult.RECORDED,
            "failure": failure, "evidence_digests": evidence_digests, "limitations": limitations,
            "accepted_into_loop": not failed, "state_before": None, "state_after": None},
            progress=None if failed else progress)


def _outcome_matches_binding(outcome: LearningOutcome, binding: GovernedSessionBinding) -> bool:
    """The provider's own identity and its session/assessment identity must match the authoritative binding.

    The response is attributed to the bound provider, and any session/assessment it carries names the bound
    provider too; a response whose provider differs (even with a matching session ref) is rejected so a
    foreign provider's answer can never be recorded under this binding's provider.
    """
    if outcome.provider_id != binding.provider_id:
        return False
    session = outcome.session
    if session is not None and (
            session.session_ref != binding.external_session_ref
            or session.provider_id != binding.provider_id):
        return False
    assessment = outcome.assessment
    if assessment is not None and (
            assessment.session_ref != binding.external_session_ref
            or assessment.capability_gap_ref != binding.capability_gap_ref
            or assessment.provider_id != binding.provider_id):
        return False
    return True


def _safe_project_id(candidate: object) -> str:
    """The caller/binding project id if it is a valid receipt identifier, else a bounded safe placeholder."""
    return str(candidate) if is_valid_receipt_id(candidate) else _SAFE_PROJECT_PLACEHOLDER


def _safe_capability_code(capability_gap_ref: str) -> str:
    """A receipt-valid capability code for a gap ref, or the unresolved placeholder if none can be formed."""
    code = _gap_capability(capability_gap_ref)
    return code if is_valid_receipt_code(code) else _UNRESOLVED_CAPABILITY


def _gap_capability(capability_gap_ref: str) -> str:
    """A gap ref like ``gap:cad_edit`` maps to a capability-id-shaped code for the receipt.

    Only ASCII alphanumerics survive, so a gap ref carrying Unicode alphanumerics can never produce a code
    that fails the receipt's ASCII code contract and raises.
    """
    tail = capability_gap_ref.split(":")[-1].split(".")[-1]
    code = "".join(ch if (ch.isascii() and ch.isalnum()) else "_" for ch in tail).upper().strip("_")
    if not code or not code[0].isalpha():
        code = f"GAP_{code}" if code else "GAP"
    return code[:63] if len(code) >= 3 else f"{code}_GAP"


def _progress_row(binding: GovernedSessionBinding, outcome: LearningOutcome) -> dict[str, Any]:
    session = outcome.session
    assert session is not None
    # ADVISORY_PROJECTION_ONLY: a convenience view of the latest provider state. Authorization never reads it.
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
