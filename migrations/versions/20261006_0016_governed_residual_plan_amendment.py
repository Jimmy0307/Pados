"""Persist governed product-goal to P6.10 runtime bindings and amendments.

Revision ID: 20261006_0016
Revises: 20261005_0015
Create Date: 2026-10-06
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20261006_0016"
down_revision: str | None = "20261005_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE task_plan_steps ADD CONSTRAINT uq_task_plan_steps_plan_id_id UNIQUE (task_plan_id, id)"
    )
    op.execute(
        """
        CREATE TABLE residual_plan_amendments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            amendment_id text NOT NULL UNIQUE,
            task_id text NOT NULL,
            residual_id text NOT NULL,
            goal_id text NOT NULL,
            parent_goal_id text NOT NULL,
            plan_id uuid NOT NULL REFERENCES task_plans(id) ON DELETE RESTRICT,
            loop_id uuid NOT NULL REFERENCES task_loops(id) ON DELETE RESTRICT,
            parent_step_id uuid NOT NULL,
            step_id uuid NULL,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            project_ref text NOT NULL,
            classification text NOT NULL,
            priority text NOT NULL,
            deadline timestamptz NULL,
            dependencies jsonb NOT NULL DEFAULT '[]'::jsonb,
            required_capability text NOT NULL,
            network_requirement text NOT NULL,
            resource_constraints jsonb NOT NULL DEFAULT '{}'::jsonb,
            authorization_refs jsonb NOT NULL,
            expected_scope_version_ref text NOT NULL,
            provenance jsonb NOT NULL,
            source_result_hash text NOT NULL,
            coverage_map_hash text NOT NULL,
            protocol_version text NOT NULL,
            schema_version text NOT NULL,
            idempotency_key text NOT NULL,
            content_hash text NOT NULL,
            authority_decision text NOT NULL,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (task_id, residual_id, goal_id, plan_id),
            UNIQUE (plan_id, idempotency_key),
            FOREIGN KEY (plan_id, parent_step_id)
                REFERENCES task_plan_steps(task_plan_id, id) ON DELETE RESTRICT,
            FOREIGN KEY (plan_id, step_id)
                REFERENCES task_plan_steps(task_plan_id, id) ON DELETE RESTRICT,
            CHECK (classification IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),
            CHECK (priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')),
            CHECK (network_requirement IN ('NONE', 'REQUIRED')),
            CHECK (source_result_hash ~ '^[0-9a-f]{64}$'),
            CHECK (coverage_map_hash ~ '^[0-9a-f]{64}$'),
            CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            CHECK (authority_decision IN ('AUTO_AMENDABLE', 'REAPPROVAL_REQUIRED')),
            CHECK (protocol_version ~ '^[0-9]+[.][0-9]+$'),
            CHECK (schema_version = 'pados.residual-plan-amendment.v1'),
            CHECK (
                (authority_decision = 'AUTO_AMENDABLE' AND step_id IS NOT NULL)
                OR (authority_decision = 'REAPPROVAL_REQUIRED' AND step_id IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE goal_runtime_bindings (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id text NOT NULL,
            goal_id text NOT NULL,
            residual_id text NULL,
            plan_id uuid NOT NULL REFERENCES task_plans(id) ON DELETE RESTRICT,
            step_id uuid NOT NULL,
            loop_id uuid NULL REFERENCES task_loops(id) ON DELETE RESTRICT,
            amendment_id uuid NULL REFERENCES residual_plan_amendments(id) ON DELETE RESTRICT,
            binding_identity text NOT NULL UNIQUE,
            execution_identity text NOT NULL UNIQUE,
            provenance_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (plan_id, step_id),
            FOREIGN KEY (plan_id, step_id)
                REFERENCES task_plan_steps(task_plan_id, id) ON DELETE RESTRICT,
            CHECK (provenance_hash ~ '^[0-9a-f]{64}$')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_goal_runtime_bindings_task_goal ON goal_runtime_bindings(task_id, goal_id, plan_id)"
    )
    op.execute(
        "CREATE INDEX ix_residual_plan_amendments_loop ON residual_plan_amendments(loop_id, authority_decision)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_residual_plan_amendments_loop")
    op.execute("DROP INDEX IF EXISTS ix_goal_runtime_bindings_task_goal")
    op.execute("DROP TABLE IF EXISTS goal_runtime_bindings")
    op.execute("DROP TABLE IF EXISTS residual_plan_amendments")
    op.execute("ALTER TABLE task_plan_steps DROP CONSTRAINT IF EXISTS uq_task_plan_steps_plan_id_id")
