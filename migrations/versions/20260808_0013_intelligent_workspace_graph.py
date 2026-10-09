"""P6.16 intelligent workspace graph foundation.

This additive operational branch starts at the accepted operational revision.
It does not depend on or modify the independent development-ledger/Corpus chain.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260808_0013"
down_revision: str | None = "20260711_0009"
branch_labels: str | Sequence[str] | None = ("p6_16_operational_graph",)
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE graph_workspaces (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NULL REFERENCES projects(id) ON DELETE SET NULL,
            name text NOT NULL,
            description text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'ACTIVE',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            version integer NOT NULL DEFAULT 1,
            CHECK (status IN ('ACTIVE', 'ARCHIVED')),
            CHECK (version > 0)
        );
        CREATE INDEX ix_graph_workspaces_project_status
            ON graph_workspaces(project_id, status, updated_at DESC);

        CREATE TABLE graph_groups (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES graph_workspaces(id) ON DELETE CASCADE,
            title text NOT NULL,
            x double precision NOT NULL,
            y double precision NOT NULL,
            width double precision NOT NULL,
            height double precision NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (width > 0),
            CHECK (height > 0)
        );
        CREATE INDEX ix_graph_groups_workspace ON graph_groups(workspace_id, updated_at DESC);

        CREATE TABLE graph_nodes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES graph_workspaces(id) ON DELETE CASCADE,
            group_id uuid NULL REFERENCES graph_groups(id) ON DELETE SET NULL,
            semantic_type text NOT NULL,
            title text NOT NULL,
            content text NOT NULL DEFAULT '',
            x double precision NOT NULL DEFAULT 0,
            y double precision NOT NULL DEFAULT 0,
            provenance text NOT NULL DEFAULT 'HUMAN_CREATED',
            assurance_status text NOT NULL DEFAULT 'PROVISIONAL',
            idea_state text NULL,
            source_capability_id text NULL,
            reuse_mode text NULL,
            source_version text NULL,
            parent_capability_id text NULL,
            internal_ref_type text NULL,
            internal_ref_id text NULL,
            lineage_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            version integer NOT NULL DEFAULT 1,
            CHECK (semantic_type IN ('IDEA', 'CONCEPT', 'ACTION', 'EXPERT', 'RESOURCE', 'OUTPUT')),
            CHECK (provenance IN ('HUMAN_CREATED', 'AI_PROPOSED', 'SYSTEM_PROPOSED', 'IMPORTED')),
            CHECK (assurance_status IN ('PROVISIONAL', 'HUMAN_CONFIRMED', 'VALIDATED', 'PROMOTED')),
            CHECK (idea_state IS NULL OR idea_state IN ('ACTIVE', 'INCUBATING', 'PAUSED', 'ARCHIVED')),
            CHECK (reuse_mode IS NULL OR reuse_mode IN ('REFERENCE', 'FORK', 'SNAPSHOT_COPY')),
            CHECK (version > 0)
        );
        CREATE INDEX ix_graph_nodes_workspace_position ON graph_nodes(workspace_id, updated_at DESC);
        CREATE INDEX ix_graph_nodes_workspace_type ON graph_nodes(workspace_id, semantic_type);
        CREATE INDEX ix_graph_nodes_capability ON graph_nodes(source_capability_id, reuse_mode);

        CREATE TABLE graph_edges (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES graph_workspaces(id) ON DELETE CASCADE,
            source_node_id uuid NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
            target_node_id uuid NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
            relationship_type text NOT NULL DEFAULT 'RELATED_TO',
            label text NOT NULL DEFAULT '',
            provenance text NOT NULL DEFAULT 'HUMAN_CREATED',
            metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (source_node_id <> target_node_id),
            CHECK (provenance IN ('HUMAN_CREATED', 'AI_PROPOSED', 'SYSTEM_PROPOSED', 'IMPORTED')),
            UNIQUE (workspace_id, source_node_id, target_node_id, relationship_type)
        );
        CREATE INDEX ix_graph_edges_workspace ON graph_edges(workspace_id, updated_at DESC);

        CREATE TABLE graph_intents (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES graph_workspaces(id) ON DELETE CASCADE,
            action text NOT NULL,
            selected_node_ids jsonb NOT NULL,
            selection_context jsonb NOT NULL,
            interpreted_goal text NOT NULL,
            plan_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            governance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL,
            harness_loop_id uuid NULL REFERENCES task_loops(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (action IN ('PLAN', 'RUN')),
            CHECK (status IN ('PROPOSED', 'READY', 'BLOCKED', 'STARTED'))
        );
        CREATE INDEX ix_graph_intents_workspace ON graph_intents(workspace_id, created_at DESC);

        CREATE TABLE cognitive_work_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES graph_workspaces(id) ON DELETE CASCADE,
            idea_node_id uuid NULL REFERENCES graph_nodes(id) ON DELETE SET NULL,
            operation text NOT NULL,
            status text NOT NULL DEFAULT 'QUEUED',
            selected_node_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            selection_context jsonb NOT NULL DEFAULT '{}'::jsonb,
            candidate_outputs jsonb NOT NULL DEFAULT '[]'::jsonb,
            provider text NULL,
            error_code text NULL,
            requested_by text NOT NULL DEFAULT 'HUMAN',
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz NULL,
            completed_at timestamptz NULL,
            CHECK (operation IN ('THINK', 'EXPAND', 'CONNECT', 'CRITIQUE', 'INCUBATE')),
            CHECK (status IN ('QUEUED', 'PROCESSING', 'COMPLETED', 'BLOCKED', 'FAILED'))
        );
        CREATE INDEX ix_cognitive_work_items_queue
            ON cognitive_work_items(status, created_at, workspace_id);
        """
    )


def downgrade() -> None:
    for table_name in (
        "cognitive_work_items",
        "graph_intents",
        "graph_edges",
        "graph_nodes",
        "graph_groups",
        "graph_workspaces",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
