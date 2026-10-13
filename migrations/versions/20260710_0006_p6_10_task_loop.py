"""p6.10 deterministic task loop

Revision ID: 20260710_0006
Revises: 20260710_0005
Create Date: 2026-07-11
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260710_0006"
down_revision: str | None = "20260710_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE task_goals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            request_id uuid NOT NULL REFERENCES control_work_requests(id) ON DELETE CASCADE,
            approval_id uuid NOT NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            objective text NOT NULL,
            success_criteria jsonb NOT NULL,
            constraints_json jsonb NOT NULL DEFAULT '[]'::jsonb,
            scope_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            prohibited_actions jsonb NOT NULL DEFAULT '[]'::jsonb,
            risk_level text NOT NULL,
            status text NOT NULL,
            version integer NOT NULL DEFAULT 1,
            content_hash text NOT NULL,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            approved_at timestamptz NULL,
            completed_at timestamptz NULL,
            UNIQUE (request_id, idempotency_key),
            CHECK (version > 0),
            CHECK (content_hash ~ '^[0-9A-Fa-f]{64}$'),
            CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
            CHECK (status IN (
                'DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'ACTIVE', 'COMPLETED',
                'PARTIALLY_COMPLETED', 'BLOCKED', 'FAILED', 'CANCELLED', 'SUPERSEDED'
            ))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE task_plans (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_goal_id uuid NOT NULL REFERENCES task_goals(id) ON DELETE CASCADE,
            approval_id uuid NOT NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            version integer NOT NULL DEFAULT 1,
            status text NOT NULL,
            assumptions jsonb NOT NULL DEFAULT '[]'::jsonb,
            stop_policy jsonb NOT NULL,
            validation_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
            rollback_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
            supersedes_plan_id uuid NULL REFERENCES task_plans(id) ON DELETE SET NULL,
            content_hash text NOT NULL,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            approved_at timestamptz NULL,
            UNIQUE (task_goal_id, version),
            UNIQUE (task_goal_id, idempotency_key),
            CHECK (version > 0),
            CHECK (content_hash ~ '^[0-9A-Fa-f]{64}$'),
            CHECK (status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'ACTIVE', 'COMPLETED', 'FAILED', 'SUPERSEDED', 'CANCELLED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE task_plan_steps (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_plan_id uuid NOT NULL REFERENCES task_plans(id) ON DELETE CASCADE,
            sequence integer NOT NULL,
            title text NOT NULL,
            description text NOT NULL DEFAULT '',
            step_type text NOT NULL,
            required_capability text NOT NULL,
            preferred_worker_types jsonb NOT NULL DEFAULT '[]'::jsonb,
            dependencies jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            allowed_actions jsonb NOT NULL DEFAULT '[]'::jsonb,
            prohibited_actions jsonb NOT NULL DEFAULT '[]'::jsonb,
            context_requirements jsonb NOT NULL DEFAULT '[]'::jsonb,
            input_artifacts jsonb NOT NULL DEFAULT '[]'::jsonb,
            output_contract jsonb NOT NULL DEFAULT '{}'::jsonb,
            validation_policy_id text NOT NULL,
            command_id text NULL,
            success_condition text NOT NULL,
            failure_policy text NOT NULL,
            timeout_seconds integer NOT NULL,
            risk_level text NOT NULL,
            approval_requirement text NOT NULL,
            status text NOT NULL DEFAULT 'PENDING',
            attempt_count integer NOT NULL DEFAULT 0,
            UNIQUE (task_plan_id, sequence),
            CHECK (sequence > 0),
            CHECK (attempt_count >= 0),
            CHECK (timeout_seconds BETWEEN 1 AND 600),
            CHECK (status IN ('PENDING', 'READY', 'ROUTING', 'RUNNING', 'VALIDATING', 'PASSED', 'FAILED', 'BLOCKED', 'SKIPPED', 'CANCELLED', 'TIMED_OUT')),
            CHECK (step_type IN ('INSPECT', 'ANALYZE', 'DRAFT', 'MODIFY_DOCUMENT', 'MODIFY_CODE', 'RUN_VALIDATION', 'BUILD_ARTIFACT', 'AUDIT', 'CHECKPOINT', 'HUMAN_DECISION')),
            CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE task_loops (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_goal_id uuid NOT NULL REFERENCES task_goals(id) ON DELETE CASCADE,
            active_plan_id uuid NOT NULL REFERENCES task_plans(id) ON DELETE RESTRICT,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            request_id uuid NOT NULL REFERENCES control_work_requests(id) ON DELETE CASCADE,
            approval_id uuid NOT NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            status text NOT NULL,
            current_iteration integer NOT NULL DEFAULT 0,
            max_iterations integer NOT NULL,
            max_replans integer NOT NULL DEFAULT 0,
            max_worker_failures integer NOT NULL,
            max_execution_seconds integer NOT NULL,
            no_progress_limit integer NOT NULL,
            context_token_budget integer NOT NULL,
            idempotency_key text NOT NULL,
            started_at timestamptz NULL,
            paused_at timestamptz NULL,
            completed_at timestamptz NULL,
            final_result jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (task_goal_id, idempotency_key),
            CHECK (current_iteration >= 0),
            CHECK (max_iterations > 0),
            CHECK (max_worker_failures > 0),
            CHECK (max_execution_seconds > 0),
            CHECK (no_progress_limit > 0),
            CHECK (context_token_budget > 0),
            CHECK (status IN (
                'DRAFT', 'PENDING_APPROVAL', 'READY', 'RUNNING', 'VALIDATING',
                'WAITING_HUMAN', 'PAUSED', 'BLOCKED', 'COMPLETED', 'PARTIALLY_COMPLETED',
                'FAILED_RECOVERABLE', 'FAILED_TERMINAL', 'CANCELLED', 'TIMED_OUT'
            ))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_task_loops_one_mutable_per_project
        ON task_loops(project_id)
        WHERE status IN ('READY', 'RUNNING', 'VALIDATING', 'WAITING_HUMAN', 'PAUSED', 'FAILED_RECOVERABLE')
        """
    )
    op.execute(
        """
        CREATE TABLE task_loop_iterations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            loop_id uuid NOT NULL REFERENCES task_loops(id) ON DELETE CASCADE,
            iteration_number integer NOT NULL,
            plan_id uuid NOT NULL REFERENCES task_plans(id) ON DELETE RESTRICT,
            step_id uuid NOT NULL REFERENCES task_plan_steps(id) ON DELETE RESTRICT,
            job_id uuid NULL REFERENCES worker_jobs(id) ON DELETE SET NULL,
            status text NOT NULL,
            before_snapshot_id uuid NULL,
            after_snapshot_id uuid NULL,
            progress_delta_id uuid NULL,
            decision text NULL,
            decision_reason text NULL,
            idempotency_key text NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz NULL,
            UNIQUE (loop_id, iteration_number),
            UNIQUE (loop_id, idempotency_key),
            CHECK (iteration_number > 0),
            CHECK (status IN ('CREATED', 'ROUTING', 'RUNNING', 'VALIDATING', 'DECIDED', 'FAILED')),
            CHECK (decision IS NULL OR decision IN (
                'CONTINUE', 'RETRY_SAME_STEP', 'ADVANCE_STEP', 'WAIT_FOR_HUMAN',
                'PAUSE', 'BLOCK', 'COMPLETE', 'PARTIAL_COMPLETE', 'FAIL_RECOVERABLE',
                'FAIL_TERMINAL', 'CANCEL'
            ))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_task_loop_one_active_iteration
        ON task_loop_iterations(loop_id)
        WHERE status IN ('CREATED', 'ROUTING', 'RUNNING', 'VALIDATING')
        """
    )
    op.execute(
        """
        CREATE TABLE task_progress_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            loop_id uuid NOT NULL REFERENCES task_loops(id) ON DELETE CASCADE,
            iteration_id uuid NULL REFERENCES task_loop_iterations(id) ON DELETE CASCADE,
            acceptance_criteria_total integer NOT NULL,
            acceptance_criteria_met integer NOT NULL,
            tests_total integer NOT NULL,
            tests_passed integer NOT NULL,
            tests_failed integer NOT NULL,
            blocker_count integer NOT NULL,
            artifact_count integer NOT NULL,
            validation_status text NOT NULL,
            scope_hash text NOT NULL,
            state_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (acceptance_criteria_total >= 0),
            CHECK (acceptance_criteria_met >= 0),
            CHECK (tests_total >= 0),
            CHECK (tests_passed >= 0),
            CHECK (tests_failed >= 0),
            CHECK (blocker_count >= 0),
            CHECK (artifact_count >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE task_progress_deltas (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            iteration_id uuid NOT NULL REFERENCES task_loop_iterations(id) ON DELETE CASCADE,
            criteria_met_delta integer NOT NULL,
            tests_passed_delta integer NOT NULL,
            tests_failed_delta integer NOT NULL,
            blocker_delta integer NOT NULL,
            artifact_delta integer NOT NULL,
            scope_changed boolean NOT NULL,
            state_changed boolean NOT NULL,
            progress_score integer NOT NULL,
            classification text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (classification IN ('POSITIVE', 'NEUTRAL', 'NEGATIVE', 'INCOMPARABLE'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE task_loop_leases (
            loop_id uuid PRIMARY KEY REFERENCES task_loops(id) ON DELETE CASCADE,
            owner_instance text NOT NULL,
            lease_acquired_at timestamptz NOT NULL DEFAULT now(),
            lease_expires_at timestamptz NOT NULL,
            heartbeat_at timestamptz NOT NULL DEFAULT now(),
            CHECK (lease_expires_at > lease_acquired_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE task_loop_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            loop_id uuid NOT NULL REFERENCES task_loops(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            event_type text NOT NULL,
            event_time timestamptz NOT NULL DEFAULT now(),
            sequence_number bigserial UNIQUE,
            payload_json jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_task_loop_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'task_loop_events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_loop_events_append_only_update
        BEFORE UPDATE ON task_loop_events
        FOR EACH ROW EXECUTE FUNCTION reject_task_loop_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_loop_events_append_only_delete
        BEFORE DELETE ON task_loop_events
        FOR EACH ROW EXECUTE FUNCTION reject_task_loop_event_mutation()
        """
    )
    op.create_index("ix_task_goals_project_status", "task_goals", ["project_id", "status"])
    op.create_index("ix_task_plans_goal_status", "task_plans", ["task_goal_id", "status"])
    op.create_index("ix_task_plan_steps_plan_status", "task_plan_steps", ["task_plan_id", "status"])
    op.create_index("ix_task_loops_project_status", "task_loops", ["project_id", "status"])
    op.create_index("ix_task_loop_events_loop_sequence", "task_loop_events", ["loop_id", "sequence_number"])


def downgrade() -> None:
    op.drop_index("ix_task_loop_events_loop_sequence", table_name="task_loop_events")
    op.drop_index("ix_task_loops_project_status", table_name="task_loops")
    op.drop_index("ix_task_plan_steps_plan_status", table_name="task_plan_steps")
    op.drop_index("ix_task_plans_goal_status", table_name="task_plans")
    op.drop_index("ix_task_goals_project_status", table_name="task_goals")
    op.drop_table("task_loop_events")
    op.execute("DROP FUNCTION IF EXISTS reject_task_loop_event_mutation()")
    op.drop_table("task_loop_leases")
    op.drop_table("task_progress_deltas")
    op.drop_table("task_progress_snapshots")
    op.drop_index("ux_task_loop_one_active_iteration", table_name="task_loop_iterations")
    op.drop_table("task_loop_iterations")
    op.drop_index("ux_task_loops_one_mutable_per_project", table_name="task_loops")
    op.drop_table("task_loops")
    op.drop_table("task_plan_steps")
    op.drop_table("task_plans")
    op.drop_table("task_goals")
