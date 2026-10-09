# SPDX-License-Identifier: AGPL-3.0-only
"""Typed local HTTP surface for P6.15 Corpus operator actions."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from pados.config import Settings

from . import operator


class BatchReviewRequest(BaseModel):
    selected_files: list[str] = Field(default_factory=list, max_length=100)


class BatchAuthorizationRequest(BaseModel):
    manifest_sha256: str = Field(min_length=64, max_length=64)
    selected_file_hashes: dict[str, str] = Field(min_length=1, max_length=100)
    expires_hours: int = Field(default=24, ge=1, le=168)
    ingest_after: bool = False


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=25, ge=1, le=100)


def _local_only(request: Request, settings: Settings) -> None:
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail={"code": "LOCAL_ONLY", "message": "Corpus operator API is local-only"})
    if settings.api_host not in {"127.0.0.1", "localhost"}:
        raise HTTPException(status_code=503, detail={"code": "LOCAL_BINDING_REQUIRED", "message": "Corpus operator API requires localhost binding"})


def _call(function: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], function(*args, **kwargs))
    except operator.OperatorError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "write_occurred": exc.write_occurred},
        ) from exc


def register_corpus_routes(
    app: FastAPI,
    settings: Settings,
    *,
    include_project_routes: bool = True,
) -> None:
    def guard(request: Request) -> None:
        _local_only(request, settings)

    @app.get("/api/runtime")
    def runtime_info(request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.runtime_info)

    if include_project_routes:
        @app.get("/api/projects")
        def projects(request: Request) -> dict[str, Any]:
            guard(request)
            return _call(operator.list_projects)

        @app.get("/api/projects/{project_id}")
        def project(project_id: str, request: Request) -> dict[str, Any]:
            guard(request)
            return _call(operator.get_project, project_id)

    @app.get("/api/projects/{project_id}/corpus/status")
    def corpus_status(project_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.project_status, project_id)

    @app.post("/api/projects/{project_id}/corpus/batches", status_code=201)
    async def create_batch(
        project_id: str,
        request: Request,
        files: Annotated[list[UploadFile], File(min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        guard(request)
        uploads: list[operator.StagedUpload] = []
        for upload in files:
            content = await upload.read()
            uploads.append(operator.StagedUpload(upload.filename or "", upload.content_type or "", content))
        return _call(operator.stage_batch, project_id, uploads)

    @app.get("/api/projects/{project_id}/corpus/batches")
    def project_batches(
        project_id: str,
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        guard(request)
        return _call(operator.list_batches, project_id, page=page, page_size=page_size)

    @app.get("/api/corpus/batches/{batch_id}")
    def batch(batch_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.get_batch, batch_id)

    @app.post("/api/corpus/batches/{batch_id}/scan")
    def scan_batch(batch_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.scan_batch, batch_id)

    @app.post("/api/corpus/batches/{batch_id}/review")
    def review_batch(batch_id: str, payload: BatchReviewRequest, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.review_batch, batch_id, payload.selected_files)

    @app.post("/api/corpus/batches/{batch_id}/authorize")
    def authorize_batch(batch_id: str, payload: BatchAuthorizationRequest, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(
            operator.authorize_batch,
            batch_id,
            manifest_sha256=payload.manifest_sha256,
            selected_file_hashes=payload.selected_file_hashes,
            expires_hours=payload.expires_hours,
            ingest_after=payload.ingest_after,
        )

    @app.post("/api/corpus/batches/{batch_id}/ingest")
    def ingest_batch(batch_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.ingest_batch, batch_id)

    @app.post("/api/corpus/batches/{batch_id}/retry")
    def retry_batch(batch_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.retry_batch, batch_id)

    @app.get("/api/projects/{project_id}/corpus/documents")
    def documents(
        project_id: str,
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        guard(request)
        return _call(operator.list_documents, project_id, page=page, page_size=page_size)

    @app.get("/api/corpus/documents/{document_id}")
    def document(document_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.get_document, document_id)

    @app.get("/api/corpus/documents/{document_id}/versions")
    def document_versions(document_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.get_document, document_id)

    @app.get("/api/corpus/documents/{document_id}/pages/{page_number}")
    def document_page(document_id: str, page_number: int, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.get_document_page, document_id, page_number)

    @app.post("/api/projects/{project_id}/corpus/search")
    def corpus_search(project_id: str, payload: SearchRequest, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.search, project_id, payload.query, limit=payload.limit)

    @app.get("/api/corpus/evidence/{evidence_span_id}")
    def evidence(evidence_span_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return _call(operator.get_evidence, evidence_span_id)
