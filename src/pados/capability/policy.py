# SPDX-License-Identifier: AGPL-3.0-only
"""Policy filter for one (capability, adapter) pair in one task context. Fail closed, explainable.

Authorization is never caller-asserted: a ``PolicyContext`` can only be built from an ``AuthorizedScope``,
which ``authorize_scope`` derives through the existing PADOS grant validator
(``pados.execution.authorization.authorize_execution_capabilities``). That frozen primitive has no expiry
or revocation of its own, so executors must call ``authorize_scope`` again with the current grant document
immediately before execution (re-validation, never a cached decision).

The filter decides feasibility only; it never ranks. Network use is the intersection of task, capability
and adapter restrictions (an adapter can never widen what the capability allows). Model inference must go
through the Worker Router bridge. Fallback is explicit and same-policy only, and never turns a
deterministic engine into a probabilistic model.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FallbackPolicy,
    descriptor_digest,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.authorization import authorize_execution_capabilities
from pados.execution.context import canonical_hash

POLICY_VERSION = "pados.capability.policy.v1"


class PolicyCode(StrEnum):
    ALLOWED = "ALLOWED"
    SCOPE_PROJECT_MISMATCH = "SCOPE_PROJECT_MISMATCH"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    LIFECYCLE_NOT_PLANNABLE = "LIFECYCLE_NOT_PLANNABLE"
    CLASSIFICATION_EXCEEDS_CAPABILITY = "CLASSIFICATION_EXCEEDS_CAPABILITY"
    CLASSIFICATION_EXCEEDS_ADAPTER = "CLASSIFICATION_EXCEEDS_ADAPTER"
    NETWORK_NOT_PERMITTED = "NETWORK_NOT_PERMITTED"
    ADAPTER_WIDENS_CAPABILITY_NETWORK = "ADAPTER_WIDENS_CAPABILITY_NETWORK"
    MODEL_INFERENCE_MUST_DELEGATE = "MODEL_INFERENCE_MUST_DELEGATE"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"


# Restrictiveness: lower = less network use. NETWORK_OPTIONAL may use the network when present.
NETWORK_RANK: dict[NetworkRequirement, int] = {
    NetworkRequirement.LOCAL_ONLY: 0,
    NetworkRequirement.PRIVATE_NETWORK: 1,
    NetworkRequirement.NETWORK_OPTIONAL: 2,
    NetworkRequirement.WAN_REQUIRED: 3,
}


def network_permitted(requirement: NetworkRequirement, ceiling: NetworkRequirement) -> bool:
    return NETWORK_RANK[requirement] <= NETWORK_RANK[ceiling]


@dataclass(frozen=True)
class AuthorizedScope:
    """The validated result of the existing PADOS grant check for one work contract."""

    project_id: str
    work_contract_id: str
    capabilities: frozenset[str]
    grant_hash: str
    authorization_source: str


def authorize_scope(contract: Mapping[str, Any], grant: Mapping[str, Any], *,
                    installed_skills: frozenset[str] = frozenset(),
                    registered_mcp_capabilities: frozenset[str] = frozenset()) -> AuthorizedScope:
    """Validate through the existing grant primitive; raises PermissionError when not authorized."""
    validated = authorize_execution_capabilities(contract, grant, installed_skills=installed_skills,
                                                 registered_mcp_capabilities=registered_mcp_capabilities)
    return AuthorizedScope(project_id=str(validated["project_id"]),
                           work_contract_id=str(validated["work_contract_id"]),
                           capabilities=frozenset(str(c) for c in validated["capabilities"]),
                           grant_hash=canonical_hash(dict(validated)),
                           authorization_source=str(validated["authorization_source"]))


@dataclass(frozen=True)
class PolicyContext:
    scope: AuthorizedScope
    project_id: str
    stage_id: str
    classification: DataClassification
    network_policy: NetworkRequirement  # the most permissive network use this task allows
    resource_id: str = "unassigned"


@dataclass(frozen=True)
class PolicyDecision:
    capability_id: str
    adapter_id: str
    code: PolicyCode
    checks: tuple[tuple[str, bool], ...]
    binding: tuple[tuple[str, str], ...]  # what this decision is bound to (see evaluate)

    @property
    def allowed(self) -> bool:
        return self.code is PolicyCode.ALLOWED

    @property
    def decision_hash(self) -> str:
        return canonical_hash({"policy_version": POLICY_VERSION, "capability_id": self.capability_id,
                               "adapter_id": self.adapter_id, "code": self.code.value,
                               "checks": [list(item) for item in self.checks],
                               "binding": [list(item) for item in self.binding]})


def evaluate(capability: CapabilityDescriptor, adapter: AdapterDescriptor, context: PolicyContext, *,
             adapter_available: Callable[[], bool], resource_fit: bool = True) -> PolicyDecision:
    """Evaluate every check; ``adapter_available`` (adapter code) runs only after all policy checks pass."""
    stricter = min((capability.max_classification, adapter.max_classification), key=lambda c: c.rank)
    model_runtime = capability.family is CapabilityFamily.MODEL_RUNTIME
    policy_checks: list[tuple[str, bool]] = [
        (PolicyCode.SCOPE_PROJECT_MISMATCH.value, context.project_id == context.scope.project_id),
        (PolicyCode.CAPABILITY_MISMATCH.value, adapter.capability_id == capability.capability_id),
        (PolicyCode.NOT_AUTHORIZED.value, adapter.grant_capability in context.scope.capabilities),
        (PolicyCode.LIFECYCLE_NOT_PLANNABLE.value, capability.lifecycle.plannable and adapter.lifecycle.plannable),
        (PolicyCode.CLASSIFICATION_EXCEEDS_CAPABILITY.value,
         capability.max_classification.permits(context.classification)),
        (PolicyCode.CLASSIFICATION_EXCEEDS_ADAPTER.value, stricter.permits(context.classification)),
        (PolicyCode.NETWORK_NOT_PERMITTED.value,
         network_permitted(capability.network_requirement, context.network_policy)
         and network_permitted(adapter.network_requirement, context.network_policy)),
        (PolicyCode.ADAPTER_WIDENS_CAPABILITY_NETWORK.value,
         network_permitted(adapter.network_requirement, capability.network_requirement)),
        (PolicyCode.MODEL_INFERENCE_MUST_DELEGATE.value, not model_runtime or adapter.delegates_to_worker_router),
        (PolicyCode.RESOURCE_UNAVAILABLE.value, resource_fit),
    ]
    policy_ok = all(ok for _, ok in policy_checks)
    available = bool(adapter_available()) if policy_ok else False
    checks = (*policy_checks, (PolicyCode.ADAPTER_UNAVAILABLE.value, available))
    failing = next((PolicyCode(name) for name, ok in checks if not ok), PolicyCode.ALLOWED)
    binding = (
        ("capability_digest", descriptor_digest(capability)), ("adapter_digest", descriptor_digest(adapter)),
        ("project_id", context.project_id), ("work_contract_id", context.scope.work_contract_id),
        ("grant_hash", context.scope.grant_hash), ("stage_id", context.stage_id),
        ("classification", context.classification.value), ("network_policy", context.network_policy.value),
        ("resource_id", context.resource_id), ("policy_version", POLICY_VERSION),
    )
    return PolicyDecision(capability.capability_id, adapter.adapter_id, failing, checks, binding)


@dataclass(frozen=True)
class FallbackTransition:
    """Recorded, explicit replacement of a failed adapter by an alternative (goes into the stage receipt)."""

    capability_id: str
    from_adapter_id: str
    to_adapter_id: str
    primary_decision_hash: str
    alternative_decision_hash: str

    @property
    def transition_hash(self) -> str:
        return canonical_hash({"capability_id": self.capability_id, "from": self.from_adapter_id,
                               "to": self.to_adapter_id, "primary": self.primary_decision_hash,
                               "alternative": self.alternative_decision_hash})


_CONTEXT_KEYS = ("capability_digest", "project_id", "work_contract_id", "grant_hash", "stage_id", "classification",
                 "network_policy", "policy_version")


def fallback_permitted(capability: CapabilityDescriptor, primary: AdapterDescriptor, alternative: AdapterDescriptor,
                       primary_decision: PolicyDecision,
                       alternative_decision: PolicyDecision) -> FallbackTransition | None:
    """An alternative may replace a failed adapter only when the capability opts in explicitly, both
    decisions are ALLOWED for the same capability descriptor, project, work contract, grant, stage,
    classification and network policy, the alternative decision names exactly this alternative descriptor,
    and determinism is not weakened (no deterministic engine -> probabilistic model substitution)."""
    weaker = {Determinism.DETERMINISTIC: 0, Determinism.SEEDED: 1, Determinism.PROBABILISTIC: 2}
    primary_binding, alt_binding = dict(primary_decision.binding), dict(alternative_decision.binding)
    ok = (capability.fallback is FallbackPolicy.EXPLICIT_SAME_POLICY
          and primary_decision.allowed and alternative_decision.allowed
          and primary_decision.adapter_id == primary.adapter_id
          and alternative_decision.adapter_id == alternative.adapter_id != primary.adapter_id
          and primary_decision.capability_id == alternative_decision.capability_id == capability.capability_id
          and alternative.capability_id == primary.capability_id == capability.capability_id
          and primary_binding.get("capability_digest") == descriptor_digest(capability)
          and primary_binding.get("adapter_digest") == descriptor_digest(primary)
          and alt_binding.get("adapter_digest") == descriptor_digest(alternative)
          and all(primary_binding.get(key) == alt_binding.get(key) for key in _CONTEXT_KEYS)
          and weaker[alternative.determinism] <= weaker[primary.determinism])
    if not ok:
        return None
    return FallbackTransition(capability.capability_id, primary.adapter_id, alternative.adapter_id,
                              primary_decision.decision_hash, alternative_decision.decision_hash)
