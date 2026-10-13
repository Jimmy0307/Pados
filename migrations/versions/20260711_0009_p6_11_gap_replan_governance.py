"""p6.11 project gap lifecycle

Revision ID: 20260711_0009
Revises: 20260711_0008
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260711_0009"
down_revision: str | None = "20260711_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0007 used a global identity constraint.  A condition may be resolved and
    # later recur, so only active instances are unique under the governed
    # lifecycle service.
    op.execute("ALTER TABLE project_gaps DROP CONSTRAINT IF EXISTS project_gaps_project_goal_id_identity_hash_key")
    op.execute("ALTER TABLE project_gaps ADD COLUMN project_id uuid NULL REFERENCES projects(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE project_gaps ADD COLUMN project_goal_version integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE project_gaps ADD COLUMN project_plan_version integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE project_gaps ADD COLUMN project_task_id uuid NULL REFERENCES project_tasks(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN required_state_hash text NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN observed_state_hash text NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN condition_hash text NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN resolution_evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE project_gaps ADD COLUMN resolution_state_hash text NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN resolved_by text NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN resolution_reason_code text NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN superseded_by_gap_id uuid NULL REFERENCES project_gaps(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN occurrence_number integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE project_gaps ADD COLUMN reappeared_from_gap_id uuid NULL REFERENCES project_gaps(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE project_gaps ADD COLUMN idempotency_key text NULL")
    op.execute("UPDATE project_gaps g SET project_id = goal.project_id FROM project_goals goal WHERE goal.id = g.project_goal_id")
    op.execute("ALTER TABLE project_gaps ALTER COLUMN project_id SET NOT NULL")
    op.execute("CREATE UNIQUE INDEX uq_project_gaps_active_identity ON project_gaps(project_id, identity_hash) WHERE status IN ('OPEN','ACKNOWLEDGED','RESOLVING')")
    op.execute("CREATE INDEX ix_project_gaps_active_lookup ON project_gaps(project_id, project_goal_id, status, created_at) WHERE status IN ('OPEN','ACKNOWLEDGED','RESOLVING')")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_project_gaps_active_lookup")
    op.execute("DROP INDEX IF EXISTS uq_project_gaps_active_identity")
    for column in ("idempotency_key", "reappeared_from_gap_id", "occurrence_number", "superseded_by_gap_id", "resolution_reason_code", "resolved_by", "resolution_state_hash", "resolution_evidence_ids", "condition_hash", "observed_state_hash", "required_state_hash", "project_task_id", "project_plan_version", "project_goal_version", "project_id"):
        op.execute(f"ALTER TABLE project_gaps DROP COLUMN IF EXISTS {column}")
    op.execute("ALTER TABLE project_gaps ADD CONSTRAINT project_gaps_project_goal_id_identity_hash_key UNIQUE (project_goal_id, identity_hash)")
