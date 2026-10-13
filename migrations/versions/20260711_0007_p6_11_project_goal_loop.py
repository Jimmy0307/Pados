"""p6.11 persistent project goal loop

Revision ID: 20260711_0007
Revises: 20260710_0006
Create Date: 2026-07-11
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260711_0007"
down_revision: str | None = "20260710_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE project_goals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            version integer NOT NULL,
            objective text NOT NULL,
            desired_outcomes jsonb NOT NULL,
            success_criteria jsonb NOT NULL,
            constraints_json jsonb NOT NULL DEFAULT '[]'::jsonb,
            scope_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            prohibited_actions jsonb NOT NULL DEFAULT '[]'::jsonb,
            assumptions jsonb NOT NULL DEFAULT '[]'::jsonb,
            risk_budget jsonb NOT NULL,
            execution_budget jsonb NOT NULL,
            time_horizon_seconds integer NOT NULL,
            planning_horizon text NOT NULL,
            status text NOT NULL,
            content_hash text NOT NULL,
            supersedes_goal_id uuid NULL REFERENCES project_goals(id) ON DELETE SET NULL,
            approval_id uuid NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            approved_at timestamptz NULL,
            activated_at timestamptz NULL,
            completed_at timestamptz NULL,
            UNIQUE (project_id, version),
            UNIQUE (project_id, idempotency_key),
            CHECK (version > 0),
            CHECK (time_horizon_seconds > 0),
            CHECK (content_hash ~ '^[0-9A-Fa-f]{64}$'),
            CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','ACTIVE','AT_RISK','WAITING_HUMAN','ACHIEVED','PARTIALLY_ACHIEVED','BLOCKED','FAILED','CANCELLED','SUPERSEDED'))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_project_goals_one_active ON project_goals(project_id) WHERE status = 'ACTIVE'")
    op.execute(
        """
        CREATE TABLE project_plans (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            version integer NOT NULL,
            status text NOT NULL,
            planning_horizon text NOT NULL,
            strategy_summary text NOT NULL,
            assumptions jsonb NOT NULL DEFAULT '[]'::jsonb,
            priorities jsonb NOT NULL DEFAULT '[]'::jsonb,
            task_graph jsonb NOT NULL DEFAULT '{}'::jsonb,
            dependency_graph jsonb NOT NULL DEFAULT '{}'::jsonb,
            critical_path jsonb NOT NULL DEFAULT '[]'::jsonb,
            review_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
            replan_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
            stop_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
            risk_controls jsonb NOT NULL DEFAULT '{}'::jsonb,
            resource_limits jsonb NOT NULL DEFAULT '{}'::jsonb,
            plan_diff jsonb NOT NULL DEFAULT '{}'::jsonb,
            replan_rationale text NULL,
            supporting_evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            content_hash text NOT NULL,
            supersedes_plan_id uuid NULL REFERENCES project_plans(id) ON DELETE SET NULL,
            approval_id uuid NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            approved_at timestamptz NULL,
            activated_at timestamptz NULL,
            UNIQUE (project_goal_id, version),
            UNIQUE (project_goal_id, idempotency_key),
            CHECK (version > 0),
            CHECK (content_hash ~ '^[0-9A-Fa-f]{64}$'),
            CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','ACTIVE','AT_RISK','COMPLETED','FAILED','CANCELLED','SUPERSEDED'))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_project_plans_one_active ON project_plans(project_goal_id) WHERE status = 'ACTIVE'")
    op.execute(
        """
        CREATE TABLE project_tasks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_plan_id uuid NOT NULL REFERENCES project_plans(id) ON DELETE CASCADE,
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title text NOT NULL,
            objective text NOT NULL,
            success_criteria jsonb NOT NULL,
            constraints_json jsonb NOT NULL DEFAULT '[]'::jsonb,
            scope_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            priority integer NOT NULL,
            risk_level text NOT NULL,
            estimated_cost integer NOT NULL,
            estimated_duration_seconds integer NOT NULL,
            required_capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
            task_goal_template jsonb NOT NULL,
            task_plan_template jsonb NOT NULL,
            status text NOT NULL,
            goal_hash text NOT NULL,
            scope_hash text NOT NULL,
            success_criteria_hash text NOT NULL,
            dependency_hash text NOT NULL,
            deduplication_key text NOT NULL,
            approval_id uuid NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            supersedes_task_id uuid NULL REFERENCES project_tasks(id) ON DELETE SET NULL,
            source_gap_id uuid NULL,
            task_goal_id uuid NULL REFERENCES task_goals(id) ON DELETE RESTRICT,
            task_plan_id uuid NULL REFERENCES task_plans(id) ON DELETE RESTRICT,
            task_loop_id uuid NULL REFERENCES task_loops(id) ON DELETE RESTRICT,
            launch_idempotency_key text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            approved_at timestamptz NULL,
            activated_at timestamptz NULL,
            completed_at timestamptz NULL,
            UNIQUE (project_plan_id, deduplication_key),
            UNIQUE (project_plan_id, launch_idempotency_key),
            CHECK (priority > 0),
            CHECK (estimated_cost >= 0),
            CHECK (estimated_duration_seconds > 0),
            CHECK (risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL')),
            CHECK (status IN ('PROPOSED','PENDING_APPROVAL','APPROVED','READY','ACTIVE','COMPLETED','PARTIALLY_COMPLETED','BLOCKED','FAILED','CANCELLED','SUPERSEDED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_task_dependencies (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_plan_id uuid NOT NULL REFERENCES project_plans(id) ON DELETE CASCADE,
            project_task_id uuid NOT NULL REFERENCES project_tasks(id) ON DELETE CASCADE,
            dependency_task_id uuid NOT NULL REFERENCES project_tasks(id) ON DELETE RESTRICT,
            dependency_type text NOT NULL,
            dependency_condition jsonb NOT NULL DEFAULT '{}'::jsonb,
            critical_path boolean NOT NULL DEFAULT false,
            partial_policy_approval_id uuid NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            blocked_reason text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_task_id, dependency_task_id, dependency_type),
            CHECK (project_task_id <> dependency_task_id),
            CHECK (dependency_type IN ('REQUIRES_COMPLETION','REQUIRES_ARTIFACT','REQUIRES_APPROVAL','REQUIRES_VALIDATION','OPTIONAL'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_goal_loops (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            active_plan_id uuid NOT NULL REFERENCES project_plans(id) ON DELETE RESTRICT,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            status text NOT NULL,
            current_cycle integer NOT NULL DEFAULT 0,
            stop_policy jsonb NOT NULL,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_goal_id, idempotency_key),
            CHECK (current_cycle >= 0),
            CHECK (status IN ('DRAFT','PENDING_APPROVAL','READY','ACTIVE','EVALUATING','PLANNING','WAITING_HUMAN','PAUSED','AT_RISK','BLOCKED','ACHIEVED','PARTIALLY_ACHIEVED','FAILED_RECOVERABLE','FAILED_TERMINAL','CANCELLED','TIMED_OUT'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_goal_cycles (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_goal_loop_id uuid NOT NULL REFERENCES project_goal_loops(id) ON DELETE CASCADE,
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            cycle_number integer NOT NULL,
            status text NOT NULL,
            checkpoint text NOT NULL,
            decision text NULL,
            decision_reason text NULL,
            input_hash text NOT NULL,
            idempotency_key text NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz NULL,
            UNIQUE (project_goal_id, cycle_number),
            UNIQUE (project_goal_id, idempotency_key),
            CHECK (cycle_number > 0),
            CHECK (status IN ('CREATED','RUNNING','DECIDED','COMPLETED','FAILED','WAITING_HUMAN'))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_project_cycles_one_active ON project_goal_cycles(project_goal_id) WHERE status IN ('CREATED','RUNNING','DECIDED')")
    op.execute(
        """
        CREATE TABLE project_progress_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            project_plan_id uuid NOT NULL REFERENCES project_plans(id) ON DELETE RESTRICT,
            project_goal_cycle_id uuid NULL REFERENCES project_goal_cycles(id) ON DELETE SET NULL,
            metrics jsonb NOT NULL,
            scope_hash text NOT NULL,
            state_hash text NOT NULL,
            evidence_hash text NOT NULL,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_goal_id, idempotency_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_progress_deltas (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            before_snapshot_id uuid NOT NULL REFERENCES project_progress_snapshots(id) ON DELETE RESTRICT,
            after_snapshot_id uuid NOT NULL REFERENCES project_progress_snapshots(id) ON DELETE RESTRICT,
            metrics jsonb NOT NULL,
            classification text NOT NULL,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_goal_id, idempotency_key),
            CHECK (before_snapshot_id <> after_snapshot_id),
            CHECK (classification IN ('POSITIVE','NEUTRAL','NEGATIVE','INCOMPARABLE'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_gaps (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            project_plan_id uuid NOT NULL REFERENCES project_plans(id) ON DELETE RESTRICT,
            success_criterion_id text NOT NULL,
            gap_type text NOT NULL,
            description text NOT NULL,
            required_state jsonb NOT NULL,
            observed_state jsonb NOT NULL,
            evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            severity text NOT NULL,
            confidence numeric(5,4) NOT NULL,
            identity_hash text NOT NULL,
            status text NOT NULL,
            waiver_approval_id uuid NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz NULL,
            UNIQUE (project_goal_id, identity_hash),
            CHECK (confidence >= 0 AND confidence <= 1),
            CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVING','RESOLVED','WAIVED','SUPERSEDED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_replan_proposals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            current_plan_id uuid NOT NULL REFERENCES project_plans(id) ON DELETE RESTRICT,
            proposed_plan_version integer NOT NULL,
            trigger text NOT NULL,
            evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            gap_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            proposal_payload jsonb NOT NULL,
            plan_diff jsonb NOT NULL,
            rationale text NOT NULL,
            fallback_option text NOT NULL,
            plan_hash text NOT NULL,
            inverse_plan_hash text NULL,
            task_set_hash text NOT NULL,
            dependency_hash text NOT NULL,
            priority_hash text NOT NULL,
            status text NOT NULL,
            approval_id uuid NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            approved_at timestamptz NULL,
            rejected_at timestamptz NULL,
            UNIQUE (project_goal_id, idempotency_key),
            CHECK (proposed_plan_version > 0),
            CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','REJECTED','EXPIRED','SUPERSEDED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_controller_leases (
            project_goal_id uuid PRIMARY KEY REFERENCES project_goals(id) ON DELETE CASCADE,
            owner_instance text NOT NULL,
            lease_acquired_at timestamptz NOT NULL DEFAULT now(),
            lease_expires_at timestamptz NOT NULL,
            heartbeat_at timestamptz NOT NULL DEFAULT now(),
            controller_cycle_id uuid NULL REFERENCES project_goal_cycles(id) ON DELETE SET NULL,
            CHECK (lease_expires_at > lease_acquired_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE project_goal_events (
            sequence_number bigserial PRIMARY KEY,
            id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            project_goal_id uuid NOT NULL REFERENCES project_goals(id) ON DELETE CASCADE,
            project_goal_loop_id uuid NULL REFERENCES project_goal_loops(id) ON DELETE SET NULL,
            project_goal_cycle_id uuid NULL REFERENCES project_goal_cycles(id) ON DELETE SET NULL,
            event_type text NOT NULL,
            payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            event_time timestamptz NOT NULL DEFAULT now(),
            idempotency_key text NOT NULL,
            UNIQUE (project_goal_id, idempotency_key)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_project_goal_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'project_goal_events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_goal_events_append_only
        BEFORE UPDATE OR DELETE ON project_goal_events
        FOR EACH ROW EXECUTE FUNCTION reject_project_goal_event_mutation()
        """
    )
    for statement in (
        "CREATE INDEX ix_project_tasks_goal_status ON project_tasks(project_goal_id, status)",
        "CREATE INDEX ix_project_dependencies_plan ON project_task_dependencies(project_plan_id)",
        "CREATE INDEX ix_project_cycles_loop ON project_goal_cycles(project_goal_loop_id, cycle_number)",
        "CREATE INDEX ix_project_gaps_goal_status ON project_gaps(project_goal_id, status)",
        "CREATE INDEX ix_project_replans_goal_status ON project_replan_proposals(project_goal_id, status)",
        "CREATE INDEX ix_project_events_goal_sequence ON project_goal_events(project_goal_id, sequence_number)",
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS project_goal_events_append_only ON project_goal_events")
    op.execute("DROP FUNCTION IF EXISTS reject_project_goal_event_mutation()")
    op.drop_table("project_goal_events")
    op.drop_table("project_controller_leases")
    op.drop_table("project_replan_proposals")
    op.drop_table("project_gaps")
    op.drop_table("project_progress_deltas")
    op.drop_table("project_progress_snapshots")
    op.drop_table("project_goal_cycles")
    op.drop_table("project_goal_loops")
    op.drop_table("project_task_dependencies")
    op.drop_table("project_tasks")
    op.drop_table("project_plans")
    op.drop_table("project_goals")
