from __future__ import annotations

from pathlib import Path


def test_gap_migration_is_scoped_to_0009() -> None:
    source = (Path(__file__).parents[2] / "migrations/versions/20260711_0009_p6_11_gap_replan_governance.py").read_text(encoding="utf-8")
    assert 'revision: str = "20260711_0009"' in source
    assert 'down_revision: str | None = "20260711_0008"' in source
    assert "project_replan_history" not in source
    assert "project_replan_proposals ADD" not in source
    assert "resolution_evidence_ids" in source
    assert "project_human_gate_id" not in source  # owned by accepted 0008
    assert "uq_project_gaps_active_identity" in source
