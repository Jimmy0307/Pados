"""PADOS core contracts for the Knowledge adapter and Skills/MCP, with local dummies.

These prove the core contract only. Enterprise capability depth is an
extension and is intentionally not exercised here.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pados.execution.authorization import authorize_execution_capabilities
from pados.execution.handoff import task_envelope
from pados.knowledge.adapters import ADAPTER_BOUNDARIES, KnowledgeLibraryProfile
from pados.knowledge.identity import source_identity_from_bytes

ROOT = Path(__file__).resolve().parents[2]
DUMMY_SKILL = "skill:pados-local-echo"
DUMMY_MCP = "local-dummy.read"
SECRET_REF = "secretref://local-dummy/api-reference"
SEMANTIC_AUTHORITY_MODULES = ("pados.state_proof", "pados.memory.service", "sqlalchemy")


def _imports(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


# --- Knowledge Platform Adapter -------------------------------------------------------------

def test_adapters_are_registered_with_declared_capability_status() -> None:
    names = [boundary.name for boundary in ADAPTER_BOUNDARIES]
    assert len(names) == len(set(names))
    assert {boundary.status for boundary in ADAPTER_BOUNDARIES} <= {"IMPLEMENTED", "BLOCKED", "UNVERIFIED"}
    for boundary in ADAPTER_BOUNDARIES:
        if boundary.status == "IMPLEMENTED":
            assert callable(boundary.resolve())


def test_unavailable_adapters_fail_closed_without_pretend_implementation() -> None:
    blocked = [boundary for boundary in ADAPTER_BOUNDARIES if boundary.status == "BLOCKED"]
    assert blocked
    for boundary in blocked:
        assert boundary.module is None and boundary.symbol is None
        with pytest.raises(RuntimeError, match="_UNAVAILABLE"):
            boundary.resolve()


def test_knowledge_input_is_bounded_scoped_and_carries_content_provenance() -> None:
    profile = KnowledgeLibraryProfile("dummy-library", "project-a", max_source_bytes=16)
    content = b"bounded source"
    identity = source_identity_from_bytes(content)
    profile.assert_source_scope(project_id="project-a", byte_size=identity.byte_size)
    assert identity.source_id.startswith("sha256:") and identity.byte_size == len(content)
    with pytest.raises(PermissionError, match="KNOWLEDGE_PROJECT_SCOPE_MISMATCH"):
        profile.assert_source_scope(project_id="project-b", byte_size=1)
    with pytest.raises(ValueError, match="KNOWLEDGE_SOURCE_BUDGET_EXCEEDED"):
        profile.assert_source_scope(project_id="project-a", byte_size=17)
    with pytest.raises(PermissionError):
        KnowledgeLibraryProfile("dummy-library", "project-a", private_evidence_may_leave_process=True)


def test_knowledge_adapter_cannot_promote_semantic_state() -> None:
    for relative in ("src/pados/knowledge/adapters.py", "src/pados/knowledge/identity.py"):
        assert not any(module.startswith(SEMANTIC_AUTHORITY_MODULES) for module in _imports(relative))


# --- Skills / MCP ---------------------------------------------------------------------------

def _contract(**extra: object) -> dict[str, object]:
    return {"work_contract_id": "contract-dummy", "project_id": "project-a", "required_capabilities": ["MAKE"],
            "skill_ids": [DUMMY_SKILL], "mcp_capabilities": [DUMMY_MCP], "secret_refs": [SECRET_REF], **extra}


def _grant(**extra: object) -> dict[str, object]:
    return {"work_contract_id": "contract-dummy", "project_id": "project-a", "capabilities": ["MAKE"],
            "skills": [DUMMY_SKILL], "mcp_capabilities": [DUMMY_MCP], "secret_refs": [SECRET_REF],
            "approval_ref": "approval-dummy", **extra}


def _authorize(contract: dict, grant: dict, *, installed=frozenset({DUMMY_SKILL}),
               registered=frozenset({DUMMY_MCP}), role: str | None = None) -> dict:
    return authorize_execution_capabilities(contract, grant, installed_skills=installed,
                                            registered_mcp_capabilities=registered, agent_role=role)


def test_approved_dummy_skill_and_mcp_are_authorized_only_as_references() -> None:
    authorized = _authorize(_contract(), _grant())
    assert authorized["skill_ids"] == [DUMMY_SKILL]
    assert authorized["mcp_capabilities"] == [DUMMY_MCP]
    assert authorized["secret_refs"] == [SECRET_REF]
    assert authorized["authorization_source"] == "PADOS_PROJECT_GRANT"


@pytest.mark.parametrize(("grant_update", "error"), [
    ({"skills": []}, "SKILL_NOT_AUTHORIZED"),
    ({"mcp_capabilities": []}, "MCP_CAPABILITY_NOT_AUTHORIZED"),
    ({"capabilities": []}, "CAPABILITY_NOT_AUTHORIZED"),
    ({"project_id": "project-b"}, "GRANT_SCOPE_MISMATCH"),
    ({"work_contract_id": "other"}, "GRANT_WORK_PACKAGE_MISMATCH"),
    ({"secret_refs": []}, "SECRET_REFERENCE_REQUIRED"),
])
def test_installed_or_registered_is_not_authorized(grant_update: dict, error: str) -> None:
    with pytest.raises(PermissionError, match=error):
        _authorize(_contract(), _grant(**grant_update), role="administrator")


def test_granted_but_not_installed_or_registered_is_denied() -> None:
    with pytest.raises(PermissionError, match="SKILL_NOT_REGISTERED"):
        _authorize(_contract(), _grant(), installed=frozenset())
    with pytest.raises(PermissionError, match="MCP_CAPABILITY_NOT_REGISTERED"):
        _authorize(_contract(), _grant(), registered=frozenset())


def test_secretref_stays_unresolved_and_literal_secrets_are_rejected() -> None:
    authorized = _authorize(_contract(), _grant())
    assert all(ref.startswith("secretref://") for ref in authorized["secret_refs"])
    literal = "plain-secret-value"
    with pytest.raises(PermissionError, match="SECRET_REFERENCE_REQUIRED"):
        _authorize(_contract(secret_refs=[literal]), _grant(secret_refs=[literal]))
    assert literal not in json.dumps(authorized)


def test_execution_boundary_cannot_mutate_semantic_authority() -> None:
    for relative in ("src/pados/execution/authorization.py", "src/pados/execution/handoff.py"):
        assert not any(module.startswith(SEMANTIC_AUTHORITY_MODULES) for module in _imports(relative))
    contract = _contract(state="AUTHORIZED", scope={"isolated": True, "allowed_paths": ["a.py"], "workspace_root": "/approved"},
                         changes=[{"path": "a.py"}], allowed_actions=["WRITE_APPROVED_WORKSPACE"],
                         context_fingerprint="fp", semantic_base_scope_version=0, human_gate_required=False,
                         retry_policy={"max_attempts": 1})
    envelope = task_envelope(contract, execution_grant=_grant(), installed_skills=frozenset({DUMMY_SKILL}),
                             registered_mcp_capabilities=frozenset({DUMMY_MCP}))
    assert envelope["secret_refs"] == [SECRET_REF]
    assert "semantic_commit" not in json.dumps(envelope).casefold()
