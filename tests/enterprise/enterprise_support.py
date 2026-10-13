"""Synthetic, public-safe enterprise fixtures. No company, HR or personal data."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from pados.enterprise import observability
from pados.enterprise.authorization import (
    AccessContext,
    CapabilityAuthorization,
    authorize_capability,
)
from pados.enterprise.classification import DataClassification
from pados.enterprise.manifest import (
    ArtifactContract,
    CapabilityHealth,
    CapabilityType,
    EnterpriseCapabilityManifest,
    ExecutionMode,
)
from pados.enterprise.pipeline import KnowledgePipeline, local_pipeline
from pados.enterprise.registry import CapabilityRegistry

TENANT = "tenant-synthetic"
PROJECT = "project-alpha"
OTHER_PROJECT = "project-beta"

DOCS: dict[str, dict[str, Any]] = {
    "handbook": {
        "text": "Synthetic maintenance handbook. The coolant pump inspection interval is every 90 days. "
                "Record the inspection in the maintenance ledger before closing the work order.",
        "classification": DataClassification.INTERNAL, "project": PROJECT,
    },
    "public-faq": {
        "text": "Public product FAQ. The pump model supports standard fittings and a 90 day inspection cycle.",
        "classification": DataClassification.PUBLIC, "project": PROJECT,
    },
    "restricted-memo": {
        "text": "Restricted synthetic memo zebra-lantern-7731. The pump supplier renegotiation target "
                "price is confidential and must never appear in a generated summary for general staff.",
        "classification": DataClassification.RESTRICTED, "project": PROJECT,
        "principals": ("finance-lead",),
    },
    "confidential-ops": {
        "text": "Confidential synthetic operations note: pump inspection backlog is tracked by the ops "
                "team role only and requires the operations role.",
        "classification": DataClassification.CONFIDENTIAL, "project": PROJECT, "roles": ("ops",),
    },
    "beta-doc": {
        "text": "Project beta pump inspection interval notes, isolated from project alpha.",
        "classification": DataClassification.INTERNAL, "project": OTHER_PROJECT,
    },
}


def manifest(capability_id: str = "knowledge.local", capability_type: CapabilityType = CapabilityType.KNOWLEDGE_RETRIEVAL,
             **updates: Any) -> EnterpriseCapabilityManifest:
    values: dict[str, Any] = {
        "capability_id": capability_id, "capability_type": capability_type, "version": "1",
        "supported_inputs": ("text/plain",), "supported_outputs": ("evidence-package",),
        "privacy_classes": tuple(DataClassification), "execution_mode": ExecutionMode.IN_PROCESS,
        "health": CapabilityHealth.READY, "provenance_support": True,
        "artifact_contract": ArtifactContract(output_kind="evidence", schema_ref="pados.enterprise.retrieval.v1"),
    }
    values.update(updates)
    return EnterpriseCapabilityManifest(**values)


def access(principal: str = "analyst", *, project: str = PROJECT, clearance: DataClassification = DataClassification.CONFIDENTIAL,
           roles: frozenset[str] = frozenset()) -> AccessContext:
    return AccessContext(principal_id=principal, session_id=f"session-{principal}", tenant_id=TENANT,
                         project_id=project, clearance=clearance, roles=roles)


def contract(project: str = PROJECT, **extra: Any) -> dict[str, Any]:
    return {"work_contract_id": "wc-enterprise", "project_id": project, "required_capabilities": [], **extra}


def grant(project: str = PROJECT, capabilities: tuple[str, ...] = ("ENTERPRISE:knowledge.local",), **extra: Any) -> dict[str, Any]:
    return {"work_contract_id": "wc-enterprise", "project_id": project, "capabilities": list(capabilities), **extra}


@dataclass
class Env:
    pipeline: KnowledgePipeline
    registry: CapabilityRegistry
    source_ids: dict[str, str]

    def authorize(self, who: AccessContext, **kwargs: Any) -> CapabilityAuthorization:
        return authorize_capability(self.registry, "knowledge.local", contract=contract(who.project_id),
                                    grant=grant(who.project_id), access=who, **kwargs)


@pytest.fixture
def env() -> Env:
    pipeline = local_pipeline()
    registry = CapabilityRegistry()
    registry.register(manifest(), pipeline)
    ids = {}
    for name, doc in DOCS.items():
        report = pipeline.ingest(
            doc["text"].encode(), media_type="text/plain", source_type="synthetic-doc",
            location_ref=f"synthetic://{name}", classification=doc["classification"], tenant_id=TENANT,
            project_id=doc["project"], allowed_principals=doc.get("principals", ()),
            allowed_roles=doc.get("roles", ()), provenance=("synthetic-fixture",),
        )
        ids[name] = report.source.source_id
    return Env(pipeline, registry, ids)


@pytest.fixture
def events() -> Iterator[list[dict[str, Any]]]:
    recorded: list[dict[str, Any]] = []
    remove: Callable[[], None] = observability.add_sink(recorded.append)
    yield recorded
    remove()
