# SPDX-License-Identifier: AGPL-3.0-only
"""Enterprise authorization adapter.

PADOS remains the only authorization authority: capability use is granted by
the existing project-bound grant (``authorize_execution_capabilities``).
Enterprise ACL/RBAC facts (principals/roles attached to a source by its
system of record, the caller's clearance from an identity adapter) are
*inputs* evaluated per request; PADOS persists no second ACL store.

Two stages:
1. retrieval authorization -> coarse eligibility (tenant, project,
   classification ceiling, source filters) applied by backends before scoring;
2. presentation/execution authorization -> fine per-source enforcement before
   any evidence is shown, reranked by a capability, or placed in context.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pados.enterprise import observability
from pados.enterprise.classification import DataClassification, lowest
from pados.enterprise.lineage import EnterpriseSource
from pados.enterprise.manifest import CapabilityHealth, CapabilityType, EnterpriseCapabilityManifest
from pados.enterprise.registry import CapabilityRegistry
from pados.execution.authorization import authorize_execution_capabilities


@dataclass(frozen=True)
class AccessContext:
    """Caller identity as asserted by an enterprise identity adapter."""

    principal_id: str
    session_id: str
    tenant_id: str
    project_id: str
    clearance: DataClassification
    roles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not all((self.principal_id, self.session_id, self.tenant_id, self.project_id)):
            raise PermissionError("ACCESS_CONTEXT_INCOMPLETE")

    @property
    def principal_ref(self) -> str:
        """Stable non-identifying reference for logs."""
        import hashlib

        return "principal:" + hashlib.sha256(self.principal_id.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CapabilityAuthorization:
    capability_id: str
    capability_type: CapabilityType
    project_id: str
    tenant_id: str
    ceiling: DataClassification
    grant: Mapping[str, Any]


def authorize_capability(
    registry: CapabilityRegistry,
    capability_id: str,
    *,
    contract: Mapping[str, Any],
    grant: Mapping[str, Any],
    access: AccessContext,
    request_ceiling: DataClassification | None = None,
) -> CapabilityAuthorization:
    """Authorize one capability for one governed work contract; fail closed."""
    try:
        manifest = registry.get(capability_id)
        health = registry.health(capability_id)
        if health is not CapabilityHealth.READY:
            raise PermissionError(f"CAPABILITY_{health.value}")
        if str(contract.get("project_id") or "") != access.project_id:
            raise PermissionError("ACCESS_PROJECT_MISMATCH")
        scoped = _contract_for(manifest, contract)
        granted = authorize_execution_capabilities(
            scoped, grant, installed_skills=frozenset(),
            registered_mcp_capabilities=registry.mcp_capabilities(),
        )
        ceilings = [access.clearance, manifest.max_classification]
        if request_ceiling is not None:
            ceilings.append(request_ceiling)
        decision = CapabilityAuthorization(
            capability_id=capability_id, capability_type=manifest.capability_type,
            project_id=access.project_id, tenant_id=access.tenant_id,
            ceiling=lowest(*ceilings), grant=granted,
        )
    except PermissionError as exc:
        observability.emit("authorization.decision", capability_id=capability_id, decision="DENY",
                           reason=str(exc), project_id=access.project_id, principal_ref=access.principal_ref)
        raise
    observability.emit("authorization.decision", capability_id=capability_id, decision="ALLOW",
                       classification=decision.ceiling.value, project_id=access.project_id,
                       principal_ref=access.principal_ref)
    return decision


def _contract_for(manifest: EnterpriseCapabilityManifest, contract: Mapping[str, Any]) -> dict[str, Any]:
    scoped = dict(contract)
    if manifest.capability_type is CapabilityType.MCP_TOOL:
        scoped["mcp_capabilities"] = sorted({*contract.get("mcp_capabilities", ()), manifest.grant_capability})
    scoped["required_capabilities"] = sorted({*contract.get("required_capabilities", ()),
                                              manifest.grant_capability})
    scoped["secret_refs"] = sorted({*contract.get("secret_refs", ()), *manifest.secret_refs})
    return scoped


@dataclass(frozen=True)
class RetrievalScope:
    """Coarse eligibility, applied by every backend before scoring."""

    tenant_id: str
    project_id: str
    ceiling: DataClassification
    source_ids: frozenset[str] = frozenset()
    source_types: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()

    def admits(self, source: EnterpriseSource, *node_ids: str) -> bool:
        return (source.tenant_id == self.tenant_id and source.project_id == self.project_id
                and self.ceiling.permits(source.classification)
                and (not self.source_ids or source.source_id in self.source_ids)
                and (not self.source_types or source.source_type in self.source_types)
                and not ({source.source_id, *node_ids} & self.excluded))


def retrieval_scope(
    authorization: CapabilityAuthorization, access: AccessContext, *,
    source_ids: Iterable[str] = (), source_types: Iterable[str] = (), excluded: Iterable[str] = (),
) -> RetrievalScope:
    if authorization.capability_type is not CapabilityType.KNOWLEDGE_RETRIEVAL:
        raise PermissionError("RETRIEVAL_CAPABILITY_REQUIRED")
    if (authorization.project_id, authorization.tenant_id) != (access.project_id, access.tenant_id):
        raise PermissionError("ACCESS_SCOPE_MISMATCH")
    return RetrievalScope(access.tenant_id, access.project_id, authorization.ceiling,
                          frozenset(source_ids), frozenset(source_types), frozenset(excluded))


@dataclass
class PresentationDecision:
    presented: list[Any] = field(default_factory=list)
    withheld: dict[str, int] = field(default_factory=dict)
    withheld_texts: list[str] = field(default_factory=list)

    def withhold(self, reason: str, text: str) -> None:
        self.withheld[reason] = self.withheld.get(reason, 0) + 1
        self.withheld_texts.append(text)


def presentation_filter(items: Iterable[Any], access: AccessContext, scope: RetrievalScope) -> PresentationDecision:
    """Fine-grained enforcement per evidence item (``item.source``/``item.text``)."""
    decision = PresentationDecision()
    for item in items:
        source: EnterpriseSource | None = getattr(item, "source", None)
        text = str(getattr(item, "text", ""))
        if source is None or not source.source_id:
            decision.withhold("SOURCE_IDENTITY_MISSING", text)
        elif not scope.admits(source, str(getattr(item, "chunk_id", ""))):
            decision.withhold("OUTSIDE_RETRIEVAL_SCOPE", text)
        elif not access.clearance.permits(source.classification):
            decision.withhold("CLASSIFICATION_ABOVE_CLEARANCE", text)
        elif not _source_acl_allows(source, access):
            decision.withhold("SOURCE_ACL_DENIED", text)
        else:
            decision.presented.append(item)
    return decision


def _source_acl_allows(source: EnterpriseSource, access: AccessContext) -> bool:
    if not source.allowed_principals and not source.allowed_roles:
        # No source ACL: project/tenant eligibility applies, except that
        # RESTRICTED data always needs an explicit principal or role grant.
        return source.classification is not DataClassification.RESTRICTED
    return access.principal_id in source.allowed_principals or bool(access.roles & source.allowed_roles)


_WORD = re.compile(r"[\w-]+", re.UNICODE)


def assert_no_withheld_content(output: str, withheld_texts: Iterable[str], *, shingle: int = 6) -> None:
    """Reject generated output that reproduces withheld evidence.

    Uses word shingles so paraphrase-free copying of any ``shingle``-word run
    from withheld text is detected; the check never logs either text.
    """
    produced = _shingles(output, shingle)
    if not produced:
        return
    for text in withheld_texts:
        if produced & _shingles(text, shingle):
            raise PermissionError("WITHHELD_EVIDENCE_LEAK")


def _shingles(text: str, size: int) -> set[tuple[str, ...]]:
    words = [word.casefold() for word in _WORD.findall(text)]
    return {tuple(words[index:index + size]) for index in range(max(0, len(words) - size + 1))}
