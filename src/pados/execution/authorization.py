# SPDX-License-Identifier: AGPL-3.0-only
"""Capability grants for bounded worker handoffs.

Registration and discovery describe availability. Only a project-bound PADOS
grant authorizes a capability for a specific governed Work Contract.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import Any

_SECRET_REF = re.compile(r"^secretref://[a-zA-Z0-9_-]{1,80}/[a-zA-Z0-9._/-]{1,160}$")


def authorize_execution_capabilities(
    contract: Mapping[str, Any],
    grant: Mapping[str, Any],
    *,
    installed_skills: AbstractSet[str],
    registered_mcp_capabilities: AbstractSet[str],
    agent_role: str | None = None,
) -> dict[str, Any]:
    """Validate the explicit grant and return only the authorized references.

    ``agent_role`` is intentionally ignored: a role label is not an execution
    grant. This function never resolves a SecretRef or returns secret material.
    """
    del agent_role
    work_id = str(contract.get("work_contract_id") or "")
    project_id = str(contract.get("project_id") or "")
    if not work_id or not project_id or grant.get("work_contract_id") != work_id:
        raise PermissionError("GRANT_WORK_PACKAGE_MISMATCH")
    if grant.get("project_id") != project_id:
        raise PermissionError("GRANT_SCOPE_MISMATCH")

    capabilities = _string_set(contract.get("required_capabilities"), "INVALID_CAPABILITY")
    skills = _string_set(contract.get("skill_ids", []), "INVALID_SKILL")
    mcp = _string_set(contract.get("mcp_capabilities", []), "INVALID_MCP_CAPABILITY")
    refs = _string_set(contract.get("secret_refs", []), "SECRET_REFERENCE_REQUIRED")
    granted_capabilities = _string_set(grant.get("capabilities", []), "INVALID_GRANT")
    granted_skills = _string_set(grant.get("skills", []), "INVALID_GRANT")
    granted_mcp = _string_set(grant.get("mcp_capabilities", []), "INVALID_GRANT")
    granted_refs = _string_set(grant.get("secret_refs", []), "INVALID_GRANT")

    if not capabilities <= granted_capabilities:
        raise PermissionError("CAPABILITY_NOT_AUTHORIZED")
    if not skills <= installed_skills:
        raise PermissionError("SKILL_NOT_REGISTERED")
    if not skills <= granted_skills:
        raise PermissionError("SKILL_NOT_AUTHORIZED")
    if not mcp <= registered_mcp_capabilities:
        raise PermissionError("MCP_CAPABILITY_NOT_REGISTERED")
    if not mcp <= granted_mcp:
        raise PermissionError("MCP_CAPABILITY_NOT_AUTHORIZED")
    if not refs <= granted_refs or any(not _valid_secret_ref(ref) for ref in refs):
        raise PermissionError("SECRET_REFERENCE_REQUIRED")

    return {
        "schema_version": "1",
        "work_contract_id": work_id,
        "project_id": project_id,
        "capabilities": sorted(capabilities),
        "skill_ids": sorted(skills),
        "mcp_capabilities": sorted(mcp),
        "secret_refs": sorted(refs),
        "approval_ref": _optional_text(grant.get("approval_ref")),
        "authorization_source": "PADOS_PROJECT_GRANT",
    }


def _string_set(value: Any, error: str) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise PermissionError(error)
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PermissionError(error)
    return {str(item) for item in value}


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _valid_secret_ref(value: str) -> bool:
    if not _SECRET_REF.fullmatch(value):
        return False
    path = value.removeprefix("secretref://").split("/", 1)[1]
    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)
