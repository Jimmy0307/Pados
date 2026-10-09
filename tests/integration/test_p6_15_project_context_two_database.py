from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

from pados.config import Settings
from pados.corpus.persistence import persist_pilot_result
from pados.corpus.pipeline import run_fixture_pilot
from pados.corpus.project_context import ensure_project_context_projection
from pados.db import session_scope

PROJECT_ID = "a45eed63-f747-49f7-9f48-05aaa6eb3e72"
OPERATIONAL_DATABASE = "pados_local_runtime"
CORPUS_DATABASE = "pados_corpus_runtime"


def _connect_factory(settings: Settings):
    def connect(database: str) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=database,
            user=settings.db_user,
            password=settings.db_password,
            connect_timeout=3,
        )

    return connect


def _project_row(connection: psycopg.Connection[Any]) -> tuple[Any, ...] | None:
    with connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        return connection.execute(
            """
            SELECT id::text, name, root_path, default_branch, status, classification,
                   created_at, updated_at
            FROM projects
            WHERE id = %s
            """,
            (PROJECT_ID,),
        ).fetchone()


def _counts(connection: psycopg.Connection[Any]) -> dict[str, int]:
    queries = {
        "documents": "SELECT count(*) FROM corpus_documents WHERE project_id = %s",
        "versions": "SELECT count(*) FROM corpus_document_versions v JOIN corpus_documents d ON d.id = v.document_id WHERE d.project_id = %s",
        "pages": "SELECT count(*) FROM corpus_pages p JOIN corpus_documents d ON d.id = p.document_id WHERE d.project_id = %s",
        "chunks": "SELECT count(*) FROM corpus_chunks c JOIN corpus_documents d ON d.id = c.document_id WHERE d.project_id = %s",
        "evidence": "SELECT count(*) FROM corpus_evidence_spans e JOIN corpus_documents d ON d.id = e.document_id WHERE d.project_id = %s",
    }
    return {name: int(connection.execute(query, (PROJECT_ID,)).fetchone()[0]) for name, query in queries.items()}


def _text_pdf() -> bytes:
    stream = b"BT /F1 12 Tf 72 720 Td (P6.15 projection integration) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(obj)
        body.extend(b"\nendobj\n")
    xref_offset = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(body)


def test_production_shaped_projection_and_ingest_across_two_databases(tmp_path: Path) -> None:
    if os.environ.get("P6_15_RUN_TWO_DATABASE_INTEGRATION") != "1":
        pytest.skip("set P6_15_RUN_TWO_DATABASE_INTEGRATION=1 for the authorized two-database run")

    settings = Settings()
    connect = _connect_factory(settings)
    with connect(OPERATIONAL_DATABASE) as operational:
        before_project = _project_row(operational)
    assert before_project is not None, "authorized operational project must already exist"

    with connect(CORPUS_DATABASE) as corpus:
        before_counts = _counts(corpus)
    projection = ensure_project_context_projection(
        project_id=PROJECT_ID,
        operational_database=OPERATIONAL_DATABASE,
        corpus_database=CORPUS_DATABASE,
        operational_revision="20260711_0009",
        connect=connect,
    )
    assert projection["status"] in {"CREATED", "UNCHANGED", "REFRESHED"}
    assert projection["foreign_key_target_verified"] is True

    root = tmp_path / "approved-corpus"
    root.mkdir()
    pdf = root / "projection-integration.pdf"
    pdf.write_bytes(_text_pdf())
    first = run_fixture_pilot(
        root,
        project_id=PROJECT_ID,
        max_documents=1,
        embedding_policy="KEYWORD_ONLY",
        selected_paths=[pdf],
    )
    assert first["receipt"]["accepted"] == 1
    corpus_settings = settings.model_copy(update={"db_name": CORPUS_DATABASE})
    with session_scope(corpus_settings) as session:
        persist_pilot_result(session, first, corpus_root=root, project_id=PROJECT_ID)

    with connect(CORPUS_DATABASE) as corpus:
        after_first_counts = _counts(corpus)
        assert after_first_counts["documents"] == before_counts["documents"] + 1
        assert corpus.execute(
            "SELECT EXISTS (SELECT 1 FROM projects WHERE id = %s)", (PROJECT_ID,)
        ).fetchone()[0] is True

    second = run_fixture_pilot(
        root,
        project_id=PROJECT_ID,
        max_documents=1,
        embedding_policy="KEYWORD_ONLY",
        selected_paths=[pdf],
    )
    with session_scope(corpus_settings) as session:
        persist_pilot_result(session, second, corpus_root=root, project_id=PROJECT_ID)
    assert second["receipt"]["idempotency"] == "EXACT_DUPLICATE_EXISTING_VERSION"

    with connect(CORPUS_DATABASE) as corpus:
        assert _counts(corpus) == after_first_counts
    with connect(OPERATIONAL_DATABASE) as operational:
        assert _project_row(operational) == before_project
