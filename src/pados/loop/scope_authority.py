# SPDX-License-Identifier: AGPL-3.0-only
"""Read the current ScopeVersion through the separate StateProof authority."""
from __future__ import annotations

from typing import Any

from sqlalchemy.exc import SQLAlchemyError


def read_current_scope_version(settings: Any, project_id: str) -> int | None:
    """Read, but never create, a scope head from the configured proof database."""
    if settings is None:
        return None
    product_database_name = str(getattr(settings, "product_database_name", ""))
    operational_database_name = str(getattr(settings, "operational_database_name", ""))
    if (
        not product_database_name
        or product_database_name == operational_database_name
        or str(getattr(settings, "db_host", "")) not in {"localhost", "127.0.0.1"}
    ):
        return None

    from pados.config import lane_settings
    from pados.state_proof.service import build_proof_engine, read_scope_version

    engine = build_proof_engine(lane_settings(settings, product_database_name).database_url)
    try:
        try:
            return read_scope_version(engine, project_id)
        except SQLAlchemyError:
            return None
    finally:
        engine.dispose()
