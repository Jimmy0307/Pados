# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from sqlalchemy import inspect
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from pados.config import Settings
from pados.db import get_engine
from pados.web import service
from pados.web.schemas import HealthResponse, MemoryListResponse, ProjectListResponse


def _local_only(request: Request, settings: Settings) -> None:
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail="local web control center only")
    if settings.api_host not in {"127.0.0.1", "localhost"}:
        raise HTTPException(status_code=503, detail="web control center requires localhost binding")


def _page(page: int, page_size: int) -> tuple[int, int]:
    return page, min(page_size, service.MAX_PAGE_SIZE)


def register_web_routes(
    app: FastAPI,
    settings: Settings,
    *,
    include_project_routes: bool = True,
) -> None:
    def guard(request: Request) -> None:
        _local_only(request, settings)

    @app.get("/api/health", response_model=HealthResponse)
    def web_health(request: Request) -> dict[str, Any]:
        guard(request)
        return service.health(settings)

    @app.get("/api/system")
    def web_system(request: Request) -> dict[str, Any]:
        guard(request)
        return service.system_info(settings)

    if include_project_routes:
        @app.get("/api/projects", response_model=ProjectListResponse)
        def web_projects(
            request: Request,
            q: Annotated[str, Query(max_length=200)] = "",
            page: Annotated[int, Query(ge=1)] = 1,
            page_size: Annotated[int, Query(ge=1, le=100)] = 25,
        ) -> dict[str, Any]:
            guard(request)
            page, page_size = _page(page, page_size)
            engine = get_engine(settings)
            try:
                with Session(engine) as session:
                    return service.list_projects(session, engine, query=q, page=page, page_size=page_size)
            finally:
                engine.dispose()

        @app.get("/api/projects/{project_id}")
        def web_project(project_id: str, request: Request) -> dict[str, Any]:
            guard(request)
            engine = get_engine(settings)
            try:
                with Session(engine) as session:
                    try:
                        return service.get_project(session, engine, project_id)
                    except (LookupError, NoResultFound):
                        raise HTTPException(status_code=404, detail="project not found")
                    except Exception:  # noqa: BLE001 - unavailable authority maps to 503, never 500
                        raise HTTPException(status_code=503, detail="project unavailable")
            finally:
                engine.dispose()

    @app.get("/api/memories", response_model=MemoryListResponse)
    def web_memories(
        request: Request,
        project_id: Annotated[str | None, Query(max_length=100)] = None,
        q: Annotated[str, Query(max_length=200)] = "",
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        guard(request)
        page, page_size = _page(page, page_size)
        engine = get_engine(settings)
        try:
            with Session(engine) as session:
                return service.list_memories(
                    session,
                    engine,
                    project_id=project_id,
                    query=q,
                    page=page,
                    page_size=page_size,
                )
        finally:
            engine.dispose()

    @app.get("/api/memories/search", response_model=MemoryListResponse)
    def web_memory_search(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        project_id: Annotated[str | None, Query(max_length=100)] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        return web_memories(request, project_id=project_id, q=q, page=page, page_size=page_size)

    @app.get("/api/memories/{memory_id}")
    def web_memory(memory_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        engine = get_engine(settings)
        try:
            with Session(engine) as session:
                try:
                    return service.get_memory(session, engine, memory_id)
                except LookupError as exc:
                    raise HTTPException(status_code=404, detail="memory not found") from exc
        finally:
            engine.dispose()

    @app.get("/api/database/overview")
    def web_database_overview(request: Request) -> dict[str, Any]:
        guard(request)
        try:
            return service.database_overview(settings)
        except Exception:  # noqa: BLE001 - unavailable authority maps to 503, never 500
            raise HTTPException(status_code=503, detail="database overview unavailable")

    @app.get("/api/database/schemas")
    def web_database_schemas(request: Request) -> dict[str, Any]:
        guard(request)
        engine = get_engine(settings)
        try:
            inspector = inspect(engine)
            return {"status": "ok", "schemas": [{"name": name} for name in inspector.get_schema_names() if name == service.PUBLIC_SCHEMA]}
        finally:
            engine.dispose()

    @app.get("/api/database/tables")
    def web_database_tables(request: Request) -> dict[str, Any]:
        guard(request)
        engine = get_engine(settings)
        try:
            return {"status": "ok", "tables": service.database_tables(engine)}
        finally:
            engine.dispose()

    @app.get("/api/database/tables/{schema}/{table_name}")
    def web_database_table(schema: str, table_name: str, request: Request) -> dict[str, Any]:
        guard(request)
        engine = get_engine(settings)
        try:
            try:
                return {"status": "ok", "table": service._table_metadata(engine, schema, table_name)}
            except service.UnknownTableError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except Exception:  # noqa: BLE001 - unavailable authority maps to 503, never 500
                raise HTTPException(status_code=503, detail="table metadata unavailable")
        finally:
            engine.dispose()

    @app.get("/api/database/tables/{schema}/{table_name}/columns")
    def web_database_columns(schema: str, table_name: str, request: Request) -> dict[str, Any]:
        guard(request)
        engine = get_engine(settings)
        try:
            try:
                metadata = service._table_metadata(engine, schema, table_name)
                return {"status": "ok", "columns": metadata["columns"]}
            except service.UnknownTableError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except Exception:  # noqa: BLE001 - unavailable authority maps to 503, never 500
                raise HTTPException(status_code=503, detail="table columns unavailable")
        finally:
            engine.dispose()

    @app.get("/api/database/tables/{schema}/{table_name}/rows")
    def web_database_rows(
        schema: str,
        table_name: str,
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
        q: Annotated[str, Query(max_length=200)] = "",
        filter_column: Annotated[str | None, Query(max_length=100)] = None,
        filter_value: Annotated[str | None, Query(max_length=200)] = None,
        sort: Annotated[str | None, Query(max_length=100)] = None,
        descending: bool = True,
        exact_count: bool = False,
    ) -> dict[str, Any]:
        guard(request)
        page, page_size = _page(page, page_size)
        engine = get_engine(settings)
        try:
            with Session(engine) as session:
                try:
                    return service.table_rows(
                        session,
                        engine,
                        schema=schema,
                        table_name=table_name,
                        page=page,
                        page_size=page_size,
                        query=q,
                        filter_column=filter_column,
                        filter_value=filter_value,
                        sort=sort,
                        descending=descending,
                        exact_count=exact_count,
                    )
                except service.UnknownTableError as exc:
                    raise HTTPException(status_code=404, detail=str(exc))
                except Exception:  # noqa: BLE001 - unavailable authority maps to 503, never 500
                    raise HTTPException(status_code=503, detail="table rows unavailable")
        finally:
            engine.dispose()

    @app.get("/api/database/relationships")
    def web_database_relationships(request: Request) -> dict[str, Any]:
        guard(request)
        engine = get_engine(settings)
        try:
            return {"status": "ok", "relationships": service.relationships(engine)}
        finally:
            engine.dispose()

    @app.get("/api/runs")
    def web_runs(
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        guard(request)
        page, page_size = _page(page, page_size)
        engine = get_engine(settings)
        try:
            return service.list_runs(engine, page=page, page_size=page_size)
        finally:
            engine.dispose()

    @app.get("/api/events")
    def web_events(
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        guard(request)
        page, page_size = _page(page, page_size)
        engine = get_engine(settings)
        try:
            return service.list_events(engine, page=page, page_size=page_size)
        finally:
            engine.dispose()
