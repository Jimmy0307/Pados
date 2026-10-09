"""p6.11 governance gates

Revision ID: 20260711_0008
Revises: 20260711_0007
Create Date: 2026-07-11
"""
from collections.abc import Sequence

from alembic import op


revision: str = "20260711_0008"
down_revision: str | None = "20260711_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE project_human_gates (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            project_goal_version integer NOT NULL,
            project_plan_id uuid NULL REFERENCES project_plans(id) ON DELETE SET NULL,
            project_plan_version integer NULL,
            project_task_id uuid NULL REFERENCES project_tasks(id) ON DELETE SET NULL,
            gate_type text NOT NULL,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            risk_delta integer NOT NULL DEFAULT 0,
            scope_delta jsonb NOT NULL DEFAULT '[]'::jsonb,
            budget_delta integer NOT NULL DEFAULT 0,
            time_horizon_delta integer NOT NULL DEFAULT 0,
            required_action text NOT NULL,
            supporting_evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence_hash text NOT NULL DEFAULT '',
            condition_hash text NOT NULL DEFAULT '',
            valid_from timestamptz NOT NULL DEFAULT now(),
            valid_until timestamptz NULL,
            requested_by text NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            status text NOT NULL DEFAULT 'PENDING_APPROVAL',
            approval_id uuid NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            decision_identity text NULL,
            decision text NULL,
            decision_reason text NULL,
            resolved_by text NULL,
            approval_scope_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz NULL,
            idempotency_key text NOT NULL,
            UNIQUE (project_goal_id, idempotency_key),
            CHECK (gate_type IN ('RISK_BUDGET','RISK_BUDGET_INCREASE','SCOPE_EXPANSION','EXTERNAL_SIDE_EFFECT','PRODUCTION_MODIFICATION',
              'RELEASE','DEPLOYMENT','DELETION','SECRET_OPERATION','EXTERNAL_MIGRATION',
              'NEW_WORKER_CAPABILITY','NEW_EXTERNAL_SYSTEM','GOAL_REVISION_REQUIRED','GAP_WAIVER',
              'PARTIAL_ACHIEVEMENT')),
            CHECK (status IN ('OPEN','PENDING_APPROVAL','APPROVED','REJECTED','EXPIRED','CANCELLED','SUPERSEDED'))
        )
        """
    )
    op.execute("CREATE INDEX ix_project_human_gates_goal_status ON project_human_gates(project_goal_id, status)")
    op.execute("CREATE INDEX ix_project_human_gates_scope_hash ON project_human_gates(approval_scope_hash)")
    op.execute("CREATE INDEX ix_project_human_gates_active_identity ON project_human_gates(project_id, project_goal_id, project_plan_id, project_task_id, gate_type, approval_scope_hash, condition_hash) WHERE status IN ('OPEN', 'PENDING_APPROVAL')")
    op.execute("ALTER TABLE project_gaps ADD COLUMN project_human_gate_id uuid NULL REFERENCES project_human_gates(id) ON DELETE SET NULL")
    op.execute("CREATE INDEX ix_project_gaps_human_gate ON project_gaps(project_human_gate_id)")


def downgrade() -> None:
    op.execute("ALTER TABLE project_gaps DROP COLUMN IF EXISTS project_human_gate_id")
    op.drop_table("project_human_gates")
