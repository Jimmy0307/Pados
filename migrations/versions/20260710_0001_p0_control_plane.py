"""p0 control plane schema

Revision ID: 20260710_0001
Revises:
Create Date: 2026-07-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260710_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE projects (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            root_path text NOT NULL,
            default_branch text NOT NULL DEFAULT 'main',
            status text NOT NULL DEFAULT 'active',
            configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE tasks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            parent_task_id uuid NULL REFERENCES tasks(id) ON DELETE SET NULL,
            title text NOT NULL,
            objective text NOT NULL,
            acceptance_criteria jsonb NOT NULL DEFAULT '[]'::jsonb,
            scope_allowlist jsonb NOT NULL DEFAULT '[]'::jsonb,
            scope_denylist jsonb NOT NULL DEFAULT '[]'::jsonb,
            risk_level text NOT NULL DEFAULT 'medium',
            priority integer NOT NULL DEFAULT 100,
            status text NOT NULL DEFAULT 'NEW',
            current_gate text NOT NULL DEFAULT 'G0',
            assigned_agent_role text NULL,
            version integer NOT NULL DEFAULT 1,
            retry_count integer NOT NULL DEFAULT 0,
            max_retries integer NOT NULL DEFAULT 3,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE task_dependencies (
            task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            depends_on_task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            PRIMARY KEY (task_id, depends_on_task_id),
            CHECK (task_id <> depends_on_task_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            agent_role text NOT NULL,
            provider text NOT NULL,
            model_name text NULL,
            prompt_version text NULL,
            status text NOT NULL,
            input_context_hash text NULL,
            output_hash text NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz NULL,
            duration_ms integer NULL,
            input_tokens integer NULL,
            output_tokens integer NULL,
            result_summary text NULL,
            structured_output jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_code text NULL,
            error_message text NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_handoffs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            from_role text NOT NULL,
            to_role text NOT NULL,
            handoff_type text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE file_registry (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            relative_path text NOT NULL,
            language text NULL,
            module_name text NULL,
            sha256 text NOT NULL,
            git_commit text NULL,
            size_bytes bigint NOT NULL,
            symbol_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            is_binary boolean NOT NULL DEFAULT false,
            is_secret_suspected boolean NOT NULL DEFAULT false,
            last_indexed_at timestamptz NULL,
            UNIQUE (project_id, relative_path)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE file_chunks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            file_id uuid NOT NULL REFERENCES file_registry(id) ON DELETE CASCADE,
            chunk_type text NOT NULL,
            symbol_name text NULL,
            start_line integer NOT NULL,
            end_line integer NOT NULL,
            content text NOT NULL,
            content_hash text NOT NULL,
            search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
            embedding vector(3) NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.create_index("ix_file_chunks_search_vector", "file_chunks", ["search_vector"], postgresql_using="gin")
    op.execute(
        """
        CREATE TABLE wiki_pages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            slug text NOT NULL,
            title text NOT NULL,
            category text NOT NULL,
            status text NOT NULL,
            confidence text NOT NULL,
            markdown_path text NOT NULL,
            content_hash text NOT NULL,
            source_commit text NULL,
            last_verified_at timestamptz NULL,
            stale_at timestamptz NULL,
            UNIQUE (project_id, slug)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE wiki_sources (
            wiki_page_id uuid NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
            file_id uuid NOT NULL REFERENCES file_registry(id) ON DELETE CASCADE,
            source_hash text NOT NULL,
            relationship_type text NOT NULL,
            PRIMARY KEY (wiki_page_id, file_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE architecture_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_id uuid NULL REFERENCES tasks(id) ON DELETE SET NULL,
            adr_number integer NOT NULL,
            title text NOT NULL,
            status text NOT NULL,
            decision text NOT NULL,
            rationale text NOT NULL,
            alternatives jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
            supersedes_id uuid NULL REFERENCES architecture_decisions(id) ON DELETE SET NULL,
            approved_by text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE test_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NULL REFERENCES tasks(id) ON DELETE SET NULL,
            run_type text NOT NULL,
            command text NOT NULL,
            status text NOT NULL,
            exit_code integer NOT NULL,
            stdout_path text NULL,
            stderr_path text NULL,
            summary text NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE review_findings (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            review_run_id uuid NULL REFERENCES agent_runs(id) ON DELETE SET NULL,
            severity text NOT NULL,
            category text NOT NULL,
            file_path text NOT NULL,
            line_start integer NULL,
            line_end integer NULL,
            finding text NOT NULL,
            evidence text NOT NULL,
            recommended_fix text NOT NULL,
            status text NOT NULL DEFAULT 'open',
            resolved_by_run_id uuid NULL REFERENCES agent_runs(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE artifacts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NULL REFERENCES tasks(id) ON DELETE SET NULL,
            artifact_type text NOT NULL,
            relative_path text NOT NULL,
            sha256 text NOT NULL,
            size_bytes bigint NOT NULL,
            immutable boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE checkpoints (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NULL REFERENCES tasks(id) ON DELETE SET NULL,
            state_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
            git_commit text NOT NULL,
            database_revision text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE skills (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL UNIQUE,
            description text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE skill_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            skill_id uuid NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
            task_id uuid NULL REFERENCES tasks(id) ON DELETE SET NULL,
            status text NOT NULL,
            telemetry jsonb NOT NULL DEFAULT '{}'::jsonb,
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz NULL
        )
        """
    )
    op.create_index("ix_tasks_status_priority", "tasks", ["status", "priority"])
    op.create_index("ix_agent_runs_task_status", "agent_runs", ["task_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_task_status", table_name="agent_runs")
    op.drop_index("ix_tasks_status_priority", table_name="tasks")
    for table_name in [
        "skill_runs",
        "skills",
        "checkpoints",
        "artifacts",
        "review_findings",
        "test_runs",
        "architecture_decisions",
        "wiki_sources",
        "wiki_pages",
        "file_chunks",
        "file_registry",
        "agent_handoffs",
        "agent_runs",
        "task_dependencies",
        "tasks",
        "projects",
    ]:
        op.drop_table(table_name)
    op.execute("DROP EXTENSION IF EXISTS vector")
