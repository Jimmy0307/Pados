"""Append-only hash chain for P6.19B semantic commit outcomes."""

from alembic import op


revision: str = "p6_21p_0015"
down_revision: str | None = "p6_21h_0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE state_proof_semantic_audit (
            sequence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            scope_id text NOT NULL,
            actor text NOT NULL,
            action text NOT NULL,
            target text NOT NULL,
            reason text NOT NULL,
            created_at timestamptz NOT NULL,
            request_id text NOT NULL,
            idempotency_key text NOT NULL,
            outcome text NOT NULL,
            before_state jsonb NOT NULL,
            after_state jsonb NOT NULL,
            approval_ref text NULL,
            rollback_ref text NULL,
            previous_hash text NULL,
            record_hash text NOT NULL,
            UNIQUE (scope_id, idempotency_key),
            CHECK (scope_id <> ''),
            CHECK (actor <> '' AND action <> '' AND target <> '' AND reason <> ''),
            CHECK (request_id <> '' AND idempotency_key <> '' AND outcome <> ''),
            CHECK (previous_hash IS NULL OR previous_hash ~ '^[0-9a-f]{64}$'),
            CHECK (record_hash ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX ix_state_proof_semantic_audit_scope
            ON state_proof_semantic_audit(scope_id, sequence_id);

        CREATE TABLE state_proof_semantic_audit_heads (
            scope_id text PRIMARY KEY,
            last_sequence_id bigint NULL REFERENCES state_proof_semantic_audit(sequence_id),
            last_hash text NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (last_hash IS NULL OR last_hash ~ '^[0-9a-f]{64}$')
        );

        CREATE OR REPLACE FUNCTION reject_state_proof_semantic_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'IMMUTABLE_STATE_PROOF_AUDIT_RECORD';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_state_proof_semantic_audit_update
            BEFORE UPDATE ON state_proof_semantic_audit
            FOR EACH ROW EXECUTE FUNCTION reject_state_proof_semantic_audit_mutation();
        CREATE TRIGGER trg_state_proof_semantic_audit_delete
            BEFORE DELETE ON state_proof_semantic_audit
            FOR EACH ROW EXECUTE FUNCTION reject_state_proof_semantic_audit_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_state_proof_semantic_audit_delete ON state_proof_semantic_audit;
        DROP TRIGGER IF EXISTS trg_state_proof_semantic_audit_update ON state_proof_semantic_audit;
        DROP FUNCTION IF EXISTS reject_state_proof_semantic_audit_mutation();
        DROP TABLE IF EXISTS state_proof_semantic_audit_heads;
        DROP INDEX IF EXISTS ix_state_proof_semantic_audit_scope;
        DROP TABLE IF EXISTS state_proof_semantic_audit;
        """
    )
