"""Stage 6 required authorization tests."""
import pytest
from enterprise_support import OTHER_PROJECT, PROJECT, access, contract, grant, manifest

from pados.enterprise.authorization import assert_no_withheld_content, authorize_capability
from pados.enterprise.classification import DataClassification
from pados.enterprise.manifest import CapabilityType
from pados.enterprise.registry import CapabilityRegistry
from pados.enterprise.retrieval import RetrievalRequest


def _retrieve(env, who, query="pump inspection interval", **kwargs):
    authorization = env.authorize(who)
    request = RetrievalRequest(project_id=who.project_id, tenant_id=who.tenant_id, principal_id=who.principal_id,
                               session_id=who.session_id, query=query,
                               classification_ceiling=kwargs.pop("ceiling", DataClassification.RESTRICTED), **kwargs)
    return env.pipeline.retrieve(request, who, authorization)


def test_unauthorized_source_excluded(env):
    response = _retrieve(env, access("analyst", clearance=DataClassification.RESTRICTED), query="pump supplier renegotiation")
    sources = {item.source_id for item in response.evidence}
    assert env.source_ids["restricted-memo"] not in sources  # RESTRICTED needs an explicit principal grant
    assert env.source_ids["confidential-ops"] not in sources  # role ACL
    assert response.authorization["withheld"]["SOURCE_ACL_DENIED"] >= 1
    assert all("zebra-lantern" not in item.text for item in response.evidence)


def test_authorized_source_returned(env):
    finance = access("finance-lead", clearance=DataClassification.RESTRICTED)
    response = _retrieve(env, finance, query="pump supplier renegotiation", budget=_budget(restricted=1))
    assert env.source_ids["restricted-memo"] in {item.source_id for item in response.evidence}
    ops = access("operator", roles=frozenset({"ops"}))
    assert env.source_ids["confidential-ops"] in {i.source_id for i in _retrieve(env, ops, query="inspection backlog").evidence}


def test_role_and_project_isolation(env):
    alpha = _retrieve(env, access("analyst"))
    assert env.source_ids["beta-doc"] not in {item.source_id for item in alpha.evidence}
    beta = _retrieve(env, access("analyst", project=OTHER_PROJECT))
    assert {item.source_id for item in beta.evidence} == {env.source_ids["beta-doc"]}
    # An access context for one project cannot use a grant/contract for another.
    with pytest.raises(PermissionError, match="ACCESS_PROJECT_MISMATCH"):
        authorize_capability(env.registry, "knowledge.local", contract=contract(OTHER_PROJECT),
                             grant=grant(OTHER_PROJECT), access=access("analyst", project=PROJECT))
    # Roles are enterprise facts, not PADOS grants: without the ops role the ops note is withheld.
    plain = _retrieve(env, access("operator"), query="inspection backlog")
    assert env.source_ids["confidential-ops"] not in {item.source_id for item in plain.evidence}


def test_restricted_evidence_cannot_leak_through_generated_summaries(env):
    who = access("analyst", clearance=DataClassification.RESTRICTED)
    authorization = env.authorize(who)
    response = _retrieve(env, who, query="pump supplier renegotiation target price")
    presented = " ".join(item.text for item in response.evidence)
    withheld = env.pipeline.unauthorized_texts(who, authorization)
    assert any("zebra-lantern-7731" in text for text in withheld)
    assert_no_withheld_content(presented, withheld)  # the package itself is clean
    leaked = f"Summary: {presented} The pump supplier renegotiation target price is confidential and must never appear"
    with pytest.raises(PermissionError, match="WITHHELD_EVIDENCE_LEAK"):
        assert_no_withheld_content(leaked, withheld)


def test_mcp_tool_permission_denied_by_default(env):
    registry = CapabilityRegistry()
    registry.register(manifest("tool.ticket-lookup", CapabilityType.MCP_TOOL), object())
    who = access("analyst")
    with pytest.raises(PermissionError, match="CAPABILITY_NOT_AUTHORIZED|MCP_CAPABILITY_NOT_AUTHORIZED"):
        authorize_capability(registry, "tool.ticket-lookup", contract=contract(), grant=grant(capabilities=()), access=who)
    with pytest.raises(PermissionError, match="MCP_CAPABILITY_NOT_AUTHORIZED"):
        authorize_capability(registry, "tool.ticket-lookup", contract=contract(),
                             grant=grant(capabilities=("ENTERPRISE:tool.ticket-lookup",)), access=who)
    allowed = authorize_capability(
        registry, "tool.ticket-lookup", contract=contract(), access=who,
        grant=grant(capabilities=("ENTERPRISE:tool.ticket-lookup",), mcp_capabilities=["ENTERPRISE:tool.ticket-lookup"]))
    assert allowed.grant["authorization_source"] == "PADOS_PROJECT_GRANT"


def test_installed_capability_is_not_authorized_capability(env, events):
    who = access("analyst")
    assert env.registry.health("knowledge.local").value == "READY"
    with pytest.raises(PermissionError, match="CAPABILITY_NOT_AUTHORIZED"):
        authorize_capability(env.registry, "knowledge.local", contract=contract(), grant=grant(capabilities=()), access=who)
    with pytest.raises(PermissionError, match="GRANT_WORK_PACKAGE_MISMATCH"):
        authorize_capability(env.registry, "knowledge.local", contract=contract(),
                             grant={**grant(), "work_contract_id": "other"}, access=who)
    # An agent role label is never a grant.
    with pytest.raises(PermissionError):
        authorize_capability(env.registry, "knowledge.local", contract={**contract(), "agent_role": "admin"},
                             grant=grant(capabilities=()), access=access("admin", roles=frozenset({"admin"})))
    denials = [e for e in events if e["event"] == "pados.enterprise.authorization.decision"]
    assert denials and all(e["pados.decision"] == "DENY" for e in denials)


def test_capability_ceiling_and_clearance_bound_retrieval(env):
    internal_only = access("analyst", clearance=DataClassification.INTERNAL)
    response = _retrieve(env, internal_only, query="pump inspection")
    assert {item.classification for item in response.evidence} <= {DataClassification.PUBLIC, DataClassification.INTERNAL}
    public = _retrieve(env, access("analyst"), query="pump inspection", ceiling=DataClassification.PUBLIC)
    assert {item.source_id for item in public.evidence} == {env.source_ids["public-faq"]}


def _budget(restricted=0):
    from pados.enterprise.budget import EvidenceBudget

    return EvidenceBudget(max_restricted_items=restricted)
