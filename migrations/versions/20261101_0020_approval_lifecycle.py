"""WORK_ROUTE approval lifecycle: expiry and single-loop consumption (RC2 SEC-4).

Revision ID: 20261101_0020
Revises: 20261010_0019
Create Date: 2026-11-01

``expires_at`` is set when a WORK_ROUTE approval is approved; NULL (approvals decided before this revision, and
subject-less approvals) never expires here. ``consumed_by_loop_id`` binds a WORK_ROUTE approval to the one TaskLoop
created from it and can never be moved to another loop. Revocation uses the existing ``CANCELLED`` status.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20261101_0020"
down_revision: str | None = "20261010_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE control_approvals ADD COLUMN expires_at timestamptz NULL")
    op.execute("ALTER TABLE control_approvals ADD COLUMN consumed_by_loop_id uuid NULL")
    op.execute(
        """
        ALTER TABLE control_approvals ADD CONSTRAINT ck_control_approvals_lifecycle_work_route_only
        CHECK ((expires_at IS NULL AND consumed_by_loop_id IS NULL) OR subject_kind = 'WORK_ROUTE')
        """
    )
    op.execute(
        """
        CREATE FUNCTION control_approvals_consumption_write_once() RETURNS trigger AS $$
        BEGIN
          IF OLD.consumed_by_loop_id IS NOT NULL
             AND NEW.consumed_by_loop_id IS DISTINCT FROM OLD.consumed_by_loop_id THEN
            RAISE EXCEPTION 'APPROVAL_CONSUMED: a consumed approval cannot be moved to another loop';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_control_approvals_consumption_write_once
        BEFORE UPDATE ON control_approvals
        FOR EACH ROW EXECUTE FUNCTION control_approvals_consumption_write_once()
        """
    )
    # Bind existing WORK_ROUTE approvals to their loop when exactly one loop uses them; shared legacy approvals stay
    # unbound (and keep working for those loops) until a new loop consumes them.
    op.execute(
        """
        UPDATE control_approvals a SET consumed_by_loop_id = only_loop.loop_id
        FROM (SELECT approval_id, min(id::text)::uuid AS loop_id FROM task_loops GROUP BY approval_id
              HAVING count(*) = 1) only_loop
        WHERE only_loop.approval_id = a.id AND a.subject_kind = 'WORK_ROUTE'
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_control_approvals_consumption_write_once ON control_approvals")
    op.execute("DROP FUNCTION IF EXISTS control_approvals_consumption_write_once()")
    op.execute("ALTER TABLE control_approvals DROP CONSTRAINT IF EXISTS ck_control_approvals_lifecycle_work_route_only")
    op.execute("ALTER TABLE control_approvals DROP COLUMN IF EXISTS consumed_by_loop_id")
    op.execute("ALTER TABLE control_approvals DROP COLUMN IF EXISTS expires_at")
