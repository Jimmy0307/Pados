"""p1 audit events

Revision ID: 20260710_0002
Revises: 20260710_0001
Create Date: 2026-07-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260710_0002"
down_revision: str | None = "20260710_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            entity_type text NOT NULL,
            entity_id uuid NOT NULL,
            event_type text NOT NULL,
            actor text NOT NULL,
            before_state jsonb NOT NULL DEFAULT '{}'::jsonb,
            after_state jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_table("audit_events")

