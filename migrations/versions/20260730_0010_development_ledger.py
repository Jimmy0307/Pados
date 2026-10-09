"""PADOS development ledger and capability version history.

This migration is intentionally additive.  It creates the development system
of record before the Knowledge Corpus Pilot migration and does not rewrite or
backfill existing operational records.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260730_0010"
down_revision: str | None = "20260711_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


HISTORY_TABLES = (
    "development_change_sets",
    "development_change_items",
    "development_decisions",
    "development_validation_receipts",
    "development_artifacts",
    "capability_versions",
    "capability_changes",
    "development_events",
    "capability_delta_candidates",
    "development_backfill_records",
)


def upgrade() -> None:
    # Capability identity belongs to the ledger and therefore exists before
    # the corpus pilot.  IF NOT EXISTS also permits safe reconstruction when a
    # local fixture already contains the earlier pilot identity table.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_registry (
            capability_id text PRIMARY KEY,
            canonical_name text,
            name text NOT NULL,
            domain text NOT NULL,
            owner text NOT NULL DEFAULT 'UNKNOWN',
            description text NOT NULL,
            authority_level text NOT NULL DEFAULT 'PROPOSED',
            input_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
            output_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
            required_permissions jsonb NOT NULL DEFAULT '[]'::jsonb,
            read_write_classification text NOT NULL DEFAULT 'READ_ONLY',
            backend_route text,
            supported_ui_views jsonb NOT NULL DEFAULT '[]'::jsonb,
            status text NOT NULL DEFAULT 'PROPOSED',
            version text NOT NULL DEFAULT '1.0.0',
            validation_status text NOT NULL DEFAULT 'UNVALIDATED',
            lifecycle_status text NOT NULL DEFAULT 'PROPOSED',
            current_version_id uuid NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_capability_registry_lifecycle ON capability_registry(lifecycle_status, updated_at DESC)")
    # The P6.15 corpus pilot may have created a lightweight identity table.
    # Keep that identity, add governance fields, and store every version separately.
    op.execute("ALTER TABLE capability_registry ADD COLUMN IF NOT EXISTS canonical_name text")
    op.execute("ALTER TABLE capability_registry ADD COLUMN IF NOT EXISTS owner text NOT NULL DEFAULT 'UNKNOWN'")
    op.execute("ALTER TABLE capability_registry ADD COLUMN IF NOT EXISTS authority_level text NOT NULL DEFAULT 'PROPOSED'")
    op.execute("ALTER TABLE capability_registry ADD COLUMN IF NOT EXISTS lifecycle_status text NOT NULL DEFAULT 'PROPOSED'")
    op.execute("UPDATE capability_registry SET canonical_name = capability_id WHERE canonical_name IS NULL")
    op.execute("ALTER TABLE capability_registry ALTER COLUMN canonical_name SET NOT NULL")

    op.execute(
        """
        CREATE TABLE development_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NULL REFERENCES projects(id) ON DELETE SET NULL,
            goal_id uuid NULL REFERENCES project_goals(id) ON DELETE SET NULL,
            task_id uuid NULL,
            title text NOT NULL,
            objective text NOT NULL,
            input_requirement text NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz NULL,
            status text NOT NULL DEFAULT 'IN_PROGRESS',
            executor_type text NOT NULL DEFAULT 'UNKNOWN',
            executor_name text NOT NULL DEFAULT 'UNKNOWN',
            runtime_identity jsonb NOT NULL DEFAULT '{}'::jsonb,
            repository text NULL,
            worktree text NULL,
            branch text NULL,
            starting_commit text NULL,
            ending_commit text NULL,
            instruction_hash text NULL,
            summary text NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_development_sessions_project_time ON development_sessions(project_id, started_at DESC);
        CREATE INDEX ix_development_sessions_status ON development_sessions(status, started_at DESC);

        CREATE TABLE development_change_sets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            development_session_id uuid NOT NULL REFERENCES development_sessions(id) ON DELETE CASCADE,
            repository text NOT NULL,
            branch text NULL,
            base_commit text NULL,
            result_commit text NULL,
            diff_hash text NULL,
            files_added jsonb NOT NULL DEFAULT '[]'::jsonb,
            files_modified jsonb NOT NULL DEFAULT '[]'::jsonb,
            files_deleted jsonb NOT NULL DEFAULT '[]'::jsonb,
            schema_changed boolean NOT NULL DEFAULT false,
            migration_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            api_changed boolean NOT NULL DEFAULT false,
            cli_changed boolean NOT NULL DEFAULT false,
            ui_changed boolean NOT NULL DEFAULT false,
            configuration_changed boolean NOT NULL DEFAULT false,
            documentation_changed boolean NOT NULL DEFAULT false,
            change_summary text NOT NULL,
            risk_level text NOT NULL DEFAULT 'MEDIUM',
            rollback_reference text NULL,
            status text NOT NULL DEFAULT 'UNCOMMITTED_CHANGE_SET',
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_development_change_sets_session ON development_change_sets(development_session_id, created_at DESC);

        CREATE TABLE development_change_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            change_set_id uuid NOT NULL REFERENCES development_change_sets(id) ON DELETE CASCADE,
            entity_type text NOT NULL,
            entity_identifier text NOT NULL,
            operation text NOT NULL DEFAULT 'UNKNOWN',
            before_reference text NULL,
            after_reference text NULL,
            summary text NOT NULL,
            evidence_reference jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_development_change_items_set ON development_change_items(change_set_id, entity_type);

        CREATE TABLE development_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NULL REFERENCES projects(id) ON DELETE SET NULL,
            development_session_id uuid NULL REFERENCES development_sessions(id) ON DELETE SET NULL,
            decision_type text NOT NULL,
            question text NOT NULL,
            selected_option text NOT NULL,
            alternatives jsonb NOT NULL DEFAULT '[]'::jsonb,
            rationale_summary text NOT NULL,
            constraints jsonb NOT NULL DEFAULT '[]'::jsonb,
            decided_by text NOT NULL,
            decided_at timestamptz NOT NULL DEFAULT now(),
            supersedes_decision_id uuid NULL REFERENCES development_decisions(id) ON DELETE SET NULL,
            status text NOT NULL DEFAULT 'RECORDED',
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_development_decisions_session ON development_decisions(development_session_id, decided_at DESC);

        CREATE TABLE development_validation_receipts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            change_set_id uuid NOT NULL REFERENCES development_change_sets(id) ON DELETE CASCADE,
            validation_type text NOT NULL,
            command text NOT NULL,
            environment jsonb NOT NULL DEFAULT '{}'::jsonb,
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz NULL,
            result text NOT NULL,
            passed_count integer NOT NULL DEFAULT 0,
            failed_count integer NOT NULL DEFAULT 0,
            skipped_count integer NOT NULL DEFAULT 0,
            exit_code integer NOT NULL,
            report_path text NULL,
            artifact_hash text NULL,
            structured_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_development_validations_set ON development_validation_receipts(change_set_id, started_at DESC);

        CREATE TABLE development_artifacts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            development_session_id uuid NOT NULL REFERENCES development_sessions(id) ON DELETE CASCADE,
            change_set_id uuid NULL REFERENCES development_change_sets(id) ON DELETE SET NULL,
            artifact_type text NOT NULL,
            relative_path text NOT NULL,
            content_hash text NOT NULL,
            mime_type text NOT NULL DEFAULT 'application/octet-stream',
            created_at timestamptz NOT NULL DEFAULT now(),
            authority_status text NOT NULL DEFAULT 'UNVERIFIED',
            UNIQUE (relative_path, content_hash)
        );
        CREATE INDEX ix_development_artifacts_session ON development_artifacts(development_session_id, created_at DESC);

        CREATE TABLE capability_versions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            capability_id text NOT NULL REFERENCES capability_registry(capability_id) ON DELETE CASCADE,
            semantic_version text NOT NULL,
            introduced_by_change_set uuid NULL REFERENCES development_change_sets(id) ON DELETE SET NULL,
            input_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
            output_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
            backend_routes jsonb NOT NULL DEFAULT '[]'::jsonb,
            cli_commands jsonb NOT NULL DEFAULT '[]'::jsonb,
            frontend_routes jsonb NOT NULL DEFAULT '[]'::jsonb,
            permissions jsonb NOT NULL DEFAULT '[]'::jsonb,
            dependencies jsonb NOT NULL DEFAULT '[]'::jsonb,
            read_write_classification text NOT NULL DEFAULT 'READ_ONLY',
            validation_status text NOT NULL DEFAULT 'UNVALIDATED',
            limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
            release_status text NOT NULL DEFAULT 'UNRELEASED',
            lifecycle_status text NOT NULL DEFAULT 'PROPOSED',
            effective_from timestamptz NOT NULL DEFAULT now(),
            supersedes_version_id uuid NULL REFERENCES capability_versions(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (capability_id, semantic_version)
        );
        CREATE INDEX ix_capability_versions_capability ON capability_versions(capability_id, effective_from DESC);

        CREATE TABLE capability_changes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            capability_id text NOT NULL REFERENCES capability_registry(capability_id) ON DELETE CASCADE,
            capability_version_id uuid NULL REFERENCES capability_versions(id) ON DELETE SET NULL,
            development_session_id uuid NULL REFERENCES development_sessions(id) ON DELETE SET NULL,
            change_set_id uuid NULL REFERENCES development_change_sets(id) ON DELETE SET NULL,
            decision_id uuid NULL REFERENCES development_decisions(id) ON DELETE SET NULL,
            validation_receipt_id uuid NULL REFERENCES development_validation_receipts(id) ON DELETE SET NULL,
            related_project_id uuid NULL REFERENCES projects(id) ON DELETE SET NULL,
            related_goal_id uuid NULL,
            change_type text NOT NULL,
            summary text NOT NULL,
            release_reference text NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_capability_changes_capability ON capability_changes(capability_id, created_at DESC);

        CREATE TABLE development_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            development_session_id uuid NULL REFERENCES development_sessions(id) ON DELETE SET NULL,
            change_set_id uuid NULL REFERENCES development_change_sets(id) ON DELETE SET NULL,
            event_type text NOT NULL,
            raw_event_type text NULL,
            status text NOT NULL DEFAULT 'RECORDED',
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_development_events_time ON development_events(created_at DESC, event_type);

        CREATE TABLE capability_delta_candidates (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            development_session_id uuid NOT NULL REFERENCES development_sessions(id) ON DELETE CASCADE,
            change_set_id uuid NULL REFERENCES development_change_sets(id) ON DELETE SET NULL,
            capability_id text NULL REFERENCES capability_registry(capability_id) ON DELETE SET NULL,
            candidate_type text NOT NULL,
            entity_type text NOT NULL,
            entity_identifier text NOT NULL,
            before_reference text NULL,
            after_reference text NULL,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'CANDIDATE',
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_capability_delta_session ON capability_delta_candidates(development_session_id, created_at DESC);

        CREATE TABLE development_backfill_records (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            record_type text NOT NULL,
            source_reference text NOT NULL,
            source_hash text NULL,
            confidence text NOT NULL,
            missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
            inferred_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
            target_id text NULL,
            status text NOT NULL DEFAULT 'BACKFILLED',
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_development_backfill_source ON development_backfill_records(record_type, created_at DESC);
        """
    )
    op.execute(
        "ALTER TABLE capability_registry ADD COLUMN IF NOT EXISTS current_version_id uuid NULL"
    )
    op.execute(
        "ALTER TABLE capability_registry ADD CONSTRAINT capability_registry_current_version_fk "
        "FOREIGN KEY (current_version_id) REFERENCES capability_versions(id) ON DELETE SET NULL"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_development_history_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'development ledger history is append-only: %', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in HISTORY_TABLES:
        trigger_name = f"trg_{table_name}_immutable"
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION reject_development_history_mutation()
            """
        )


def downgrade() -> None:
    # Drop triggers before the function and then remove only objects owned by
    # this migration.  Existing corpus identity rows are retained by 0011's
    # downgrade ordering, while the governance columns are removed here.
    for table_name in HISTORY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS reject_development_history_mutation()")
    op.execute("ALTER TABLE capability_registry DROP CONSTRAINT IF EXISTS capability_registry_current_version_fk")
    for table_name in (
        "development_backfill_records",
        "capability_delta_candidates",
        "development_events",
        "capability_changes",
        "capability_versions",
        "development_artifacts",
        "development_validation_receipts",
        "development_decisions",
        "development_change_items",
        "development_change_sets",
        "development_sessions",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
    op.execute("DROP TABLE IF EXISTS capability_registry CASCADE")
