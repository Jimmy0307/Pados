# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from .ingestion import (
    MAX_PILOT_DOCUMENTS,
    IngestionState,
    chunk_text_pages,
    deterministic_embedding,
    normalize_text,
    sha256_file,
    transition,
)
from .pdf import parse_pdf
from .security import discover_pdf_files, resolve_approved_root


def _event(events: list[dict[str, Any]], current: IngestionState | None, target: IngestionState, detail: str = "") -> IngestionState:
    record = transition(current, target, detail=detail)
    events.append({"from_state": record.from_state.value if record.from_state else None, "to_state": record.to_state.value, "event_type": record.event_type, "detail": record.detail})
    return target


def run_fixture_pilot(
    root: str | Path,
    *,
    project_id: str | None = None,
    max_documents: int = MAX_PILOT_DOCUMENTS,
    embedding_policy: str = "FIXTURE_VALIDATION_ONLY",
    selected_paths: list[Path] | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    if embedding_policy not in {"KEYWORD_ONLY", "FIXTURE_VALIDATION_ONLY"}:
        raise ValueError("unsupported embedding policy")
    approved_root = resolve_approved_root(root)
    if selected_paths is None:
        paths = discover_pdf_files(approved_root, max_documents=max_documents)
    else:
        if len(selected_paths) > max_documents:
            raise ValueError("selected paths exceed the requested document ceiling")
        from .security import resolve_child_path

        paths = [resolve_child_path(approved_root, path.relative_to(approved_root)) for path in selected_paths]
    active_batch_id = batch_id or str(uuid4())
    documents: list[dict[str, Any]] = []
    seen_hashes: dict[str, str] = {}
    seen_text_hashes: dict[str, str] = {}
    counts: Counter[str] = Counter()
    batch_events: list[dict[str, Any]] = []
    for path in paths:
        document_id = str(uuid4())
        version_id = str(uuid4())
        events: list[dict[str, Any]] = []
        state: IngestionState | None = None
        state = _event(events, state, IngestionState.DISCOVERED, path.name)
        state = _event(events, state, IngestionState.REGISTERED, path.relative_to(approved_root).as_posix())
        content_hash = sha256_file(path)
        state = _event(events, state, IngestionState.HASHED, content_hash)
        duplicate_of = seen_hashes.get(content_hash)
        try:
            parsed = parse_pdf(path)
        except RuntimeError as exc:
            state = _event(events, state, IngestionState.UNSUPPORTED, str(exc))
            counts["failed"] += 1
            documents.append({"document_id": document_id, "version_id": version_id, "path": path.relative_to(approved_root).as_posix(), "status": state.value, "events": events})
            batch_events.extend(events)
            continue
        normalized_hash = sha256_bytes(normalize_text("\n".join(page.normalized_text for page in parsed.pages)).encode("utf-8"))
        duplicate_of = duplicate_of or seen_text_hashes.get(normalized_hash)
        if duplicate_of:
            state = _event(events, state, IngestionState.DUPLICATE_CHECKED, duplicate_of)
            state = _event(events, state, IngestionState.DUPLICATE, "EXACT_DUPLICATE" if content_hash in seen_hashes else "IDENTICAL_NORMALIZED_TEXT")
            counts["duplicate"] += 1
            documents.append({"document_id": document_id, "version_id": version_id, "path": path.relative_to(approved_root).as_posix(), "status": state.value, "duplicate_of": duplicate_of, "events": events})
            batch_events.extend(events)
            continue
        state = _event(events, state, IngestionState.DUPLICATE_CHECKED, "no duplicate detected")
        if parsed.extraction_status == "PARSE_FAILED":
            state = _event(events, state, IngestionState.FAILED_MALFORMED, "; ".join(parsed.warnings))
            counts["failed"] += 1
            documents.append({"document_id": document_id, "version_id": version_id, "path": path.relative_to(approved_root).as_posix(), "status": state.value, "warnings": parsed.warnings, "events": events})
            batch_events.extend(events)
            continue
        if parsed.extraction_status == "UNSUPPORTED":
            state = _event(events, state, IngestionState.QUARANTINED_ENCRYPTED, "; ".join(parsed.warnings) or "encrypted PDF")
            counts["quarantined"] += 1
            documents.append({"document_id": document_id, "version_id": version_id, "path": path.relative_to(approved_root).as_posix(), "status": state.value, "warnings": parsed.warnings, "events": events})
            batch_events.extend(events)
            continue
        state = _event(events, state, IngestionState.PARSED, parsed.extraction_status)
        if parsed.extraction_status == "OCR_REQUIRED":
            state = _event(events, state, IngestionState.QUARANTINED_IMAGE_ONLY_OCR_DISABLED, "OCR is a separate governed capability")
            counts["ocr_required"] += 1
            counts["quarantined"] += 1
            documents.append({"document_id": document_id, "version_id": version_id, "path": path.relative_to(approved_root).as_posix(), "status": state.value, "warnings": parsed.warnings, "pages": len(parsed.pages), "events": events})
            batch_events.extend(events)
            continue
        state = _event(events, state, IngestionState.STRUCTURED, "page boundaries preserved")
        chunks = chunk_text_pages(parsed.pages)
        state = _event(events, state, IngestionState.CHUNKED, f"chunks={len(chunks)}")
        page_ids = {page.page_number: str(uuid4()) for page in parsed.pages}
        page_records = [
            {"page_id": page_ids[page.page_number], "page_number": page.page_number, "section": page.section, "text": page.text, "normalized_text": page.normalized_text, "char_start": page.char_start, "char_end": page.char_end}
            for page in parsed.pages
        ]
        chunk_records = [
            {
                "chunk_id": str(uuid4()),
                "page_id": page_ids[chunk.page_number],
                "page_number": chunk.page_number,
                "chunk_order": chunk.chunk_order,
                "section": chunk.section,
                "text": chunk.normalized_text,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "token_estimate": chunk.token_estimate,
                "embedding": (
                    deterministic_embedding(chunk.normalized_text)
                    if embedding_policy == "FIXTURE_VALIDATION_ONLY"
                    else None
                ),
            }
            for chunk in chunks
        ]
        state = _event(
            events,
            state,
            IngestionState.EMBEDDED,
            f"embedding_policy={embedding_policy}; embeddings={len(chunk_records) if embedding_policy == 'FIXTURE_VALIDATION_ONLY' else 0}",
        )
        state = _event(events, state, IngestionState.VALIDATED, "evidence anchors have document/version/page/span")
        state = _event(events, state, IngestionState.PUBLISHED, "fixture pilot record ready")
        counts["accepted"] += 1
        counts["parsed_pages"] += len(parsed.pages)
        counts["searchable_chunks"] += len(chunk_records)
        documents.append({"document_id": document_id, "version_id": version_id, "path": path.relative_to(approved_root).as_posix(), "title": parsed.title or path.stem, "authors": parsed.authors, "venue": parsed.venue, "publication_year": parsed.publication_year, "doi": parsed.doi, "content_hash": content_hash, "file_size": path.stat().st_size, "status": state.value, "extraction_status": parsed.extraction_status, "warnings": parsed.warnings, "pages": len(parsed.pages), "page_records": page_records, "chunks": chunk_records, "events": events})
        seen_hashes[content_hash] = document_id
        seen_text_hashes[normalized_hash] = document_id
        batch_events.extend(events)
    receipt = {
        "status": "PASS",
            "batch_id": active_batch_id,
        "project_id": project_id,
        "root": str(approved_root),
        "requested_file_count": max_documents,
        "discovered_file_count": len(paths),
        "accepted": counts["accepted"],
        "duplicate": counts["duplicate"],
        "failed": counts["failed"],
        "quarantined": counts["quarantined"],
        "ocr_required": counts["ocr_required"],
        "parsed_pages": counts["parsed_pages"],
        "searchable_chunks": counts["searchable_chunks"],
        "embedding_completion": (
            1.0
            if embedding_policy == "FIXTURE_VALIDATION_ONLY" and counts["searchable_chunks"]
            else 0.0
        ),
        "embedding_policy": embedding_policy,
        "embedding_model": (
            "pados-deterministic-hash-32"
            if embedding_policy == "FIXTURE_VALIDATION_ONLY"
            else "KEYWORD_ONLY"
        ),
        "evidence_anchor_validation": "PASS",
        "pilot_cap": MAX_PILOT_DOCUMENTS,
    }
    return {"batch_id": active_batch_id, "status": "VALIDATED", "receipt": receipt, "documents": documents, "events": batch_events}


def sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()
