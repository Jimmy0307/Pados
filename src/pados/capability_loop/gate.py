# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS discovery gate: every external inspection request is authorized here, before any provider call.

Order (fail closed, first failing check wins, every check recorded):

1. target: the request names an operator-registered target (``TargetRegistration``) of the same project,
   with a recorded owner authorization; the caller never supplies a path.
2. classification: the request classification may not exceed the target's ceiling.
3. credential exposure: the target is not credential-shaped, and the work contract carries no SecretRef.
4. grant: ``capability.policy.authorize_scope`` (the existing project-bound PADOS grant validator).
5. policy: ``capability.policy.evaluate`` on the discovery capability / provider adapter descriptors
   (project, grant, lifecycle, classification, network); provider availability is probed only after every
   policy check passed.

The gate decides; it never invokes a provider and never changes PADOS state.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pados.capability.model import AdapterDescriptor, CapabilityDescriptor
from pados.capability.policy import PolicyCode, PolicyContext, authorize_scope, evaluate
from pados.capability_loop.contracts import DiscoveryRequest
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification

GATE_VERSION = "pados.capability_loop.gate.v1"
# File names that hold credentials or key material are never handed to an inspection provider.
_CREDENTIAL_NAME = re.compile(
    r"(^|[._-])(env|secret|secrets|credential|credentials|token|tokens|password|passwd|shadow|keychain|"
    r"keystore|id_rsa|id_dsa|id_ecdsa|id_ed25519|netrc|npmrc|pypirc)($|[._-])|\.(pem|key|p12|pfx|kdbx|jks)$",
    re.IGNORECASE,
)


class GateCode:
    ALLOWED = "ALLOWED"
    TARGET_NOT_REGISTERED = "TARGET_NOT_REGISTERED"
    TARGET_PROJECT_MISMATCH = "TARGET_PROJECT_MISMATCH"
    TARGET_NOT_AUTHORIZED = "TARGET_NOT_AUTHORIZED"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    TARGET_OUTSIDE_ALLOWED_ROOT = "TARGET_OUTSIDE_ALLOWED_ROOT"
    CLASSIFICATION_EXCEEDS_TARGET = "CLASSIFICATION_EXCEEDS_TARGET"
    CREDENTIAL_EXPOSURE = "CREDENTIAL_EXPOSURE"
    WORK_CONTRACT_MISMATCH = "WORK_CONTRACT_MISMATCH"
    GRANT_REFUSED = "GRANT_REFUSED"


@dataclass(frozen=True)
class TargetRegistration:
    """An operator-registered inspection target. Only operator config creates these."""

    target_ref: str
    project_id: str
    path: Path
    classification: DataClassification
    owner_authorization_ref: str  # the operator's recorded statement that the project owns / may inspect it
    allowed_root: Path


@dataclass(frozen=True)
class GateDecision:
    code: str
    checks: tuple[tuple[str, bool], ...]
    policy_decision_hash: str | None
    target_path: str | None  # resolved only when allowed

    @property
    def allowed(self) -> bool:
        return self.code == GateCode.ALLOWED


def _refused(code: str, checks: list[tuple[str, bool]]) -> GateDecision:
    return GateDecision(code, tuple(checks), None, None)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _target_checks(request: DiscoveryRequest, target: TargetRegistration | None,
                   checks: list[tuple[str, bool]]) -> tuple[str | None, Path | None]:
    checks.append((GateCode.TARGET_NOT_REGISTERED, target is not None))
    if target is None:
        return GateCode.TARGET_NOT_REGISTERED, None
    checks.append((GateCode.TARGET_PROJECT_MISMATCH, target.project_id == request.project_id))
    if target.project_id != request.project_id:
        return GateCode.TARGET_PROJECT_MISMATCH, None
    authorized = bool(target.owner_authorization_ref.strip())
    checks.append((GateCode.TARGET_NOT_AUTHORIZED, authorized))
    if not authorized:
        return GateCode.TARGET_NOT_AUTHORIZED, None
    permitted = target.classification.permits(request.classification)
    checks.append((GateCode.CLASSIFICATION_EXCEEDS_TARGET, permitted))
    if not permitted:
        return GateCode.CLASSIFICATION_EXCEEDS_TARGET, None
    if target.path.is_symlink() or not target.path.is_file():
        checks.append((GateCode.TARGET_UNAVAILABLE, False))
        return GateCode.TARGET_UNAVAILABLE, None
    resolved, root = target.path.resolve(), target.allowed_root.resolve()
    inside = _within(resolved, root)
    checks.append((GateCode.TARGET_OUTSIDE_ALLOWED_ROOT, inside))
    if not inside:
        return GateCode.TARGET_OUTSIDE_ALLOWED_ROOT, None
    clean = not any(_CREDENTIAL_NAME.search(part) for part in resolved.relative_to(root).parts)
    checks.append((GateCode.CREDENTIAL_EXPOSURE, clean))
    if not clean:
        return GateCode.CREDENTIAL_EXPOSURE, None
    return None, resolved


def evaluate_discovery(request: DiscoveryRequest, *, target: TargetRegistration | None,
                       contract: Mapping[str, Any], grant: Mapping[str, Any],
                       capability: CapabilityDescriptor, adapter: AdapterDescriptor,
                       network_policy: NetworkRequirement, adapter_available: Callable[[], bool],
                       registered_mcp_capabilities: frozenset[str] = frozenset()) -> GateDecision:
    checks: list[tuple[str, bool]] = []
    refusal, resolved = _target_checks(request, target, checks)
    if refusal is not None or resolved is None:
        return _refused(refusal or GateCode.TARGET_UNAVAILABLE, checks)
    no_secrets = not contract.get("secret_refs")
    checks.append((GateCode.CREDENTIAL_EXPOSURE, no_secrets))
    if not no_secrets:
        return _refused(GateCode.CREDENTIAL_EXPOSURE, checks)
    same_contract = (contract.get("work_contract_id") == request.work_contract_id
                     and contract.get("project_id") == request.project_id)
    checks.append((GateCode.WORK_CONTRACT_MISMATCH, same_contract))
    if not same_contract:
        return _refused(GateCode.WORK_CONTRACT_MISMATCH, checks)
    try:
        scope = authorize_scope(contract, grant, registered_mcp_capabilities=registered_mcp_capabilities)
    except PermissionError:
        checks.append((GateCode.GRANT_REFUSED, False))
        return _refused(GateCode.GRANT_REFUSED, checks)
    checks.append((GateCode.GRANT_REFUSED, True))
    context = PolicyContext(scope=scope, project_id=request.project_id, stage_id=f"discover:{request.request_id}",
                            classification=request.classification, network_policy=network_policy)
    decision = evaluate(capability, adapter, context, adapter_available=adapter_available)
    checks.extend(decision.checks)
    if decision.code is not PolicyCode.ALLOWED:
        return GateDecision(decision.code.value, tuple(checks), decision.decision_hash, None)
    return GateDecision(GateCode.ALLOWED, tuple(checks), decision.decision_hash, str(resolved))


def evaluate_learning(*, project_id: str, work_contract_id: str, request_id: str,
                      classification: DataClassification, contract: Mapping[str, Any],
                      grant: Mapping[str, Any], capability: CapabilityDescriptor, adapter: AdapterDescriptor,
                      network_policy: NetworkRequirement, adapter_available: Callable[[], bool],
                      registered_mcp_capabilities: frozenset[str] = frozenset()) -> GateDecision:
    """Authorize a learning operation (no filesystem target): credential, work-contract, grant and policy.

    A learning request sends a goal and material references to a local learning runtime. There is no path,
    so there is no target check; the material bytes stay in PADOS (the adapter sends references only).
    """
    checks: list[tuple[str, bool]] = []
    no_secrets = not contract.get("secret_refs")
    checks.append((GateCode.CREDENTIAL_EXPOSURE, no_secrets))
    if not no_secrets:
        return _refused(GateCode.CREDENTIAL_EXPOSURE, checks)
    same_contract = (contract.get("work_contract_id") == work_contract_id
                     and contract.get("project_id") == project_id)
    checks.append((GateCode.WORK_CONTRACT_MISMATCH, same_contract))
    if not same_contract:
        return _refused(GateCode.WORK_CONTRACT_MISMATCH, checks)
    try:
        scope = authorize_scope(contract, grant, registered_mcp_capabilities=registered_mcp_capabilities)
    except PermissionError:
        checks.append((GateCode.GRANT_REFUSED, False))
        return _refused(GateCode.GRANT_REFUSED, checks)
    checks.append((GateCode.GRANT_REFUSED, True))
    context = PolicyContext(scope=scope, project_id=project_id, stage_id=f"learn:{request_id}",
                            classification=classification, network_policy=network_policy)
    decision = evaluate(capability, adapter, context, adapter_available=adapter_available)
    checks.extend(decision.checks)
    if decision.code is not PolicyCode.ALLOWED:
        return GateDecision(decision.code.value, tuple(checks), decision.decision_hash, None)
    return GateDecision(GateCode.ALLOWED, tuple(checks), decision.decision_hash, None)
