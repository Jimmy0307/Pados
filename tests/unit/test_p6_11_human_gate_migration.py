from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).parents[2] / "migrations/versions/20260711_0008_p6_11_governance_gates.py"


def test_human_gate_migration_has_governance_contract_and_safe_downgrade() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "project_human_gates", "project_goal_version", "project_plan_version",
        "approval_scope_hash", "condition_hash", "valid_from", "valid_until",
        "requested_by", "resolved_by", "decision_identity", "project_human_gate_id",
    ):
        assert required in source
    for status in ("OPEN", "PENDING_APPROVAL", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED", "SUPERSEDED"):
        assert status in source
    assert "DROP COLUMN IF EXISTS project_human_gate_id" in source
    assert "op.drop_table(\"project_human_gates\")" in source
