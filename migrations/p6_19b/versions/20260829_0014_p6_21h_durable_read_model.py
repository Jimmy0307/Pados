"""p6.21h rebuildable product read index.

The index stores only stable run identity and pointers to content-addressed
objects. Product semantics remain in P6.19B results, commits, projections,
and immutable ObjectRefs.
"""

from alembic import op


revision: str = "p6_21h_0014"
down_revision: str | None = "p6_19b_0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE p6_21h_product_read_index (
            run_id uuid PRIMARY KEY,
            scope_id text NOT NULL,
            idempotency_key text NOT NULL,
            context_object_id uuid NOT NULL REFERENCES state_proof_objects(id),
            context_hash text NOT NULL,
            latest_snapshot_object_id uuid NULL REFERENCES state_proof_objects(id),
            latest_snapshot_hash text NULL,
            final_report_object_id uuid NULL REFERENCES state_proof_objects(id),
            final_report_hash text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (scope_id <> ''),
            CHECK (idempotency_key <> ''),
            CHECK (context_hash <> ''),
            CHECK (latest_snapshot_hash IS NULL OR latest_snapshot_hash <> ''),
            CHECK (final_report_hash IS NULL OR final_report_hash <> '')
        );
        CREATE INDEX ix_p6_21h_product_read_scope
            ON p6_21h_product_read_index(scope_id, updated_at DESC);
        CREATE INDEX ix_p6_21h_product_read_idempotency
            ON p6_21h_product_read_index(idempotency_key);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS p6_21h_product_read_index")
