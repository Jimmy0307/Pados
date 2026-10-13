"""Readiness validates the operational and proof authority lanes independently.

Needs a disposable operational database at the operational revision, declared in
PADOS_TEST_OPERATIONAL_DATABASE; the default PADOS_DB_NAME is the proof lane.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pados.config import Settings, get_settings
from pados.operations.service import system_health

OPERATIONAL = os.environ.get("PADOS_TEST_OPERATIONAL_DATABASE", "")
pytestmark = pytest.mark.skipif(not OPERATIONAL, reason="DATABASE_INTEGRATION_SUITE: set PADOS_TEST_OPERATIONAL_DATABASE")


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    base = get_settings()
    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "objects").mkdir(exist_ok=True)
    values = {
        "operational_database_name": OPERATIONAL,
        "product_database_name": base.db_name,
        "product_workspace_root": str(tmp_path / "workspace"),
        "product_object_root": str(tmp_path / "objects"),
        "api_host": "127.0.0.1",
        **overrides,
    }
    return base.model_copy(update=values)


def test_separate_lanes_are_each_validated(tmp_path: Path) -> None:
    health = system_health(_settings(tmp_path))
    dims = health["dimensions"]
    for lane in ("operational", "proof"):
        for key in ("configured", "reachable", "authenticated", "migrations_current", "usable"):
            assert dims[f"{lane}_{key}"] is True, (lane, key)
    assert health["lanes"]["operational"]["proof_revisions"] == []
    assert health["ready"] is (dims["provider_configured"] is True)


def test_proof_lane_in_operational_database_is_rejected(tmp_path: Path) -> None:
    health = system_health(_settings(tmp_path, product_database_name=OPERATIONAL))
    assert "PROOF_DB_NOT_SEPARATE" in health["reasons"] and health["ready"] is False


def test_operational_lane_carrying_proof_migrations_is_stale(tmp_path: Path) -> None:
    proof = get_settings().db_name
    health = system_health(_settings(tmp_path, operational_database_name=proof, product_database_name=OPERATIONAL))
    assert "OPERATIONAL_MIGRATION_STALE" in health["reasons"] and health["ready"] is False


def test_unconfigured_or_unreachable_proof_lane_fails_closed(tmp_path: Path) -> None:
    assert "PROOF_DB_UNCONFIGURED" in system_health(_settings(tmp_path, product_database_name=""))["reasons"]
    missing = system_health(_settings(tmp_path, product_database_name="pados_v1_absent_proof_db"))
    assert "PROOF_DB_UNAVAILABLE" in missing["reasons"]
    assert missing["dimensions"]["operational_usable"] is True


def test_missing_object_root_blocks_readiness(tmp_path: Path) -> None:
    health = system_health(_settings(tmp_path, product_object_root=str(tmp_path / "absent")))
    assert "OBJECT_ROOT_UNUSABLE" in health["reasons"] and health["ready"] is False
