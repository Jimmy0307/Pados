"""p6.9 governed worker router

Revision ID: 20260710_0005
Revises: 20260710_0004
Create Date: 2026-07-11
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260710_0005"
down_revision: str | None = "20260710_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        ALTER TABLE control_work_requests
        DROP CONSTRAINT IF EXISTS control_work_requests_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE control_work_requests
        ADD COLUMN IF NOT EXISTS approval_id uuid NULL REFERENCES control_approvals(id) ON DELETE SET NULL,
        ADD COLUMN IF NOT EXISTS capability_id text NULL,
        ADD COLUMN IF NOT EXISTS risk_level text NOT NULL DEFAULT 'LOW',
        ADD COLUMN IF NOT EXISTS context_packet_id uuid NULL REFERENCES context_packets(id) ON DELETE SET NULL,
        ADD COLUMN IF NOT EXISTS scope_hash text NULL,
        ADD CONSTRAINT control_work_requests_status_check CHECK (status IN (
            'PENDING_WORKER_ROUTER',
            'READY_FOR_ROUTING',
            'ROUTING',
            'ROUTED',
            'QUEUED',
            'RUNNING',
            'VALIDATING',
            'SUCCEEDED',
            'FAILED',
            'BLOCKED',
            'CANCELLED',
            'TIMED_OUT',
            'SUPERSEDED'
        )),
        ADD CONSTRAINT control_work_requests_risk_level_check CHECK (risk_level IN (
            'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'
        ))
        """
    )
    op.execute(
        """
        CREATE TABLE worker_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            request_id uuid NOT NULL REFERENCES control_work_requests(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            worker_id text NOT NULL,
            worker_type text NOT NULL,
            status text NOT NULL,
            capability_id text NOT NULL,
            context_packet_id uuid NULL REFERENCES context_packets(id) ON DELETE SET NULL,
            approval_id uuid NOT NULL REFERENCES control_approvals(id) ON DELETE RESTRICT,
            working_directory text NOT NULL,
            isolation_type text NOT NULL,
            attempt_number integer NOT NULL DEFAULT 1,
            timeout_seconds integer NOT NULL,
            queued_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz NULL,
            completed_at timestamptz NULL,
            exit_code integer NULL,
            result_summary text NULL,
            error_code text NULL,
            validation_status text NOT NULL DEFAULT 'NOT_RUN',
            routing_explanation text NOT NULL,
            idempotency_key text NOT NULL,
            route_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            usage_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (request_id, idempotency_key),
            CHECK (attempt_number = 1),
            CHECK (timeout_seconds BETWEEN 1 AND 600),
            CHECK (worker_type IN (
                'DETERMINISTIC_TOOL', 'OLLAMA', 'CLAUDE_CODE_RUNTIME', 'CODEX_CLI'
            )),
            CHECK (status IN (
                'READY_FOR_ROUTING', 'ROUTING', 'ROUTED', 'QUEUED', 'RUNNING', 'VALIDATING',
                'SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED', 'TIMED_OUT'
            )),
            CHECK (isolation_type IN ('READ_ONLY', 'TEMP_DIRECTORY', 'GIT_WORKTREE')),
            CHECK (validation_status IN ('NOT_RUN', 'PASSED', 'FAILED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE worker_job_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id uuid NOT NULL REFERENCES worker_jobs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            event_type text NOT NULL,
            event_time timestamptz NOT NULL DEFAULT now(),
            sequence_number bigserial UNIQUE,
            payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            CHECK (event_type IN (
                'JOB_CREATED',
                'ROUTING_STARTED',
                'WORKER_SELECTED',
                'WORKER_UNAVAILABLE',
                'JOB_QUEUED',
                'JOB_STARTED',
                'CONTEXT_MATERIALIZED',
                'PROCESS_STARTED',
                'OUTPUT_CAPTURED',
                'ARTIFACT_REGISTERED',
                'DIFF_CAPTURED',
                'VALIDATION_STARTED',
                'VALIDATION_PASSED',
                'VALIDATION_FAILED',
                'JOB_SUCCEEDED',
                'JOB_FAILED',
                'JOB_TIMED_OUT',
                'JOB_CANCELLED'
            ))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE worker_job_artifacts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id uuid NOT NULL REFERENCES worker_jobs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            artifact_type text NOT NULL,
            path text NOT NULL,
            sha256 text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (sha256 ~ '^[0-9A-Fa-f]{64}$')
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_worker_job_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'worker_job_events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_worker_job_events_append_only_update
        BEFORE UPDATE ON worker_job_events
        FOR EACH ROW EXECUTE FUNCTION reject_worker_job_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_worker_job_events_append_only_delete
        BEFORE DELETE ON worker_job_events
        FOR EACH ROW EXECUTE FUNCTION reject_worker_job_event_mutation()
        """
    )
    op.create_index("ix_worker_jobs_project_status", "worker_jobs", ["project_id", "status"])
    op.create_index("ix_worker_jobs_request", "worker_jobs", ["request_id"])
    op.create_index("ix_worker_job_events_job_time", "worker_job_events", ["job_id", "event_time"])
    op.create_index("ix_worker_job_events_project_sequence", "worker_job_events", ["project_id", "sequence_number"])


def downgrade() -> None:
    op.drop_index("ix_worker_job_events_project_sequence", table_name="worker_job_events")
    op.drop_index("ix_worker_job_events_job_time", table_name="worker_job_events")
    op.drop_index("ix_worker_jobs_request", table_name="worker_jobs")
    op.drop_index("ix_worker_jobs_project_status", table_name="worker_jobs")
    op.drop_table("worker_job_artifacts")
    op.drop_table("worker_job_events")
    op.drop_table("worker_jobs")
    op.execute("DROP FUNCTION IF EXISTS reject_worker_job_event_mutation()")
    op.execute(
        """
        ALTER TABLE control_work_requests
        DROP CONSTRAINT IF EXISTS control_work_requests_risk_level_check,
        DROP CONSTRAINT IF EXISTS control_work_requests_status_check,
        DROP COLUMN IF EXISTS scope_hash,
        DROP COLUMN IF EXISTS context_packet_id,
        DROP COLUMN IF EXISTS risk_level,
        DROP COLUMN IF EXISTS capability_id,
        DROP COLUMN IF EXISTS approval_id,
        ADD CONSTRAINT control_work_requests_status_check CHECK (status IN (
            'PENDING_WORKER_ROUTER',
            'CANCELLED',
            'SUPERSEDED'
        ))
        """
    )
