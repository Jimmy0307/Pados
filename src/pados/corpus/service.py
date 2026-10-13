# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import MetaData, String, Table, cast, func, inspect, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .ingestion import cosine_similarity, deterministic_embedding

CORPUS_TABLES = {
    "corpus_sources",
    "corpus_ingestion_batches",
    "corpus_documents",
    "corpus_document_versions",
    "corpus_pages",
    "corpus_chunks",
    "corpus_evidence_spans",
    "corpus_document_relationships",
    "corpus_ingestion_events",
}
MAX_SEARCH_SCAN = 5000
MAX_RESULT_LIMIT = 100


CAPABILITY_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "capability_id": "corpus.evidence_search",
        "name": "Evidence Search",
        "domain": "corpus",
        "description": "Bounded keyword search with page-level evidence anchors.",
        "input_schema": {"query": "string", "mode": ["keyword"]},
        "output_schema": {"results": "evidence_search_result[]"},
        "required_permissions": ["local_read"],
        "read_write_classification": "READ_ONLY",
        "status": "FOUNDATION_ONLY",
        "version": "1.0.0",
        "validation_status": "UNIT_VALIDATED",
    },
    {
        "capability_id": "corpus.ingestion_pilot",
        "name": "Corpus Ingestion Pilot",
        "domain": "corpus",
        "description": "Explicitly confirmed local ingestion capped at 100 PDF documents from an approved root.",
        "input_schema": {"project_id": "uuid?", "root": "absolute_path", "confirm": "true"},
        "output_schema": {"batch_id": "uuid", "validation_receipt": "object"},
        "required_permissions": ["localhost_only", "approved_root", "explicit_confirmation"],
        "read_write_classification": "GOVERNED_WRITE",
        "status": "FOUNDATION_ONLY",
        "version": "1.0.0",
        "validation_status": "FIXTURE_VALIDATED",
    },
)


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names(schema="public"))


def storage_status(engine: Engine) -> str:
    return "READY" if "corpus_documents" in _tables(engine) else "NOT_MIGRATED"


def _table(engine: Engine, name: str) -> Table:
    if name not in _tables(engine):
        raise LookupError("corpus schema is not migrated")
    return Table(name, MetaData(), schema="public", autoload_with=engine)


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return str(value)


def _row(row: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): _safe(value) for key, value in row.items()}


def _pagination(page: int, page_size: int, total: int) -> dict[str, Any]:
    return {"page": page, "page_size": page_size, "total": total, "has_next": page * page_size < total}


def score_text(query: str, text: str, mode: str) -> float:
    normalized_mode = mode.lower()
    keyword_score = 1.0 if query.casefold() in text.casefold() else 0.0
    semantic_score = max(0.0, cosine_similarity(deterministic_embedding(query), deterministic_embedding(text)))
    if normalized_mode == "keyword":
        return keyword_score
    if normalized_mode == "semantic":
        return semantic_score
    if normalized_mode == "hybrid":
        return (keyword_score * 0.55) + (semantic_score * 0.45)
    raise ValueError("mode must be keyword, semantic, or hybrid")


def _count(session: Session, table: Table, *conditions: Any) -> int:
    statement = select(func.count()).select_from(table)
    if conditions:
        statement = statement.where(*conditions)
    return int(session.execute(statement).scalar_one())


def overview(engine: Engine) -> dict[str, Any]:
    if storage_status(engine) != "READY":
        return {
            "status": "ok",
            "storage_status": "NOT_MIGRATED",
            "documents": {"total": 0, "published": 0, "parsing": 0, "failed": 0, "quarantined": 0, "duplicates": 0, "ocr_required": 0},
            "pages": 0,
            "searchable_chunks": 0,
            "latest_batch": None,
        }
    document = _table(engine, "corpus_documents")
    version = _table(engine, "corpus_document_versions")
    page = _table(engine, "corpus_pages")
    chunk = _table(engine, "corpus_chunks")
    batch = _table(engine, "corpus_ingestion_batches")
    with Session(engine) as session:
        statuses = {
            "total": _count(session, document),
            "published": _count(session, document, document.c.status == "PUBLISHED"),
            "parsing": _count(session, document, document.c.status.in_(["PARSED", "STRUCTURED", "CHUNKED", "EMBEDDED"])),
            "failed": _count(session, document, document.c.status.in_(["PARSE_FAILED", "VALIDATION_FAILED", "UNSUPPORTED"])),
            "quarantined": _count(session, document, document.c.status == "QUARANTINED"),
            "duplicates": _count(session, document, document.c.status == "DUPLICATE"),
            "ocr_required": _count(session, version, version.c.extraction_status == "OCR_REQUIRED"),
        }
        latest = session.execute(select(batch).order_by(batch.c.started_at.desc()).limit(1)).mappings().first()
        return {
            "status": "ok",
            "storage_status": "READY",
            "documents": statuses,
            "pages": _count(session, page),
            "searchable_chunks": _count(session, chunk, chunk.c.embedding_status.in_(["READY", "PUBLISHED"])),
            "latest_batch": _row(latest) if latest else None,
        }


def list_documents(
    engine: Engine,
    *,
    page: int,
    page_size: int,
    query: str = "",
    project_id: str | None = None,
    status: str | None = None,
    author: str | None = None,
    year: int | None = None,
    method: str | None = None,
    construct: str | None = None,
    duplicate_group: str | None = None,
) -> dict[str, Any]:
    if storage_status(engine) != "READY":
        return {"status": "ok", "storage_status": "NOT_MIGRATED", "documents": [], "pagination": _pagination(page, page_size, 0)}
    document = _table(engine, "corpus_documents")
    version = _table(engine, "corpus_document_versions")
    conditions: list[Any] = []
    if query:
        conditions.append(or_(document.c.canonical_title.ilike(f"%{query[:200]}%"), document.c.doi.ilike(f"%{query[:200]}%")))
    if project_id:
        conditions.append(document.c.project_id == project_id)
    if status:
        conditions.append(document.c.status == status.upper())
    if author:
        conditions.append(cast(document.c.authors, String).ilike(f"%{author[:100]}%"))
    if year:
        conditions.append(document.c.publication_year == year)
    if method:
        conditions.append(document.c.method.ilike(f"%{method[:100]}%"))
    if construct:
        conditions.append(document.c.construct.ilike(f"%{construct[:100]}%"))
    if duplicate_group:
        relationship = _table(engine, "corpus_document_relationships")
        conditions.append(or_(relationship.c.left_document_id == document.c.id, relationship.c.right_document_id == document.c.id))
    statement = select(document, func.count(version.c.id).label("version_count")).select_from(document.outerjoin(version, version.c.document_id == document.c.id)).group_by(document.c.id).order_by(document.c.updated_at.desc())
    count_statement = select(func.count(func.distinct(document.c.id))).select_from(document)
    if duplicate_group:
        statement = statement.join(relationship, or_(relationship.c.left_document_id == document.c.id, relationship.c.right_document_id == document.c.id))
        count_statement = count_statement.join(relationship, or_(relationship.c.left_document_id == document.c.id, relationship.c.right_document_id == document.c.id))
    if conditions:
        statement = statement.where(*conditions)
        count_statement = count_statement.where(*conditions)
    with Session(engine) as session:
        total = int(session.execute(count_statement).scalar_one())
        rows = session.execute(statement.limit(page_size).offset((page - 1) * page_size)).mappings().all()
    documents = [_row(row) for row in rows]
    return {"status": "ok", "storage_status": "READY", "documents": documents, "pagination": _pagination(page, page_size, total)}


def get_document(engine: Engine, document_id: str) -> dict[str, Any]:
    if storage_status(engine) != "READY":
        return {
            "status": "ok",
            "storage_status": "NOT_MIGRATED",
            "document": {"id": document_id, "canonical_title": "Corpus migration pending", "status": "NOT_MIGRATED"},
            "versions": [],
        }
    document = _table(engine, "corpus_documents")
    version = _table(engine, "corpus_document_versions")
    with Session(engine) as session:
        item = session.execute(select(document).where(document.c.id == document_id)).mappings().first()
        if not item:
            raise LookupError("document not found")
        versions = session.execute(select(version).where(version.c.document_id == document_id).order_by(version.c.created_at.desc())).mappings().all()
    return {"status": "ok", "document": _row(item), "versions": [_row(row) for row in versions]}


def pages(engine: Engine, document_id: str, *, version_id: str | None = None) -> dict[str, Any]:
    if storage_status(engine) != "READY":
        return {"status": "ok", "storage_status": "NOT_MIGRATED", "pages": []}
    table = _table(engine, "corpus_pages")
    statement = select(table).where(table.c.document_id == document_id).order_by(table.c.page_number)
    if version_id:
        statement = statement.where(table.c.document_version_id == version_id)
    with Session(engine) as session:
        rows = session.execute(statement).mappings().all()
    return {"status": "ok", "storage_status": "READY", "pages": [_row(row) for row in rows]}


def evidence(engine: Engine, document_id: str, *, page_id: str | None = None) -> dict[str, Any]:
    if storage_status(engine) != "READY":
        return {"status": "ok", "storage_status": "NOT_MIGRATED", "evidence": []}
    table = _table(engine, "corpus_evidence_spans")
    statement = select(table).where(table.c.document_id == document_id).order_by(table.c.page_id, table.c.char_start)
    if page_id:
        statement = statement.where(table.c.page_id == page_id)
    with Session(engine) as session:
        rows = session.execute(statement).mappings().all()
    return {"status": "ok", "storage_status": "READY", "evidence": [_row(row) for row in rows]}


def batches(engine: Engine, *, page: int, page_size: int, project_id: str | None = None) -> dict[str, Any]:
    if "corpus_ingestion_batches" not in _tables(engine):
        return {"status": "ok", "storage_status": "NOT_MIGRATED", "batches": [], "pagination": _pagination(page, page_size, 0)}
    table = _table(engine, "corpus_ingestion_batches")
    conditions = [table.c.project_id == project_id] if project_id else []
    statement = select(table).order_by(table.c.started_at.desc())
    count_statement = select(func.count()).select_from(table)
    if conditions:
        statement = statement.where(*conditions)
        count_statement = count_statement.where(*conditions)
    with Session(engine) as session:
        total = int(session.execute(count_statement).scalar_one())
        rows = session.execute(statement.limit(page_size).offset((page - 1) * page_size)).mappings().all()
    return {"status": "ok", "storage_status": "READY", "batches": [_row(row) for row in rows], "pagination": _pagination(page, page_size, total)}


def get_batch(engine: Engine, batch_id: str) -> dict[str, Any]:
    if "corpus_ingestion_batches" not in _tables(engine):
        raise LookupError("corpus schema is not migrated")
    batch = _table(engine, "corpus_ingestion_batches")
    event = _table(engine, "corpus_ingestion_events")
    with Session(engine) as session:
        item = session.execute(select(batch).where(batch.c.id == batch_id)).mappings().first()
        if not item:
            raise LookupError("batch not found")
        events = session.execute(select(event).where(event.c.batch_id == batch_id).order_by(event.c.created_at)).mappings().all()
    return {"status": "ok", "batch": _row(item), "events": [_row(row) for row in events]}


def duplicates(engine: Engine) -> dict[str, Any]:
    if "corpus_document_relationships" not in _tables(engine):
        return {"status": "ok", "storage_status": "NOT_MIGRATED", "groups": []}
    relationship = _table(engine, "corpus_document_relationships")
    document = _table(engine, "corpus_documents")
    statement = select(relationship, document.c.canonical_title.label("left_title")).join(document, document.c.id == relationship.c.left_document_id).order_by(relationship.c.created_at.desc()).limit(500)
    with Session(engine) as session:
        rows = session.execute(statement).mappings().all()
    return {"status": "ok", "storage_status": "READY", "groups": [_row(row) for row in rows]}


def search(
    engine: Engine,
    *,
    query: str,
    mode: str,
    limit: int = 25,
    project_id: str | None = None,
    document_id: str | None = None,
    author: str | None = None,
    year: int | None = None,
    method: str | None = None,
    construct: str | None = None,
    status: str | None = None,
    page: int | None = None,
    source_type: str | None = None,
) -> dict[str, Any]:
    if not query.strip():
        raise ValueError("query is required")
    normalized_mode = mode.lower()
    if normalized_mode not in {"keyword", "semantic", "hybrid"}:
        raise ValueError("mode must be keyword, semantic, or hybrid")
    bounded_limit = min(max(limit, 1), MAX_RESULT_LIMIT)
    if storage_status(engine) != "READY":
        return {"status": "ok", "storage_status": "NOT_MIGRATED", "mode": normalized_mode, "results": []}
    chunk = _table(engine, "corpus_chunks")
    page_table = _table(engine, "corpus_pages")
    document = _table(engine, "corpus_documents")
    version = _table(engine, "corpus_document_versions")
    source = _table(engine, "corpus_sources")
    fields = [
        chunk.c.id.label("chunk_id"), chunk.c.document_id, chunk.c.document_version_id, chunk.c.page_id,
        chunk.c.section, chunk.c.char_start, chunk.c.char_end, chunk.c.normalized_text,
        chunk.c.embedding, chunk.c.embedding_status, page_table.c.page_number,
        document.c.canonical_title, document.c.authors, document.c.publication_year,
        document.c.project_id, document.c.method, document.c.construct, document.c.status,
        version.c.source_path, version.c.ingestion_batch_id, source.c.source_type,
    ]
    conditions: list[Any] = [chunk.c.embedding_status.in_(["READY", "PUBLISHED"])]
    if project_id:
        conditions.append(document.c.project_id == project_id)
    if document_id:
        conditions.append(document.c.id == document_id)
    if year:
        conditions.append(document.c.publication_year == year)
    if method:
        conditions.append(document.c.method.ilike(f"%{method[:100]}%"))
    if construct:
        conditions.append(document.c.construct.ilike(f"%{construct[:100]}%"))
    if status:
        conditions.append(document.c.status == status.upper())
    if page:
        conditions.append(page_table.c.page_number == page)
    if source_type:
        conditions.append(source.c.source_type == source_type)
    if author:
        conditions.append(cast(document.c.authors, String).ilike(f"%{author[:100]}%"))
    if normalized_mode == "keyword":
        conditions.append(chunk.c.normalized_text.ilike(f"%{query[:200]}%"))
    statement = select(*fields).select_from(
        chunk.join(page_table, chunk.c.page_id == page_table.c.id)
        .join(document, chunk.c.document_id == document.c.id)
        .join(version, chunk.c.document_version_id == version.c.id)
        .outerjoin(source, document.c.source_id == source.c.id)
    ).where(*conditions).limit(MAX_SEARCH_SCAN)
    with Session(engine) as session:
        rows = session.execute(statement).mappings().all()
    query_embedding = deterministic_embedding(query) if normalized_mode != "keyword" else None
    results: list[dict[str, Any]] = []
    for row in rows:
        text_value = str(row["normalized_text"] or "")
        if normalized_mode == "keyword":
            score = score_text(query, text_value, "keyword")
        elif isinstance(row["embedding"], list) and query_embedding is not None:
            stored_embedding = row["embedding"]
            semantic_score = max(0.0, cosine_similarity(query_embedding, stored_embedding))
            keyword_score = 1.0 if query.casefold() in text_value.casefold() else 0.0
            score = keyword_score if normalized_mode == "keyword" else semantic_score if normalized_mode == "semantic" else (keyword_score * 0.55) + (semantic_score * 0.45)
        else:
            score = 0.0
        results.append(
            {
                "document_title": row["canonical_title"],
                "source_id": str(row["document_id"]),
                "source_type": row["source_type"] or "UNKNOWN",
                "origin": row["source_path"],
                "retrieval_status": "PUBLISHED",
                "normalization_status": "NORMALIZED",
                "content_version": str(row["document_version_id"]),
                "selection_status": "REFERENCE_ONLY",
                "evidence_id": None,
                "document_id": str(row["document_id"]),
                "chunk_id": str(row["chunk_id"]),
                "page_number": row["page_number"],
                "section": row["section"],
                "excerpt": text_value[:600],
                "score": round(float(score), 6),
                "search_mode": normalized_mode,
                "source_path": row["source_path"],
                "ingestion_batch": str(row["ingestion_batch_id"]) if row["ingestion_batch_id"] else None,
                "document_version": str(row["document_version_id"]),
                "evidence_anchor": {
                    "document_id": str(row["document_id"]),
                    "document_version_id": str(row["document_version_id"]),
                    "chunk_id": str(row["chunk_id"]),
                    "page_id": str(row["page_id"]),
                    "page_number": row["page_number"],
                    "section": row["section"],
                    "char_start": row["char_start"],
                    "char_end": row["char_end"],
                },
            }
        )
    results.sort(key=lambda item: item["score"], reverse=True)
    return {"status": "ok", "storage_status": "READY", "mode": normalized_mode, "results": results[:bounded_limit]}


def capabilities() -> dict[str, Any]:
    return {
        "status": "ok",
        "registry_status": "READ_ONLY_PREVIEW",
        "capabilities": [dict(item) for item in CAPABILITY_REGISTRY],
        "boundary": "Generated UI specifications require isolated worktree, tests, browser preview, and human approval before merge.",
    }

def create_batch_stub(*, corpus_root: str, project_id: str | None, requested: int) -> dict[str, Any]:
    return {
        "batch_id": str(uuid4()),
        "project_id": project_id,
        "corpus_root": corpus_root,
        "requested_file_count": requested,
        "status": "DISCOVERED",
        "validation_receipt": {"status": "NOT_RUN", "reason": "schema not migrated or fixture-only run"},
    }
