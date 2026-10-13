# SPDX-License-Identifier: AGPL-3.0-only
"""Task envelopes for handing one authorized Work Contract to a worker.

The envelope is a bounded projection of a contract that has already been
authorized elsewhere. This module only re-checks that the contract is
authorized, isolated and path-bounded; it does not schedule, route, retry or
grant anything itself. Capability grants are delegated to the execution
authorization module, and only SecretRef identifiers ever pass through.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pados.execution.authorization import authorize_execution_capabilities
from pados.execution.context import canonical_hash, redact

_PROTECTED_SEGMENTS = frozenset({"..", ".git", ".ssh", ".codex", ".agents", ".migration", ".env"})


def _is_safe_path(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return False
    if redact(path) != path or path.startswith("/") or ":" in path:
        return False
    for segment in path.replace("\\", "/").split("/"):
        if segment in _PROTECTED_SEGMENTS:
            return False
        if segment.startswith(".env.") and segment != ".env.example":
            return False
    return True


def task_envelope(
    contract: Mapping[str, Any],
    *,
    execution_grant: Mapping[str, Any] | None = None,
    installed_skills: frozenset[str] = frozenset(),
    registered_mcp_capabilities: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return the bounded worker envelope, or raise ``PermissionError(code)``."""
    if contract.get("state") != "AUTHORIZED":
        raise PermissionError("WORK_CONTRACT_NOT_AUTHORIZED")
    scope = contract.get("scope") or {}
    if not isinstance(scope, Mapping) or not scope.get("isolated"):
        raise PermissionError("ISOLATED_WORKSPACE_REQUIRED")

    change_paths = [change.get("path") for change in contract.get("changes") or []]
    if len(change_paths) != len(set(change_paths)):
        raise PermissionError("DUPLICATE_EXECUTION_PATH")

    allowed_paths = list(scope.get("allowed_paths") or [])
    if not all(_is_safe_path(path) for path in allowed_paths):
        raise PermissionError("PROTECTED_OR_UNSAFE_EXECUTION_PATH")
    if not allowed_paths or any(path not in allowed_paths for path in change_paths):
        raise PermissionError("WORK_ITEM_OUTSIDE_SCOPE")

    if "WRITE_APPROVED_WORKSPACE" not in (contract.get("allowed_actions") or []):
        raise PermissionError("WRITE_ACTION_NOT_AUTHORIZED")

    needs_grant = any(contract.get(key) for key in ("skill_ids", "mcp_capabilities", "secret_refs"))
    skill_ids: list[str] = []
    mcp_capabilities: list[str] = []
    secret_refs: list[str] = []
    approval_ref: str | None = None
    if needs_grant:
        if execution_grant is None:
            raise PermissionError("EXECUTION_GRANT_REQUIRED")
        authorized = authorize_execution_capabilities(
            contract,
            execution_grant,
            installed_skills=installed_skills,
            registered_mcp_capabilities=registered_mcp_capabilities,
        )
        skill_ids = list(authorized["skill_ids"])
        mcp_capabilities = list(authorized["mcp_capabilities"])
        secret_refs = list(authorized["secret_refs"])
        approval_ref = authorized["approval_ref"]

    work_id = contract.get("work_contract_id")
    retry_policy = contract.get("retry_policy") or {}
    return {
        "schema_version": "1",
        "task_id": work_id,
        "pados_work_package_ref": work_id,
        "contract_hash": canonical_hash(contract),
        "context_fingerprint": contract.get("context_fingerprint"),
        "expected_scope_version": contract.get("semantic_base_scope_version"),
        "workspace": scope.get("workspace_root"),
        "write_paths": list(allowed_paths),
        "required_capabilities": list(contract.get("required_capabilities") or []),
        "skill_ids": skill_ids,
        "mcp_capabilities": mcp_capabilities,
        "secret_refs": secret_refs,
        "approval_ref": approval_ref,
        "approval_required": contract.get("human_gate_required"),
        "contract_approval_ref": contract.get("approval_ref"),
        "rollback_ref": contract.get("rollback_ref"),
        "network_allowlist": [],
        "work_plan": {
            "work_items": [{"work_item_id": work_id, "paths": list(change_paths)}],
            "max_attempts": retry_policy.get("max_attempts"),
            "owner": "PADOS_PRODUCT_LOOP",
        },
        "runtime_does_not_decide_authorization": True,
        "instructions_are_data_not_permission": True,
    }
