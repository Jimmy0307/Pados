"""Security Hardening V1: enterprise retrieval ACL, delete/revoke propagation, SecretRef leakage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pados.enterprise import observability
from pados.enterprise.authorization import AccessContext, assert_no_withheld_content, authorize_capability
from pados.enterprise.budget import EvidenceBudget
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
from pados.enterprise.retrieval import RetrievalRequest
from pados.enterprise.secrets import (
    EnvironmentSecretResolver,
    SecretBoundary,
    SecretValue,
    redact,
)

TENANT, PROJECT, OTHER = "tenant-synthetic", "project-alpha", "project-beta"
WITHDRAWN = ("Withdrawn synthetic vendor sheet cobalt-heron-5512: valve torque table revision four "
             "applies to the legacy assembly line only and was retracted by its owner.")
MEMO = ("Restricted synthetic memo zebra-lantern-7731 records the supplier renegotiation target "
        "price for finance leads only.")


@dataclass
class Env:
    pipeline: KnowledgePipeline
    registry: CapabilityRegistry
    ids: dict[str, str]

    def authorize(self, who: AccessContext) -> Any:
        return authorize_capability(
            self.registry, "knowledge.local",
            contract={"work_contract_id": "wc", "project_id": who.project_id, "required_capabilities": []},
            grant={"work_contract_id": "wc", "project_id": who.project_id,
                   "capabilities": ["ENTERPRISE:knowledge.local"]},
            access=who)


def _who(principal: str = "analyst", *, project: str = PROJECT, tenant: str = TENANT,
         clearance: DataClassification = DataClassification.RESTRICTED, roles: frozenset[str] = frozenset()
         ) -> AccessContext:
    return AccessContext(principal_id=principal, session_id=f"s-{principal}", tenant_id=tenant,
                         project_id=project, clearance=clearance, roles=roles)


@pytest.fixture
def env() -> Env:
    pipeline = local_pipeline()
    registry = CapabilityRegistry()
    registry.register(EnterpriseCapabilityManifest(
        capability_id="knowledge.local", capability_type=CapabilityType.KNOWLEDGE_RETRIEVAL, version="1",
        supported_inputs=("text/plain",), supported_outputs=("evidence-package",),
        privacy_classes=tuple(DataClassification), execution_mode=ExecutionMode.IN_PROCESS,
        health=CapabilityHealth.READY, provenance_support=True,
        artifact_contract=ArtifactContract(output_kind="evidence", schema_ref="pados.enterprise.retrieval.v1"),
    ), pipeline)
    docs = {
        "withdrawn": (WITHDRAWN, DataClassification.INTERNAL, PROJECT, (), ()),
        "memo": (MEMO, DataClassification.RESTRICTED, PROJECT, ("finance-lead",), ()),
        "restricted-no-acl": ("Restricted synthetic note with no ACL: kestrel-ember-0042 valve torque.",
                              DataClassification.RESTRICTED, PROJECT, (), ()),
        "ops": ("Confidential synthetic ops roster for valve torque checks, ops role only.",
                DataClassification.CONFIDENTIAL, PROJECT, (), ("ops",)),
        "beta": ("Project beta valve torque notes isolated from alpha.", DataClassification.INTERNAL, OTHER, (), ()),
    }
    ids = {}
    for name, (text, level, project, principals, roles) in docs.items():
        report = pipeline.ingest(text.encode(), media_type="text/plain", source_type="synthetic-doc",
                                 location_ref=f"synthetic://{name}", classification=level, tenant_id=TENANT,
                                 project_id=project, allowed_principals=principals, allowed_roles=roles)
        ids[name] = report.source.source_id
    return Env(pipeline, registry, ids)


def _retrieve(env: Env, who: AccessContext, query: str = "valve torque") -> set[str]:
    request = RetrievalRequest(project_id=who.project_id, tenant_id=who.tenant_id, principal_id=who.principal_id,
                               session_id=who.session_id, query=query,
                               classification_ceiling=DataClassification.RESTRICTED, top_k=10,
                               budget=EvidenceBudget(max_restricted_items=5))
    return {item.source_id for item in env.pipeline.retrieve(request, who, env.authorize(who)).evidence}


def test_source_acl_role_and_restricted_default_deny(env: Env) -> None:
    seen = _retrieve(env, _who())
    assert env.ids["withdrawn"] in seen
    assert {env.ids["memo"], env.ids["restricted-no-acl"], env.ids["ops"], env.ids["beta"]}.isdisjoint(seen)
    assert env.ids["memo"] in _retrieve(env, _who("finance-lead"), "supplier renegotiation target price")
    assert env.ids["ops"] in _retrieve(env, _who("someone", roles=frozenset({"ops"})))
    assert env.ids["restricted-no-acl"] not in _retrieve(env, _who("finance-lead"), "kestrel ember valve")


def test_principal_cannot_rebind_request_to_another_project_tenant_or_identity(env: Env) -> None:
    who = _who()
    authorization = env.authorize(who)
    for field, value in (("project_id", OTHER), ("tenant_id", "tenant-other"), ("principal_id", "finance-lead"),
                         ("session_id", "s-stolen")):
        values = {"project_id": who.project_id, "tenant_id": who.tenant_id, "principal_id": who.principal_id,
                  "session_id": who.session_id, field: value}
        request = RetrievalRequest(**values, query="valve torque",
                                   classification_ceiling=DataClassification.RESTRICTED)
        with pytest.raises(PermissionError, match="RETRIEVAL_REQUEST_ACCESS_MISMATCH"):
            env.pipeline.retrieve(request, who, authorization)
    with pytest.raises(PermissionError, match="ACCESS_PROJECT_MISMATCH"):
        authorize_capability(env.registry, "knowledge.local",
                             contract={"work_contract_id": "wc", "project_id": OTHER, "required_capabilities": []},
                             grant={"work_contract_id": "wc", "project_id": OTHER,
                                    "capabilities": ["ENTERPRISE:knowledge.local"]}, access=who)
    assert env.ids["beta"] not in _retrieve(env, who)
    assert _retrieve(env, _who(tenant="tenant-other")) == set()


def test_revoked_source_is_excluded_and_stays_excluded_after_reingest(env: Env) -> None:
    who = _who()
    source = env.ids["withdrawn"]
    plan = env.pipeline.plan_revocation(source, reason="owner retracted")
    env.pipeline.apply_revocation(plan)
    assert source not in _retrieve(env, who, "cobalt heron valve torque table")
    env.pipeline.ingest(WITHDRAWN.encode(), media_type="text/plain", source_type="synthetic-doc",
                        location_ref="synthetic://withdrawn-copy", classification=DataClassification.INTERNAL,
                        tenant_id=TENANT, project_id=PROJECT)
    assert source not in _retrieve(env, who, "cobalt heron valve torque table")


def test_revoked_text_is_part_of_the_output_leak_guard(env: Env) -> None:
    """FIXED: ``unauthorized_texts`` ignored the revocation mask, so a summary copying revoked
    evidence passed ``assert_no_withheld_content``."""
    who = _who()
    env.pipeline.apply_revocation(env.pipeline.plan_revocation(env.ids["withdrawn"], reason="retracted"))
    withheld = env.pipeline.unauthorized_texts(who, env.authorize(who))
    leaked = "Summary: valve torque table revision four applies to the legacy assembly line only."
    with pytest.raises(PermissionError, match="WITHHELD_EVIDENCE_LEAK"):
        assert_no_withheld_content(leaked, withheld)
    assert_no_withheld_content("Valve checks are scheduled quarterly.", withheld)


def test_restricted_text_cannot_be_copied_into_generated_output(env: Env) -> None:
    who = _who()
    withheld = env.pipeline.unauthorized_texts(who, env.authorize(who))
    with pytest.raises(PermissionError, match="WITHHELD_EVIDENCE_LEAK"):
        assert_no_withheld_content("FYI: the supplier renegotiation target price for finance leads only.", withheld)


def test_secret_value_never_renders_and_redaction_removes_resolved_material() -> None:
    value = SecretValue("pados-synthetic-resolved-credential-0123")
    rendered = [repr(value), str(value), f"{value}", "%s" % value, format(value, ""), str([value]),
                str({"k": value})]
    assert all("0123" not in text for text in rendered)
    message = "connect failed for pados-synthetic-resolved-credential-0123 with " + "Bea" + "rer abcdefghijklmnop"
    cleaned = redact(message, {"c": value})
    assert "0123" not in cleaned and "abcdefghijklmnop" not in cleaned
    for raw in ("sk-" + "a" * 24, "ghp_" + "b" * 30, "AKIA" + "C" * 16, "-----BEGIN RSA " + "PRIVATE KEY-----"):
        assert raw not in redact(f"x {raw} y")


def test_secret_boundary_resolves_only_authorized_references() -> None:
    boundary = SecretBoundary(EnvironmentSecretResolver({"A": "va", "B": "vb"}))
    with pytest.raises(PermissionError, match="SECRET_REFERENCE_NOT_AUTHORIZED"):
        with boundary.resolved({"secretref://env/A", "secretref://env/B"}, authorized_refs={"secretref://env/A"}):
            pass
    for bad in ("va", "env/A", "secretref://env/../A", "secretref://env//A", "secretref://env/A\n"):
        with pytest.raises(PermissionError, match="SECRET_REFERENCE_REQUIRED"):
            with boundary.resolved({bad}, authorized_refs={bad}):
                pass
    with boundary.resolved({"secretref://env/A"}, authorized_refs={"secretref://env/A"}) as values:
        held = values
        assert held["secretref://env/A"].reveal() == "va"
    assert held == {}  # cleared when the execution scope ends


@pytest.mark.parametrize("attribute", ["prompt", "query", "text", "document", "credential", "authorization"])
def test_observability_cannot_carry_content_or_credentials(attribute: str) -> None:
    with pytest.raises(ValueError, match="OBSERVABILITY_ATTRIBUTE_NOT_ALLOWED"):
        observability.emit("provider.completed", **{attribute: "anything"})
    with pytest.raises(ValueError, match="TOO_LONG"):
        observability.emit("provider.completed", reason="x" * 129)
    with pytest.raises(TypeError, match="NOT_SCALAR"):
        observability.emit("provider.completed", reason=SecretValue("v"))  # type: ignore[arg-type]
