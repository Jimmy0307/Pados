"""P6.15 governed knowledge corpus pilot schema.

This migration is additive. It creates a document corpus domain beside the
existing memory domain and does not backfill or ingest any existing records.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260730_0011"
down_revision: str | None = "20260730_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE corpus_sources (
            id uuid PRIMARY KEY,
            source_type text NOT NULL,
            registered_root text NOT NULL,
            relative_path text,
            external_identifier text,
            status text NOT NULL DEFAULT 'REGISTERED',
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_corpus_sources_status ON corpus_sources(status);

        CREATE TABLE corpus_ingestion_batches (
            id uuid PRIMARY KEY,
            project_id uuid NULL REFERENCES projects(id) ON DELETE SET NULL,
            corpus_root text NOT NULL,
            requested_file_count integer NOT NULL DEFAULT 0,
            discovered_file_count integer NOT NULL DEFAULT 0,
            accepted_file_count integer NOT NULL DEFAULT 0,
            duplicate_count integer NOT NULL DEFAULT 0,
            failed_count integer NOT NULL DEFAULT 0,
            quarantined_count integer NOT NULL DEFAULT 0,
            ocr_required_count integer NOT NULL DEFAULT 0,
            parsed_page_count integer NOT NULL DEFAULT 0,
            searchable_chunk_count integer NOT NULL DEFAULT 0,
            embedding_completion numeric(5,4) NOT NULL DEFAULT 0,
            parser_version text NOT NULL,
            embedding_model text NOT NULL,
            status text NOT NULL DEFAULT 'DISCOVERED',
            warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
            validation_receipt jsonb,
            started_at timestamp NOT NULL DEFAULT now(),
            ended_at timestamp,
            created_at timestamp NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_corpus_batches_project_status ON corpus_ingestion_batches(project_id, status, started_at);

        CREATE TABLE corpus_documents (
            id uuid PRIMARY KEY,
            canonical_title text NOT NULL,
            authors jsonb NOT NULL DEFAULT '[]'::jsonb,
            publication_year integer,
            journal_or_venue text,
            doi text,
            language text,
            document_type text NOT NULL DEFAULT 'PAPER',
            method text,
            construct text,
            project_id uuid NULL REFERENCES projects(id) ON DELETE SET NULL,
            source_id uuid NULL REFERENCES corpus_sources(id) ON DELETE SET NULL,
            status text NOT NULL DEFAULT 'DISCOVERED',
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_corpus_documents_project_status ON corpus_documents(project_id, status, updated_at);
        CREATE INDEX ix_corpus_documents_doi ON corpus_documents(doi);

        CREATE TABLE corpus_document_versions (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
            ingestion_batch_id uuid NULL REFERENCES corpus_ingestion_batches(id) ON DELETE SET NULL,
            content_hash text NOT NULL,
            normalized_text_hash text,
            file_size bigint NOT NULL,
            mime_type text NOT NULL DEFAULT 'application/pdf',
            parser_version text NOT NULL,
            extraction_status text NOT NULL DEFAULT 'DISCOVERED',
            page_count integer NOT NULL DEFAULT 0,
            source_path text NOT NULL,
            warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            UNIQUE(content_hash)
        );
        CREATE INDEX ix_corpus_versions_document ON corpus_document_versions(document_id, created_at);

        CREATE TABLE corpus_pages (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
            document_version_id uuid NOT NULL REFERENCES corpus_document_versions(id) ON DELETE CASCADE,
            page_number integer NOT NULL,
            section text,
            text_content text NOT NULL DEFAULT '',
            normalized_text text NOT NULL DEFAULT '',
            char_start integer NOT NULL DEFAULT 0,
            char_end integer NOT NULL DEFAULT 0,
            created_at timestamp NOT NULL DEFAULT now(),
            UNIQUE(document_version_id, page_number)
        );
        CREATE INDEX ix_corpus_pages_document_page ON corpus_pages(document_id, page_number);

        CREATE TABLE corpus_chunks (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
            document_version_id uuid NOT NULL REFERENCES corpus_document_versions(id) ON DELETE CASCADE,
            page_id uuid NOT NULL REFERENCES corpus_pages(id) ON DELETE CASCADE,
            chunk_order integer NOT NULL,
            section text,
            char_start integer NOT NULL DEFAULT 0,
            char_end integer NOT NULL DEFAULT 0,
            normalized_text text NOT NULL,
            token_estimate integer NOT NULL DEFAULT 0,
            embedding jsonb,
            embedding_status text NOT NULL DEFAULT 'PENDING',
            created_at timestamp NOT NULL DEFAULT now(),
            UNIQUE(document_version_id, chunk_order)
        );
        CREATE INDEX ix_corpus_chunks_document_page ON corpus_chunks(document_id, page_id, chunk_order);

        CREATE TABLE corpus_evidence_spans (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
            document_version_id uuid NOT NULL REFERENCES corpus_document_versions(id) ON DELETE CASCADE,
            page_id uuid NOT NULL REFERENCES corpus_pages(id) ON DELETE CASCADE,
            chunk_id uuid NULL REFERENCES corpus_chunks(id) ON DELETE SET NULL,
            section text,
            char_start integer NOT NULL,
            char_end integer NOT NULL,
            quoted_text text NOT NULL,
            anchor_label text NOT NULL,
            created_at timestamp NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_corpus_evidence_document_page ON corpus_evidence_spans(document_id, page_id, char_start);

        CREATE TABLE corpus_document_relationships (
            id uuid PRIMARY KEY,
            left_document_id uuid NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
            right_document_id uuid NOT NULL REFERENCES corpus_documents(id) ON DELETE CASCADE,
            relationship_type text NOT NULL,
            confidence numeric(5,4),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            UNIQUE(left_document_id, right_document_id, relationship_type)
        );

        CREATE TABLE corpus_ingestion_events (
            id uuid PRIMARY KEY,
            batch_id uuid NOT NULL REFERENCES corpus_ingestion_batches(id) ON DELETE CASCADE,
            document_id uuid NULL REFERENCES corpus_documents(id) ON DELETE SET NULL,
            from_state text,
            to_state text NOT NULL,
            event_type text NOT NULL,
            detail text,
            created_at timestamp NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_corpus_ingestion_events_batch ON corpus_ingestion_events(batch_id, created_at);

        """
    )


def downgrade() -> None:
    for table_name in (
        "corpus_ingestion_events",
        "corpus_document_relationships",
        "corpus_evidence_spans",
        "corpus_chunks",
        "corpus_pages",
        "corpus_document_versions",
        "corpus_documents",
        "corpus_ingestion_batches",
        "corpus_sources",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
