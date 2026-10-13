from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "versions" / "20260711_0007_p6_11_project_goal_loop.py"


def test_p6_11_migration_has_canonical_lineage_and_tables() -> None:
    content = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260711_0007"' in content
    assert 'down_revision: str | None = "20260710_0006"' in content
    for table in (
        "project_goals",
        "project_plans",
        "project_tasks",
        "project_task_dependencies",
        "project_goal_loops",
        "project_goal_cycles",
        "project_progress_snapshots",
        "project_progress_deltas",
        "project_gaps",
        "project_replan_proposals",
        "project_controller_leases",
        "project_goal_events",
    ):
        assert f"CREATE TABLE {table}" in content
        assert f'op.drop_table("{table}")' in content


def test_p6_11_migration_enforces_governance_constraints() -> None:
    content = MIGRATION.read_text(encoding="utf-8")
    for contract in (
        "UNIQUE (project_id, version)",
        "UNIQUE (project_goal_id, version)",
        "UNIQUE (project_plan_id, deduplication_key)",
        "CHECK (project_task_id <> dependency_task_id)",
        "UNIQUE (project_task_id, dependency_task_id, dependency_type)",
        "UNIQUE (project_goal_id, cycle_number)",
        "UNIQUE (project_goal_id, idempotency_key)",
        "CREATE UNIQUE INDEX uq_project_goals_one_active",
        "CREATE UNIQUE INDEX uq_project_plans_one_active",
    ):
        assert contract in content


def test_project_goal_events_are_append_only_and_downgrade_is_scoped() -> None:
    content = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE FUNCTION reject_project_goal_event_mutation" in content
    assert "BEFORE UPDATE OR DELETE ON project_goal_events" in content
    downgrade = content.split("def downgrade() -> None:", maxsplit=1)[1]
    assert "task_goals" not in downgrade
    assert "context_" not in downgrade
