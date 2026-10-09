"""p6.7 context memory foundation

Revision ID: 20260710_0003
Revises: 20260710_0002
Create Date: 2026-07-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260710_0003"
down_revision: str | None = "20260710_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS classification text NOT NULL DEFAULT 'internal',
        ADD COLUMN IF NOT EXISTS last_verified_at timestamptz NULL
        """
    )
    op.execute(
        """
        CREATE TABLE context_project_registry (
            project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            canonical_root text NOT NULL UNIQUE,
            status text NOT NULL DEFAULT 'active',
            classification text NOT NULL DEFAULT 'internal',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            last_verified_at timestamptz NULL,
            CHECK (status IN ('active', 'inactive', 'archived', 'blocked'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE context_sources (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_type text NOT NULL,
            source_uri text NOT NULL,
            canonical_path text NULL,
            source_hash text NULL,
            verification_status text NOT NULL,
            freshness_status text NOT NULL,
            trust_level text NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_id, source_uri),
            CHECK (source_type IN ('project', 'markdown', 'run', 'decision', 'artifact', 'observation')),
            CHECK (verification_status IN ('UNVERIFIED', 'VERIFIED', 'CONFLICTED', 'REJECTED', 'DEPRECATED')),
            CHECK (freshness_status IN ('CURRENT', 'STALE', 'EXPIRED', 'UNKNOWN')),
            CHECK (trust_level IN ('CANONICAL', 'VERIFIED_GENERATED', 'USER_CONFIRMED', 'OBSERVED', 'MODEL_INFERRED', 'UNTRUSTED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memory_records (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            memory_type text NOT NULL,
            title text NOT NULL,
            content_summary text NOT NULL,
            source_type text NOT NULL,
            source_uri text NOT NULL,
            source_hash text NULL,
            source_trust_level text NOT NULL,
            verification_status text NOT NULL,
            freshness_status text NOT NULL,
            importance integer NOT NULL DEFAULT 50,
            retrieval_priority integer NOT NULL DEFAULT 50,
            valid_from timestamptz NULL,
            valid_until timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            supersedes_memory_id uuid NULL REFERENCES memory_records(id) ON DELETE SET NULL,
            search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || content_summary)) STORED,
            embedding vector(3) NULL,
            CHECK (memory_type IN ('semantic', 'episodic', 'procedural', 'decision', 'artifact', 'observation')),
            CHECK (verification_status IN ('UNVERIFIED', 'VERIFIED', 'CONFLICTED', 'REJECTED', 'DEPRECATED')),
            CHECK (freshness_status IN ('CURRENT', 'STALE', 'EXPIRED', 'UNKNOWN')),
            CHECK (source_trust_level IN ('CANONICAL', 'VERIFIED_GENERATED', 'USER_CONFIRMED', 'OBSERVED', 'MODEL_INFERRED', 'UNTRUSTED')),
            CHECK (importance BETWEEN 0 AND 100),
            CHECK (retrieval_priority BETWEEN 0 AND 100),
            CHECK (supersedes_memory_id IS NULL OR supersedes_memory_id <> id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memory_relationships (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            from_memory_id uuid NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
            to_memory_id uuid NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
            relationship_type text NOT NULL,
            evidence_uri text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (from_memory_id, to_memory_id, relationship_type),
            CHECK (from_memory_id <> to_memory_id),
            CHECK (relationship_type IN ('CONFLICTS_WITH', 'SUPERSEDES', 'EVIDENCED_BY', 'RELATED_TO'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE context_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_type text NOT NULL,
            worker_type text NOT NULL,
            status text NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz NULL,
            input_context_packet_id uuid NULL,
            result_summary text NULL,
            error_summary text NULL,
            CHECK (status IN ('CREATED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE run_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES context_runs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            event_type text NOT NULL,
            event_time timestamptz NOT NULL DEFAULT now(),
            sequence_number integer NOT NULL,
            payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            evidence_uri text NULL,
            UNIQUE (run_id, sequence_number),
            CHECK (sequence_number > 0),
            CHECK (event_type IN (
                'RUN_CREATED', 'CONTEXT_REQUESTED', 'CONTEXT_ASSEMBLED', 'WORKER_STARTED',
                'OBSERVATION_RECORDED', 'FILE_CHANGED', 'TEST_EXECUTED', 'TEST_PASSED',
                'TEST_FAILED', 'ARTIFACT_CREATED', 'DECISION_REQUIRED', 'DECISION_RECORDED',
                'STATE_UPDATED', 'RUN_COMPLETED', 'RUN_FAILED'
            ))
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_run_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'run_events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_run_events_append_only_update
        BEFORE UPDATE ON run_events
        FOR EACH ROW EXECUTE FUNCTION reject_run_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_run_events_append_only_delete
        BEFORE DELETE ON run_events
        FOR EACH ROW EXECUTE FUNCTION reject_run_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TABLE memory_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id uuid NULL REFERENCES context_runs(id) ON DELETE SET NULL,
            decision_type text NOT NULL,
            question text NOT NULL,
            decision text NOT NULL,
            rationale text NOT NULL,
            status text NOT NULL,
            decided_by text NULL,
            evidence_uri text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            decided_at timestamptz NULL,
            supersedes_decision_id uuid NULL REFERENCES memory_decisions(id) ON DELETE SET NULL,
            CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'SUPERSEDED', 'CANCELLED')),
            CHECK (supersedes_decision_id IS NULL OR supersedes_decision_id <> id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memory_artifacts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id uuid NULL REFERENCES context_runs(id) ON DELETE SET NULL,
            artifact_type text NOT NULL,
            canonical_path text NOT NULL,
            sha256 text NOT NULL,
            size_bytes bigint NOT NULL,
            media_type text NOT NULL DEFAULT 'application/octet-stream',
            verification_status text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_id, canonical_path, sha256),
            CHECK (sha256 ~ '^[0-9A-Fa-f]{64}$'),
            CHECK (verification_status IN ('UNVERIFIED', 'VERIFIED', 'CONFLICTED', 'REJECTED', 'DEPRECATED'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE context_packets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            request_id text NOT NULL,
            purpose text NOT NULL,
            token_budget integer NOT NULL,
            estimated_tokens integer NOT NULL DEFAULT 0,
            assembly_policy_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (token_budget > 0),
            CHECK (estimated_tokens >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE context_packet_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            context_packet_id uuid NOT NULL REFERENCES context_packets(id) ON DELETE CASCADE,
            source_memory_id uuid NULL REFERENCES memory_records(id) ON DELETE SET NULL,
            rank integer NOT NULL,
            reason text NOT NULL,
            estimated_tokens integer NOT NULL,
            content_snapshot text NOT NULL,
            source_hash text NULL,
            verification_status text NOT NULL,
            UNIQUE (context_packet_id, rank),
            CHECK (rank > 0),
            CHECK (estimated_tokens >= 0),
            CHECK (verification_status IN ('UNVERIFIED', 'VERIFIED', 'CONFLICTED', 'REJECTED', 'DEPRECATED'))
        )
        """
    )
    op.create_index("ix_context_sources_project_type", "context_sources", ["project_id", "source_type"])
    op.create_index("ix_memory_records_project_type", "memory_records", ["project_id", "memory_type"])
    op.create_index(
        "ix_memory_records_governance",
        "memory_records",
        ["project_id", "verification_status", "freshness_status", "source_trust_level"],
    )
    op.create_index("ix_memory_records_search_vector", "memory_records", ["search_vector"], postgresql_using="gin")
    op.create_index("ix_run_events_run_sequence", "run_events", ["run_id", "sequence_number"])
    op.create_index("ix_run_events_project_time", "run_events", ["project_id", "event_time"])
    op.create_index("ix_memory_artifacts_project", "memory_artifacts", ["project_id", "artifact_type"])
    op.create_index("ix_context_packet_items_packet", "context_packet_items", ["context_packet_id", "rank"])
    op.execute(
        """
        ALTER TABLE context_runs
        ADD CONSTRAINT fk_context_runs_input_packet
        FOREIGN KEY (input_context_packet_id) REFERENCES context_packets(id) ON DELETE SET NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_context_runs_input_packet", "context_runs", type_="foreignkey")
    op.drop_index("ix_context_packet_items_packet", table_name="context_packet_items")
    op.drop_index("ix_memory_artifacts_project", table_name="memory_artifacts")
    op.drop_index("ix_run_events_project_time", table_name="run_events")
    op.drop_index("ix_run_events_run_sequence", table_name="run_events")
    op.drop_index("ix_memory_records_search_vector", table_name="memory_records")
    op.drop_index("ix_memory_records_governance", table_name="memory_records")
    op.drop_index("ix_memory_records_project_type", table_name="memory_records")
    op.drop_index("ix_context_sources_project_type", table_name="context_sources")
    for table_name in [
        "context_packet_items",
        "context_packets",
        "memory_artifacts",
        "memory_decisions",
        "run_events",
        "context_runs",
        "memory_relationships",
        "memory_records",
        "context_sources",
        "context_project_registry",
    ]:
        op.drop_table(table_name)
    op.execute("DROP FUNCTION IF EXISTS reject_run_event_mutation()")
    op.execute(
        """
        ALTER TABLE projects
        DROP COLUMN IF EXISTS last_verified_at,
        DROP COLUMN IF EXISTS classification
        """
    )
