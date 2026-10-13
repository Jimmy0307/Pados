"""Raise the bounded work-request payload ceiling for complete task envelopes.

Revision ID: 20261005_0015
Revises: 20260930_0014
Create Date: 2026-10-05
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20261005_0015"
down_revision: str | None = "20260930_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE control_work_requests
        DROP CONSTRAINT IF EXISTS control_work_requests_body_check
        """
    )
    op.execute(
        """
        ALTER TABLE control_work_requests
        ADD CONSTRAINT control_work_requests_body_check
        CHECK (length(body) BETWEEN 1 AND 65536)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM control_work_requests WHERE length(body) > 8000
            ) THEN
                RAISE EXCEPTION 'TASK_ENVELOPE_ROWS_EXCEED_PREVIOUS_BODY_LIMIT';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE control_work_requests
        DROP CONSTRAINT IF EXISTS control_work_requests_body_check
        """
    )
    op.execute(
        """
        ALTER TABLE control_work_requests
        ADD CONSTRAINT control_work_requests_body_check
        CHECK (length(body) BETWEEN 1 AND 8000)
        """
    )
