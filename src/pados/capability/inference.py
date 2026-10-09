# SPDX-License-Identifier: AGPL-3.0-only
"""The concrete Worker Router bridge for model-inference stages (the fabric's ONLY router contact).

Model-inference stages are never ranked by the fabric. ``WorkerRouterBridge`` maps the fabric
capability to an existing Worker Router capability (operator configuration), narrows the router's
candidate worker TYPES to those whose existing provider profile (``pados.enterprise.compute``) permits
the task classification and network policy — a narrowing ``select_worker`` itself supports, which never
changes its ranking — and calls ``pados.worker.router.select_worker`` read-only. The returned
``InferenceSelection.selection_hash`` is the canonical hash of the router decision bound to the request;
the executor keeps that evidence and records it in the stage receipt.

Measured local candidates (optional, ``MeasuredLocalCandidates``): for a fabric capability the operator maps to a
``CapabilityRequirement``, every REGISTERED ``ModelCapabilityProfile`` is assessed (measured evidence, current
runtime and device state, resource headroom) and checked against the EXISTING authorization authority (a
compute-node registration that explicitly names the profile's immutable model reference). Each one is then
presented to ``select_worker`` as a candidate of the router's existing local-model worker class; ineligible and
unauthorized profiles are presented as not available, with their reasons, so the router's own ranking moves on
(cloud escalation only where classification and network policy already permit it). The bridge still never ranks:
presentation order is fixed (state, then profile id), selection is the router's, and an assessment never
authorizes. Nothing here reads a model, vendor or runtime name.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pados.capability.policy import network_permitted
from pados.capability.seams import InferenceRequest, InferenceSelection
from pados.compute_node.manifest import NodeCapability
from pados.compute_node.provider import LOCAL_MODEL_WORKER
from pados.compute_node.registry import NodeRegistration
from pados.connectivity.requirements import NetworkRequirement, resolve_requirement
from pados.enterprise.compute import profile_for_worker
from pados.execution.context import canonical_hash
from pados.resources.capability_profile import (
    CandidateState,
    CapabilityRequirement,
    LiveRuntimeBinding,
    MeasuredCapability,
    ModelCapabilityProfile,
    ResourceHeadroom,
    assess,
)
from pados.worker.router import select_worker
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType

ROUTER_REF = "pados.worker.router.select_worker"
ROUTED = "ROUTED"
MAX_ISSUED = 1024


@dataclass(frozen=True)
class LocalProfileCandidate:
    """A REGISTERED measured profile with the CURRENT state of its runtime and device (read per selection)."""

    profile: ModelCapabilityProfile
    runtime_state: CandidateState
    device_state: CandidateState
    headroom: ResourceHeadroom | None
    reasons: tuple[str, ...] = ()  # why the live binding is degraded or refused (explanation only)
    binding: LiveRuntimeBinding | None = None

    @classmethod
    def from_binding(cls, profile: ModelCapabilityProfile, binding: LiveRuntimeBinding) -> LocalProfileCandidate:
        """The candidate a live binding supports; a binding for another profile is refused."""
        if binding.profile_id != profile.profile_id or binding.manifest_digest != profile.artifact.manifest_digest:
            raise ValueError("LIVE_BINDING_PROFILE_MISMATCH")
        return cls(profile, binding.runtime_state, binding.device_state, binding.headroom, binding.reasons, binding)


def worker_id_for(profile: ModelCapabilityProfile) -> str:
    return "lcp-" + profile.profile_id.removeprefix("mcp-")[:24]


ProfileAuthorization = Callable[[ModelCapabilityProfile, InferenceRequest], bool]


@dataclass(frozen=True)
class MeasuredLocalCandidates:
    requirements: Mapping[str, CapabilityRequirement]  # fabric capability_id -> measured requirement (operator)
    candidates: Callable[[], Sequence[LocalProfileCandidate]]
    authorization: ProfileAuthorization

    def candidate_for(self, worker_id: str) -> LocalProfileCandidate | None:
        """The CURRENT candidate behind a measured worker id (re-read; None once withdrawn)."""
        return next((item for item in self.candidates() if worker_id_for(item.profile) == worker_id), None)


def registration_authorizes(registrations: Callable[[], Iterable[NodeRegistration]],
                            capability: NodeCapability = NodeCapability.LOCAL_INFERENCE) -> ProfileAuthorization:
    """Authorization from the existing compute-node registrations: authorized, project admitted, capability and
    classification inside the registration, and the profile's immutable model reference named EXPLICITLY (an
    empty model allowlist never authorizes a measured profile; a re-tagged artifact is another reference)."""
    def authorized(profile: ModelCapabilityProfile, request: InferenceRequest) -> bool:
        return any(item.authorized and item.admits_project(request.project_id)
                   and capability in item.allowed_capabilities
                   and item.max_classification.permits(request.classification)
                   and profile.artifact.model_ref in item.allowed_models for item in registrations())
    return authorized


def measured_local_workers(source: MeasuredLocalCandidates, request: InferenceRequest,
                           router_capability: CapabilityId) -> dict[str, WorkerSpec]:
    """Worker Router candidates for the measured local profiles of ``request.capability_id`` (no requirement
    mapped -> none). Unauthorized -> DISABLED; ineligible -> UNAVAILABLE; reasons travel in ``WorkerSpec.reason``."""
    requirement = source.requirements.get(request.capability_id)
    if requirement is None:
        return {}
    ranked: list[tuple[tuple[int, str], str, WorkerSpec]] = []
    for candidate in source.candidates():
        profile = candidate.profile
        assessment = assess(profile, requirement, runtime_state=candidate.runtime_state,
                            device_state=candidate.device_state, headroom=candidate.headroom)
        try:
            authorized = source.authorization(profile, request) is True
        except Exception:  # noqa: BLE001 - an authority that cannot answer authorizes nothing (fail closed)
            authorized = False
        eligible = assessment.eligible(requirement)
        availability = (WorkerAvailability.DISABLED if not authorized else WorkerAvailability.AVAILABLE if eligible
                        else WorkerAvailability.UNAVAILABLE)
        reasons = tuple(dict.fromkeys((*(() if authorized else ("NOT_AUTHORIZED",)), *assessment.reasons,
                                       *candidate.reasons)))
        worker_id = worker_id_for(profile)
        contexts = [o.context_tokens for o in profile.observations_of(MeasuredCapability.LONG_CONTEXT_RETRIEVAL)
                    if o.all_passed and o.context_tokens]
        spec = WorkerSpec(
            worker_id=worker_id, worker_type=LOCAL_MODEL_WORKER, display_name=f"measured local profile {worker_id}",
            version=profile.binding.runtime_version, availability=availability, capabilities=[router_capability],
            risk_ceiling=RiskLevel.MEDIUM,
            supports_structured_output=profile.observation(MeasuredCapability.STRUCTURED_JSON).all_passed,
            supports_file_changes=False,
            supports_tool_calls=profile.observation(MeasuredCapability.TOOL_CALL_FORMATTING).all_passed,
            supports_streaming=False, max_context_tokens=max(contexts, default=profile.footprint.base_context_tokens),
            default_timeout_seconds=120,
            reason=f"{assessment.state.value}:" + ",".join(reasons)[:400] if reasons else assessment.state.value)
        order = 0 if availability is WorkerAvailability.AVAILABLE and assessment.state is CandidateState.AVAILABLE \
            else 1 if availability is WorkerAvailability.AVAILABLE else 2
        ranked.append(((order, profile.profile_id), worker_id, spec))
    return {worker_id: spec for _, worker_id, spec in sorted(ranked, key=lambda item: item[0])}


class WorkerRouterBridge:
    """``ModelInferenceDelegate`` implementation; holds no worker state and never ranks providers."""

    def __init__(self, workers: Callable[[], Mapping[str, WorkerSpec]],
                 capability_map: Mapping[str, CapabilityId], *,
                 measured_local: MeasuredLocalCandidates | None = None) -> None:
        self._workers = workers
        self._capability_map = dict(capability_map)
        self._measured_local = measured_local
        self._issued: dict[str, InferenceSelection] = {}  # this bridge's own decisions, by selection hash

    def issued(self, selection_hash: str) -> InferenceSelection | None:
        """A decision this bridge returned (evidence lookup for the adapter that runs the routed stage)."""
        return self._issued.get(selection_hash)

    def select(self, request: InferenceRequest) -> InferenceSelection:
        router_capability = self._capability_map.get(request.capability_id)
        try:
            risk = RiskLevel(request.risk_level)
            network = NetworkRequirement(request.network_policy)
        except ValueError:
            return self._blocked(request, "INFERENCE_REQUEST_INVALID")
        if router_capability is None:
            return self._blocked(request, "MODEL_CAPABILITY_UNMAPPED")
        workers = dict(self._workers())
        if self._measured_local is not None:
            measured = measured_local_workers(self._measured_local, request, router_capability)
            if set(measured) & set(workers):
                return self._blocked(request, "MEASURED_WORKER_ID_COLLISION")
            workers = {**measured, **workers}
        allowed = self._permitted_types(workers, request, network)
        decision = select_worker(workers, capability_id=router_capability, risk_level=risk,
                                 allowed_worker_types=allowed)
        document = {
            "status": decision.status, "selected_worker_id": decision.selected_worker_id,
            "error_code": decision.error_code, "explanation": decision.explanation,
            "considered": decision.considered,
        }
        selection = InferenceSelection(decision.selected_worker_id, decision.status, decision.explanation,
                                       self._hash(request, router_capability.value, allowed, document))
        if len(self._issued) >= MAX_ISSUED:
            self._issued.pop(next(iter(self._issued)))
        self._issued[selection.selection_hash] = selection
        return selection

    @staticmethod
    def _permitted_types(workers: Mapping[str, WorkerSpec], request: InferenceRequest,
                         network: NetworkRequirement) -> frozenset[WorkerType]:
        permitted: set[WorkerType] = set()
        for worker in workers.values():
            profile = profile_for_worker(worker)
            requirement = resolve_requirement(profile=profile)
            if profile.permits(request.classification) and network_permitted(requirement, network):
                permitted.add(worker.worker_type)
        return frozenset(permitted)

    def _blocked(self, request: InferenceRequest, code: str) -> InferenceSelection:
        document: dict[str, Any] = {"status": "BLOCKED", "selected_worker_id": None, "error_code": code,
                    "explanation": code, "considered": []}
        return InferenceSelection(None, "BLOCKED", code, self._hash(request, None, frozenset(), document))

    @staticmethod
    def _hash(request: InferenceRequest, router_capability: str | None, allowed: frozenset[WorkerType],
              decision: Mapping[str, Any]) -> str:
        return canonical_hash({
            "router": ROUTER_REF,
            "request": {"project_id": request.project_id, "stage_id": request.stage_id,
                        "capability_id": request.capability_id, "risk_level": request.risk_level,
                        "classification": request.classification.value, "network_policy": request.network_policy,
                        "grant_hash": request.grant_hash},
            "router_capability": router_capability,
            "allowed_worker_types": sorted(item.value for item in allowed),
            "decision": dict(decision),
        })


def selection_routed(selection: InferenceSelection) -> bool:
    return selection.status == ROUTED and selection.selected_worker_id is not None
