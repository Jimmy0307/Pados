# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded authorization primitives for the first-product surface.

Scope discipline for P6.21M:

* Authentication is the existing local control-plane control token. It proves a
  trusted local caller and nothing else.
* Resource authorization is a bounded *scope* consistency rule built on the
  scope identifier that the frozen P6.21H read index already stores per run.
* No user identity, organization, tenancy, or role concept is introduced here,
  and none may be inferred from anything in this module.

AUTHENTICATION != SEMANTIC AUTHORITY. Nothing in this module grants acceptance,
promotion, or commit authority.
"""

from __future__ import annotations

from pathlib import Path

PRODUCT_SCOPE_HEADER = "X-PADOS-Product-Scope"

AUTHENTICATION_FAILED = "PRODUCT_AUTHENTICATION_FAILED"
RESOURCE_SCOPE_REQUIRED = "RESOURCE_SCOPE_REQUIRED"
RESOURCE_SCOPE_MISMATCH = "RESOURCE_SCOPE_MISMATCH"
INVALID_WORKSPACE_SCOPE = "INVALID_WORKSPACE_SCOPE"
INVALID_WORK_CONTRACT = "INVALID_WORK_CONTRACT"
PRODUCT_REQUEST_REJECTED = "PRODUCT_REQUEST_REJECTED"
PRODUCT_RUN_NOT_FOUND = "PRODUCT_RUN_NOT_FOUND"

MAX_SCOPE_ID_LENGTH = 160


class ProductAuthorizationError(PermissionError):
    """Rejection carrying a stable, public, implementation-free error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_scope_id(value: str | None) -> str | None:
    """Return a comparable scope identifier, or None when none was declared."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_SCOPE_ID_LENGTH:
        return None
    return candidate


def require_declared_scope(value: str | None) -> str:
    """Require an explicit bounded resource scope on the request itself."""
    declared = normalize_scope_id(value)
    if declared is None:
        raise ProductAuthorizationError(RESOURCE_SCOPE_REQUIRED)
    return declared


def assert_scope_matches(*, declared: str | None, actual: str | None) -> str:
    """Verify server-side that the addressed resource belongs to the declared scope.

    Knowing a run identifier is never sufficient: the caller must declare the
    scope, and the declared value must equal the scope recorded for that run in
    the durable state substrate.
    """
    requested = require_declared_scope(declared)
    recorded = normalize_scope_id(actual)
    if recorded is None or requested != recorded:
        raise ProductAuthorizationError(RESOURCE_SCOPE_MISMATCH)
    return requested


def resolve_approved_workspace_root(requested: str, approved_root: Path | str) -> Path:
    """Canonicalize a caller-supplied workspace root against the governed root.

    The caller may not select absolute directories, sibling worktrees, parent
    directories, drive-relative or traversal-equivalent spellings, or any other
    root than the single server-configured approved workspace. This is an early
    defense-in-depth gate; the Direct Maker keeps its own independent check.
    """
    approved = _canonical(approved_root, code=INVALID_WORKSPACE_SCOPE)
    candidate_value = (requested or "").strip()
    if not candidate_value:
        raise ProductAuthorizationError(INVALID_WORKSPACE_SCOPE)
    if "\x00" in candidate_value:
        raise ProductAuthorizationError(INVALID_WORKSPACE_SCOPE)
    try:
        candidate = Path(candidate_value).expanduser()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ProductAuthorizationError(INVALID_WORKSPACE_SCOPE) from exc
    if not candidate.is_absolute():
        # A relative or drive-relative root would resolve against server state,
        # which would make the effective root caller-influenced.
        raise ProductAuthorizationError(INVALID_WORKSPACE_SCOPE)
    resolved = _canonical(candidate, code=INVALID_WORKSPACE_SCOPE)
    if resolved != approved:
        raise ProductAuthorizationError(INVALID_WORKSPACE_SCOPE)
    return resolved


def _canonical(value: Path | str, *, code: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ProductAuthorizationError(code) from exc
