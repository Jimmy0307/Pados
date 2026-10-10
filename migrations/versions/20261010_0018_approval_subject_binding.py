"""Bind control approvals to an immutable WORK_ROUTE subject (RC2 D1).

Adds ``control_approvals.subject_kind`` / ``subject_json``. ``proposal_hash`` is the
canonical hash of the subject; a trigger makes the subject, its hash and the
approval's project immutable once written. Approvals without a subject remain
valid for non-routing uses but can never route work.

Revision ID: 20261010_0018
Revises: 20261007_0017
Create Date: 2026-10-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20261010_0018"
down_revision: str | None = "20261007_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE control_approvals
          ADD COLUMN subject_kind text NULL,
          ADD COLUMN subject_json jsonb NULL,
          ADD CONSTRAINT ck_control_approvals_subject_pair
            CHECK ((subject_kind IS NULL) = (subject_json IS NULL)),
          ADD CONSTRAINT ck_control_approvals_subject_kind
            CHECK (subject_kind IS NULL OR subject_kind IN ('WORK_ROUTE'))
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_control_approval_subject_mutation()
        RETURNS trigger AS $$
        BEGIN
          IF NEW.subject_kind IS DISTINCT FROM OLD.subject_kind
             OR NEW.subject_json IS DISTINCT FROM OLD.subject_json
             OR NEW.proposal_hash IS DISTINCT FROM OLD.proposal_hash
             OR NEW.project_id IS DISTINCT FROM OLD.project_id THEN
            RAISE EXCEPTION 'APPROVAL_SUBJECT_IMMUTABLE' USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_control_approvals_subject_immutable
        BEFORE UPDATE ON control_approvals
        FOR EACH ROW EXECUTE FUNCTION reject_control_approval_subject_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_control_approvals_subject_immutable ON control_approvals")
    op.execute("DROP FUNCTION IF EXISTS reject_control_approval_subject_mutation()")
    op.execute(
        """
        ALTER TABLE control_approvals
          DROP CONSTRAINT IF EXISTS ck_control_approvals_subject_kind,
          DROP CONSTRAINT IF EXISTS ck_control_approvals_subject_pair,
          DROP COLUMN IF EXISTS subject_json,
          DROP COLUMN IF EXISTS subject_kind
        """
    )
