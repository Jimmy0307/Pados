"""p6.19b bounded immutable result and semantic commit proof.

This proof-only migration is intentionally outside the shared PADOS Alembic
graph. It adds the disposable proof records without changing the accepted
P6.15/P6.16 two-head compatibility contract.
"""

from alembic import op


revision: str = "p6_19b_0013"
down_revision: str | None = "p6_20b_0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE state_proof_objects (
            id uuid PRIMARY KEY,
            content_hash text NOT NULL UNIQUE,
            size_bytes bigint NOT NULL,
            media_type text NOT NULL,
            storage_uri text NOT NULL,
            object_kind text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (size_bytes >= 0),
            CHECK (content_hash <> ''),
            CHECK (storage_uri <> ''),
            CHECK (object_kind IN ('INPUT_MANIFEST', 'RESULT_PAYLOAD', 'OTHER'))
        );
        CREATE INDEX ix_state_proof_objects_kind_hash
            ON state_proof_objects(object_kind, content_hash);

        CREATE TABLE state_proof_scope_heads (
            scope_id text PRIMARY KEY,
            current_version integer NOT NULL DEFAULT 0,
            current_commit_id uuid NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (current_version >= 0)
        );

        CREATE TABLE state_proof_results (
            id uuid PRIMARY KEY,
            operation_id text NOT NULL,
            attempt integer NOT NULL DEFAULT 1,
            operation_type text NOT NULL,
            run_id uuid NULL,
            correlation_id text NULL,
            scope_id text NOT NULL,
            input_scope_version integer NOT NULL,
            input_manifest_hash text NOT NULL,
            input_manifest_object_id uuid NOT NULL REFERENCES state_proof_objects(id),
            context_fingerprint text NULL,
            context_receipt_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            provider_id text NULL,
            runtime_id text NULL,
            model_id text NULL,
            contract_id text NULL,
            determinism_class text NOT NULL DEFAULT 'NDETERMINISTIC',
            output_object_id uuid NULL REFERENCES state_proof_objects(id),
            status text NOT NULL,
            parsed_payload_json jsonb NULL,
            failure_classification text NULL,
            duration_ms integer NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(operation_id, attempt),
            CHECK (attempt > 0),
            CHECK (input_scope_version >= 0),
            CHECK (determinism_class IN ('DETERMINISTIC', 'NDETERMINISTIC', 'UNKNOWN')),
            CHECK (status IN ('SUCCEEDED', 'INVALID_SCHEMA', 'FAILED')),
            CHECK (duration_ms IS NULL OR duration_ms >= 0)
        );
        CREATE INDEX ix_state_proof_results_scope_created
            ON state_proof_results(scope_id, created_at DESC);
        CREATE INDEX ix_state_proof_results_operation
            ON state_proof_results(operation_id, attempt);

        CREATE TABLE state_proof_commits (
            id uuid PRIMARY KEY,
            scope_id text NOT NULL,
            expected_parent_version integer NOT NULL,
            result_id uuid NOT NULL REFERENCES state_proof_results(id),
            candidate_ref text NOT NULL,
            new_version integer NULL,
            status text NOT NULL,
            actor_type text NOT NULL DEFAULT 'SYSTEM',
            gate text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(scope_id, result_id),
            CHECK (expected_parent_version >= 0),
            CHECK (new_version IS NULL OR new_version > expected_parent_version),
            CHECK (status IN ('COMMITTED', 'STALE_CANDIDATE', 'ALREADY_APPLIED')),
            CHECK (actor_type <> '')
        );
        CREATE INDEX ix_state_proof_commits_scope_version
            ON state_proof_commits(scope_id, new_version DESC);

        CREATE TABLE state_proof_scope_versions (
            id uuid PRIMARY KEY,
            scope_id text NOT NULL,
            version integer NOT NULL,
            parent_version integer NULL,
            commit_id uuid NULL REFERENCES state_proof_commits(id),
            manifest_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(scope_id, version),
            CHECK (version >= 0),
            CHECK (parent_version IS NULL OR parent_version >= 0),
            CHECK (parent_version IS NULL OR version = parent_version + 1)
        );
        CREATE INDEX ix_state_proof_scope_versions_scope
            ON state_proof_scope_versions(scope_id, version DESC);

        CREATE TABLE state_proof_projections (
            id uuid PRIMARY KEY,
            scope_id text NOT NULL,
            scope_version integer NOT NULL,
            result_id uuid NOT NULL REFERENCES state_proof_results(id),
            projection_kind text NOT NULL,
            title text NOT NULL,
            content text NOT NULL,
            projection_metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            projection_contract_id text NOT NULL,
            status text NOT NULL DEFAULT 'MATERIALIZED',
            created_at timestamptz NOT NULL DEFAULT now(),
            invalidated_at timestamptz NULL,
            CHECK (projection_kind IN ('OUTPUT', 'LIVING_SYNTHESIS')),
            CHECK (status IN ('MATERIALIZED', 'INVALIDATED')),
            CHECK (title <> ''),
            CHECK (projection_contract_id <> '')
        );
        CREATE INDEX ix_state_proof_projections_source
            ON state_proof_projections(scope_id, scope_version, result_id);

        CREATE OR REPLACE FUNCTION reject_state_proof_immutable_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'IMMUTABLE_STATE_PROOF_RECORD:%', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER state_proof_objects_immutable
            BEFORE UPDATE OR DELETE ON state_proof_objects
            FOR EACH ROW EXECUTE FUNCTION reject_state_proof_immutable_mutation();
        CREATE TRIGGER state_proof_results_immutable
            BEFORE UPDATE OR DELETE ON state_proof_results
            FOR EACH ROW EXECUTE FUNCTION reject_state_proof_immutable_mutation();
        CREATE TRIGGER state_proof_commits_immutable
            BEFORE UPDATE OR DELETE ON state_proof_commits
            FOR EACH ROW EXECUTE FUNCTION reject_state_proof_immutable_mutation();
        CREATE TRIGGER state_proof_scope_versions_immutable
            BEFORE UPDATE OR DELETE ON state_proof_scope_versions
            FOR EACH ROW EXECUTE FUNCTION reject_state_proof_immutable_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS state_proof_projections")
    op.execute("DROP TABLE IF EXISTS state_proof_scope_versions")
    op.execute("DROP TABLE IF EXISTS state_proof_commits")
    op.execute("DROP TABLE IF EXISTS state_proof_results")
    op.execute("DROP TABLE IF EXISTS state_proof_scope_heads")
    op.execute("DROP TABLE IF EXISTS state_proof_objects")
    op.execute("DROP FUNCTION IF EXISTS reject_state_proof_immutable_mutation()")
