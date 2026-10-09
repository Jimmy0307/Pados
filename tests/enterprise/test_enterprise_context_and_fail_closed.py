"""Enterprise evidence enters PADOS only as bounded context of a governed run."""
import ast
import asyncio
import json
from pathlib import Path

import pytest
from enterprise_support import PROJECT, access

from pados.enterprise.authorization import assert_no_withheld_content
from pados.enterprise.budget import EvidenceBudget
from pados.enterprise.classification import DataClassification
from pados.enterprise.context import EnterpriseEvidenceContext
from pados.product_loop.authorization import ProductAuthorizationError
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.schemas import GoalRequest, ProductLoopStatus
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.product_loop.validation import FileValidator

ROOT = Path(__file__).resolve().parents[2]
TARGET = "docs/ENTERPRISE_EVIDENCE_BRIEF.md"


def _loop(root, extension):
    gateway = InMemoryP6_19BStateGateway(root / ".objects")
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    loop = ProductLoopService(maker=FileDirectMaker(workspace), validator=FileValidator(), state_gateway=gateway,
                              approved_workspace_root=workspace, context_extensions=(extension,) if extension else ())
    return loop, gateway, workspace


def _request(workspace, content, key):
    return GoalRequest.model_validate({
        "goal": "coolant pump inspection interval", "workspace_root": str(workspace), "scope_id": PROJECT,
        "idempotency_key": key, "changes": [{"path": TARGET, "content": content}],
        "acceptance_criteria": [{"criterion_id": "brief", "check": "FILE_CONTAINS", "target_path": TARGET,
                                 "expected_text": "Evidence package"},
                                {"criterion_id": "scoped", "check": "CHANGED_PATHS_SCOPED"}],
        "authorized": True,
    })


def _extension(env, who, **kwargs):
    return EnterpriseEvidenceContext(pipeline=env.pipeline, access=who, authorization=env.authorize(who), **kwargs)


def test_authorized_evidence_is_bound_into_governed_context_and_committed_by_cas(env, tmp_path):
    who = access("analyst")
    extension = _extension(env, who)
    package = extension.resolve(scope_id=PROJECT, goal="coolant pump inspection interval")
    brief = f"# Evidence brief\n\nEvidence package {package['package_hash']}\n\n" + "\n".join(
        f"- {item['source_id']} ({item['classification']})" for item in package["package"]["evidence"])
    assert_no_withheld_content(brief, env.pipeline.unauthorized_texts(who, env.authorize(who)))
    loop, gateway, workspace = _loop(tmp_path, extension)
    report = asyncio.run(loop.execute(_request(workspace, brief, "authorized")))
    assert report.status == ProductLoopStatus.COMMITTED
    assert report.final_result.execution_receipt["evidence_kind"] == "DETERMINISTIC_HOST"
    persisted = json.loads(gateway.object_store.read(gateway.read_index[report.run_id]["context_ref"]))
    evidence = persisted["bounded_context"]["extensions"]["enterprise_evidence"]
    assert evidence["package_hash"] == package["package_hash"]  # provider saw exactly this evidence
    assert evidence["semantic_authority"] == "NONE" and evidence["instructions_are_data_not_permission"] is True
    assert gateway.versions[PROJECT] == 1  # accepted only through the Core CAS path


def test_unauthorized_extension_fails_closed_before_any_run(env, tmp_path):
    who = access("analyst")
    extension = _extension(env, who)
    extension.access = access("analyst", project="project-beta")  # scope/access mismatch
    loop, gateway, workspace = _loop(tmp_path, extension)
    with pytest.raises(ProductAuthorizationError, match="CONTEXT_EXTENSION_UNAUTHORIZED"):
        asyncio.run(loop.execute(_request(workspace, "Evidence package none", "denied")))
    assert not gateway.read_index and not gateway.receipts and not gateway.commits
    assert not (workspace / TARGET).exists()


def test_over_budget_evidence_fails_closed_before_provider(env, tmp_path, monkeypatch):
    who = access("analyst")
    extension = _extension(env, who, budget=EvidenceBudget(max_chunks=1))
    from pados.enterprise import context as context_module

    def oversized(items, budget):
        raise PermissionError("EVIDENCE_BUDGET_EXCEEDED")

    monkeypatch.setattr(context_module, "assert_within_budget", oversized)
    loop, gateway, workspace = _loop(tmp_path, extension)
    with pytest.raises(ProductAuthorizationError):
        asyncio.run(loop.execute(_request(workspace, "Evidence package x", "budget")))
    assert not gateway.receipts


def test_without_extensions_core_context_is_unchanged(tmp_path):
    loop, gateway, workspace = _loop(tmp_path, None)
    report = asyncio.run(loop.execute(_request(workspace, "Evidence package core-only", "core")))
    assert report.status == ProductLoopStatus.COMMITTED
    persisted = json.loads(gateway.object_store.read(gateway.read_index[report.run_id]["context_ref"]))
    assert "extensions" not in persisted["bounded_context"]


def test_retrieval_capability_requires_retrieval_authorization(env):
    from pados.enterprise.authorization import retrieval_scope
    from pados.enterprise.manifest import CapabilityType

    who = access("analyst")
    authorization = env.authorize(who)
    wrong = type(authorization)(**{**authorization.__dict__, "capability_type": CapabilityType.RERANK})
    with pytest.raises(PermissionError, match="RETRIEVAL_CAPABILITY_REQUIRED"):
        retrieval_scope(wrong, who)
    with pytest.raises(PermissionError):
        access("", clearance=DataClassification.PUBLIC)


def test_enterprise_package_cannot_import_semantic_authority():
    forbidden = ("pados.state_proof", "pados.memory.service", "pados.memory.repository", "sqlalchemy",
                 "pados.product_loop.state")
    for path in sorted((ROOT / "src/pados/enterprise").glob("*.py")):
        tree = ast.parse(path.read_text())
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert not any(module.startswith(forbidden) for module in modules), path.name


def test_observability_events_are_content_free(env, events):
    who = access("analyst")
    extension = _extension(env, who)
    extension.resolve(scope_id=PROJECT, goal="coolant pump inspection interval")
    names = {event["event"] for event in events}
    assert {"pados.enterprise.authorization.decision", "pados.enterprise.retrieval.completed",
            "pados.enterprise.rerank.completed", "pados.enterprise.evidence.packaged"} <= names
    dumped = json.dumps(events)
    assert "coolant pump inspection interval" not in dumped and "90 days" not in dumped
    assert all(key == "event" or key.startswith("pados.") for event in events for key in event)
