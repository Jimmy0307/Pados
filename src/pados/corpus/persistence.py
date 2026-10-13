# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData, Table, inspect, null, select
from sqlalchemy.orm import Session


def _table(session: Session, name: str) -> Table:
    return Table(name, MetaData(), schema="public", autoload_with=session.bind)


def persist_pilot_result(
    session: Session,
    result: dict[str, Any],
    *,
    corpus_root: str | Path,
    project_id: str | None,
) -> str:
    """Persist a validated pipeline result through the service layer only."""
    batch = _table(session, "corpus_ingestion_batches")
    source = _table(session, "corpus_sources")
    document = _table(session, "corpus_documents")
    version = _table(session, "corpus_document_versions")
    page = _table(session, "corpus_pages")
    chunk = _table(session, "corpus_chunks")
    evidence = _table(session, "corpus_evidence_spans")
    event = _table(session, "corpus_ingestion_events")
    receipt = result["receipt"]
    embedding_policy = str(receipt.get("embedding_policy", "FIXTURE_VALIDATION_ONLY"))
    embedding_model = str(
        receipt.get(
            "embedding_model",
            "pados-deterministic-hash-32"
            if embedding_policy == "FIXTURE_VALIDATION_ONLY"
            else "KEYWORD_ONLY",
        )
    )
    content_hashes = [
        str(item["content_hash"])
        for item in result["documents"]
        if item.get("content_hash") and item.get("status") == "PUBLISHED"
    ]
    if content_hashes:
        existing = session.execute(
            select(version.c.content_hash, version.c.ingestion_batch_id).where(
                version.c.content_hash.in_(content_hashes)
            )
        ).first()
        if existing:
            receipt["idempotency"] = "EXACT_DUPLICATE_EXISTING_VERSION"
            receipt["accepted"] = 0
            receipt["duplicate"] = 1
            receipt["searchable_chunks"] = 0
            receipt["parsed_pages"] = 0
            return str(existing[1]) if existing[1] else ""
    batch_id = UUID(result["batch_id"])
    session.execute(
        batch.insert().values(
            id=batch_id,
            project_id=UUID(project_id) if project_id else None,
            corpus_root=str(corpus_root),
            requested_file_count=receipt["requested_file_count"],
            discovered_file_count=receipt["discovered_file_count"],
            accepted_file_count=receipt["accepted"],
            duplicate_count=receipt["duplicate"],
            failed_count=receipt["failed"],
            quarantined_count=receipt["quarantined"],
            ocr_required_count=receipt["ocr_required"],
            parsed_page_count=receipt["parsed_pages"],
            searchable_chunk_count=receipt["searchable_chunks"],
            embedding_completion=receipt["embedding_completion"],
            parser_version="pypdf",
            embedding_model=embedding_model,
            status="VALIDATED",
            warnings=[],
            validation_receipt=receipt,
        )
    )
    root = Path(corpus_root).resolve()
    for item in result["documents"]:
        document_id = UUID(item["document_id"])
        version_id = UUID(item["version_id"])
        relative_path = item["path"]
        status = item["status"]
        existing_source = None
        if project_id:
            existing_source = session.execute(
                select(document.c.id, document.c.source_id)
                .select_from(document.join(source, document.c.source_id == source.c.id))
                .where(
                    document.c.project_id == UUID(project_id),
                    source.c.registered_root == str(root),
                    source.c.relative_path == relative_path,
                )
                .order_by(document.c.updated_at.desc())
                .limit(1)
            ).first()
        if existing_source:
            document_id = UUID(str(existing_source[0]))
            source_id = UUID(str(existing_source[1]))
        else:
            source_id = UUID(bytes=hashlib.sha256(f"{batch_id}:{relative_path}".encode()).digest()[:16])
            session.execute(source.insert().values(id=source_id, source_type="LOCAL_APPROVED_ROOT", registered_root=str(root), relative_path=relative_path, status="REGISTERED"))
            session.execute(document.insert().values(id=document_id, canonical_title=item.get("title") or Path(relative_path).stem, authors=item.get("authors", []), publication_year=item.get("publication_year"), journal_or_venue=item.get("venue"), doi=item.get("doi"), project_id=UUID(project_id) if project_id else None, source_id=source_id, status=status))
        session.execute(version.insert().values(id=version_id, document_id=document_id, ingestion_batch_id=batch_id, content_hash=item.get("content_hash") or "fixture:" + str(document_id), file_size=item.get("file_size", 0), source_path=relative_path, extraction_status=item.get("extraction_status") or status, page_count=item.get("pages", 0), warnings=item.get("warnings", []), parser_version="pypdf"))
        page_ids: dict[int, UUID] = {}
        for page_item in item.get("page_records", []):
            page_id = UUID(page_item["page_id"])
            page_ids[int(page_item["page_number"])] = page_id
            session.execute(page.insert().values(id=page_id, document_id=document_id, document_version_id=version_id, page_number=page_item["page_number"], section=page_item.get("section"), text_content=page_item.get("text", ""), normalized_text=page_item.get("normalized_text", ""), char_start=page_item.get("char_start", 0), char_end=page_item.get("char_end", 0)))
        for chunk_item in item.get("chunks", []):
            chunk_id = UUID(chunk_item["chunk_id"])
            page_id = page_ids[int(chunk_item["page_number"])]
            embedding = null() if embedding_policy == "KEYWORD_ONLY" else chunk_item.get("embedding")
            session.execute(chunk.insert().values(id=chunk_id, document_id=document_id, document_version_id=version_id, page_id=page_id, chunk_order=chunk_item.get("chunk_order", 0), section=chunk_item.get("section"), char_start=chunk_item.get("char_start", 0), char_end=chunk_item.get("char_end", 0), normalized_text=chunk_item.get("text", ""), token_estimate=chunk_item.get("token_estimate", 0), embedding=embedding, embedding_status="PUBLISHED" if embedding_policy == "KEYWORD_ONLY" else "READY"))
            session.execute(evidence.insert().values(id=UUID(bytes=hashlib.sha256(f"evidence:{chunk_id}".encode()).digest()[:16]), document_id=document_id, document_version_id=version_id, page_id=page_id, chunk_id=chunk_id, section=chunk_item.get("section"), char_start=chunk_item.get("char_start", 0), char_end=chunk_item.get("char_end", 0), quoted_text=chunk_item.get("text", "")[:600], anchor_label=f"page-{chunk_item['page_number']}-span-{chunk_item.get('char_start', 0)}-{chunk_item.get('char_end', 0)}"))
        for event_item in item.get("events", []):
            session.execute(event.insert().values(id=UUID(bytes=hashlib.sha256(f"event:{batch_id}:{document_id}:{event_item['to_state']}:{event_item.get('detail', '')}".encode()).digest()[:16]), batch_id=batch_id, document_id=document_id, from_state=event_item.get("from_state"), to_state=event_item["to_state"], event_type=event_item["event_type"], detail=event_item.get("detail")))
    bind = session.get_bind()
    if "audit_events" in inspect(bind).get_table_names(schema="public"):
        audit = _table(session, "audit_events")
        session.execute(audit.insert().values(id=UUID(bytes=hashlib.sha256(f"audit:{batch_id}".encode()).digest()[:16]), entity_type="corpus_ingestion_batch", entity_id=batch_id, event_type="CORPUS_INGESTION_PUBLISHED", actor="web_control_center", before_state={}, after_state=receipt))
    session.commit()
    return str(batch_id)
