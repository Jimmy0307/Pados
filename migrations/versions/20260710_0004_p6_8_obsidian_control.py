"""p6.8 obsidian control surface

Revision ID: 20260710_0004
Revises: 20260710_0003
Create Date: 2026-07-11
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260710_0004"
down_revision: str | None = "20260710_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE control_messages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            body text NOT NULL,
            author text NOT NULL DEFAULT 'human',
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_id, idempotency_key),
            CHECK (length(body) BETWEEN 1 AND 8000)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE control_work_requests (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title text NOT NULL,
            body text NOT NULL,
            status text NOT NULL DEFAULT 'PENDING_WORKER_ROUTER',
            idempotency_key text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_id, idempotency_key),
            CHECK (status IN (
                'PENDING_WORKER_ROUTER',
                'CANCELLED',
                'SUPERSEDED'
            )),
            CHECK (length(title) BETWEEN 1 AND 300),
            CHECK (length(body) BETWEEN 1 AND 8000)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE control_approvals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            proposal_hash text NOT NULL,
            proposal_summary text NOT NULL,
            status text NOT NULL DEFAULT 'PENDING',
            version integer NOT NULL DEFAULT 1,
            idempotency_key text NOT NULL,
            decision_reason text NULL,
            decided_at timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_id, idempotency_key),
            CHECK (proposal_hash ~ '^[0-9A-Fa-f]{64}$'),
            CHECK (version > 0),
            CHECK (status IN (
                'PENDING',
                'APPROVED',
                'REJECTED',
                'REVISION_REQUESTED',
                'EXPIRED',
                'SUPERSEDED',
                'CANCELLED'
            ))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE control_sync_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NULL REFERENCES projects(id) ON DELETE CASCADE,
            event_type text NOT NULL,
            payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            event_time timestamptz NOT NULL DEFAULT now(),
            sequence_number bigserial UNIQUE
        )
        """
    )
    op.create_index("ix_control_messages_project_time", "control_messages", ["project_id", "created_at"])
    op.create_index("ix_control_work_requests_project_status", "control_work_requests", ["project_id", "status"])
    op.create_index("ix_control_approvals_project_status", "control_approvals", ["project_id", "status"])
    op.create_index("ix_control_sync_events_sequence", "control_sync_events", ["sequence_number"])


def downgrade() -> None:
    op.drop_index("ix_control_sync_events_sequence", table_name="control_sync_events")
    op.drop_index("ix_control_approvals_project_status", table_name="control_approvals")
    op.drop_index("ix_control_work_requests_project_status", table_name="control_work_requests")
    op.drop_index("ix_control_messages_project_time", table_name="control_messages")
    op.drop_table("control_sync_events")
    op.drop_table("control_approvals")
    op.drop_table("control_work_requests")
    op.drop_table("control_messages")
