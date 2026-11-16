# SPDX-License-Identifier: AGPL-3.0-only
"""Local project setup shared by ``pados project create`` and ``POST /control/projects``.

``ensure_local_proof_scope`` creates (idempotently) the initial ScopeVersion of a project through the separate
StateProof authority. It is project setup, not semantic acceptance: the ResultCandidate path only reads this head, and
approval and CAS stay in their existing governed interfaces. Both callers run exactly this function, so the CLI and the
Control Center cannot drift into two setup paths.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def ensure_local_proof_scope(settings: Any, project_id: str) -> int | None:
    """Create the initial ScopeVersion through StateProof for a local project; ``None`` without a proof lane."""
    proof_database = settings.product_database_name
    if not proof_database:
        return None
    if settings.db_host not in {"127.0.0.1", "localhost"}:
        raise PermissionError("PROJECT_PROOF_DATABASE_MUST_BE_LOCAL")
    if proof_database == settings.operational_database_name:
        raise PermissionError("PROJECT_PROOF_DATABASE_MUST_BE_SEPARATE")
    object_root = Path(settings.product_object_root).expanduser()
    if not object_root.is_absolute():
        raise PermissionError("PRODUCT_OBJECT_ROOT_MUST_BE_ABSOLUTE")

    # Late imports: the proof service and its engine are resolved at call time (tests replace them there).
    from pados.config import lane_settings
    from pados.state_proof.object_store import ObjectStore
    from pados.state_proof.service import StateProofService, build_proof_engine

    engine = build_proof_engine(lane_settings(settings, proof_database).database_url)
    try:
        proof = StateProofService(engine, ObjectStore(object_root.resolve()))
        head = proof.ensure_scope(project_id)
        return int(head["current_version"])
    finally:
        engine.dispose()
