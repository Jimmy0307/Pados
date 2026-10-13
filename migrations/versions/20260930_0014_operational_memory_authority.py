"""Operational memory authority (P6.20B schema contract, operational lane).

Memory authority is operational semantic state (V1 architecture decision 2).
This forward migration creates, on the operational branch, exactly the P6.20B
memory-authority objects the operational memory service reads and writes:
``memory_authority_events`` (append-only, with its indexes and triggers) and the
authority columns on ``context_packet_items``. The DDL is taken verbatim from
the historical P6.20B contract (proof-chain revision p6_20b_0012), which is not
edited. Existing rows are compatible: new columns are nullable or defaulted.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260930_0014"
down_revision: str | None = "20260808_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE memory_authority_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            memory_id uuid NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            version integer NOT NULL,
            authority_state text NOT NULL,
            origin_type text NOT NULL,
            origin_actor_type text NULL,
            origin_actor_id text NULL,
            actor_type text NULL,
            actor_id text NULL,
            source_ref text NULL,
            evidence_ref text NULL,
            evidence_status text NOT NULL DEFAULT 'UNKNOWN',
            result_ref text NULL,
            run_id uuid NULL REFERENCES context_runs(id) ON DELETE SET NULL,
            provider text NULL,
            model text NULL,
            runtime text NULL,
            contract_id text NULL,
            promotion_gate text NULL,
            promotion_reason text NULL,
            supersedes_memory_id uuid NULL REFERENCES memory_records(id) ON DELETE SET NULL,
            superseded_by_memory_id uuid NULL REFERENCES memory_records(id) ON DELETE SET NULL,
            previous_event_id uuid NULL REFERENCES memory_authority_events(id) ON DELETE SET NULL,
            operation_key text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (memory_id, version),
            CHECK (version > 0),
            CHECK (authority_state IN (
                'CANDIDATE', 'PROMOTED', 'HUMAN_CONFIRMED',
                'CONFLICT_CANDIDATE', 'SUPERSEDED', 'REJECTED'
            )),
            CHECK (origin_type IN ('HUMAN', 'MODEL', 'AGENT', 'SYSTEM', 'IMPORT', 'LEGACY', 'UNKNOWN')),
            CHECK (origin_actor_type IS NULL OR origin_actor_type IN
                ('HUMAN', 'MODEL', 'AGENT', 'SYSTEM', 'IMPORT', 'LEGACY', 'UNKNOWN')),
            CHECK (actor_type IS NULL OR actor_type IN
                ('HUMAN', 'MODEL', 'AGENT', 'SYSTEM', 'IMPORT', 'LEGACY', 'UNKNOWN')),
            CHECK (evidence_status IN ('PROVIDED', 'EVIDENCE_UNAVAILABLE', 'UNKNOWN')),
            CHECK (supersedes_memory_id IS NULL OR supersedes_memory_id <> memory_id),
            CHECK (superseded_by_memory_id IS NULL OR superseded_by_memory_id <> memory_id),
            CHECK (previous_event_id IS NULL OR previous_event_id <> id)
        )
        """
    )
    op.create_index(
        "ix_memory_authority_events_memory_version",
        "memory_authority_events",
        ["memory_id", "version"],
    )
    op.create_index(
        "ix_memory_authority_events_project_state",
        "memory_authority_events",
        ["project_id", "authority_state"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_memory_authority_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'memory_authority_events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_authority_events_append_only_update
        BEFORE UPDATE ON memory_authority_events
        FOR EACH ROW EXECUTE FUNCTION reject_memory_authority_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_authority_events_append_only_delete
        BEFORE DELETE ON memory_authority_events
        FOR EACH ROW EXECUTE FUNCTION reject_memory_authority_event_mutation()
        """
    )
    op.execute(
        """
        ALTER TABLE context_packet_items
        ADD COLUMN authority_event_id uuid NULL REFERENCES memory_authority_events(id) ON DELETE SET NULL,
        ADD COLUMN authority_state text NOT NULL DEFAULT 'LEGACY_UNKNOWN',
        ADD COLUMN authority_version integer NULL,
        ADD COLUMN origin_type text NOT NULL DEFAULT 'UNKNOWN',
        ADD COLUMN origin_actor_type text NULL,
        ADD COLUMN origin_actor_id text NULL,
        ADD COLUMN actor_type text NULL,
        ADD COLUMN actor_id text NULL,
        ADD COLUMN source_ref text NULL,
        ADD COLUMN evidence_ref text NULL,
        ADD COLUMN evidence_status text NOT NULL DEFAULT 'UNKNOWN',
        ADD COLUMN result_ref text NULL,
        ADD COLUMN run_id uuid NULL REFERENCES context_runs(id) ON DELETE SET NULL,
        ADD COLUMN provider text NULL,
        ADD COLUMN model text NULL,
        ADD COLUMN runtime text NULL,
        ADD COLUMN contract_id text NULL,
        ADD COLUMN promotion_gate text NULL,
        ADD COLUMN context_eligible boolean NOT NULL DEFAULT false,
        ADD CONSTRAINT ck_context_packet_items_authority_state CHECK (authority_state IN (
            'LEGACY_UNKNOWN', 'CANDIDATE', 'PROMOTED', 'HUMAN_CONFIRMED',
            'CONFLICT_CANDIDATE', 'SUPERSEDED', 'REJECTED'
        )),
        ADD CONSTRAINT ck_context_packet_items_evidence_status CHECK
            (evidence_status IN ('PROVIDED', 'EVIDENCE_UNAVAILABLE', 'UNKNOWN'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE context_packet_items
        DROP CONSTRAINT IF EXISTS ck_context_packet_items_evidence_status,
        DROP CONSTRAINT IF EXISTS ck_context_packet_items_authority_state,
        DROP COLUMN IF EXISTS context_eligible,
        DROP COLUMN IF EXISTS promotion_gate,
        DROP COLUMN IF EXISTS contract_id,
        DROP COLUMN IF EXISTS runtime,
        DROP COLUMN IF EXISTS model,
        DROP COLUMN IF EXISTS provider,
        DROP COLUMN IF EXISTS run_id,
        DROP COLUMN IF EXISTS result_ref,
        DROP COLUMN IF EXISTS evidence_status,
        DROP COLUMN IF EXISTS evidence_ref,
        DROP COLUMN IF EXISTS source_ref,
        DROP COLUMN IF EXISTS actor_id,
        DROP COLUMN IF EXISTS actor_type,
        DROP COLUMN IF EXISTS origin_actor_id,
        DROP COLUMN IF EXISTS origin_actor_type,
        DROP COLUMN IF EXISTS origin_type,
        DROP COLUMN IF EXISTS authority_version,
        DROP COLUMN IF EXISTS authority_state,
        DROP COLUMN IF EXISTS authority_event_id
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_memory_authority_events_append_only_delete ON memory_authority_events")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_authority_events_append_only_update ON memory_authority_events")
    op.execute("DROP FUNCTION IF EXISTS reject_memory_authority_event_mutation()")
    op.drop_index("ix_memory_authority_events_project_state", table_name="memory_authority_events")
    op.drop_index("ix_memory_authority_events_memory_version", table_name="memory_authority_events")
    op.drop_table("memory_authority_events")
