# PADOS Web Control Center Discovery

Date: 2026-07-30

## Status

Read-only discovery completed against the current non-main worktree and the live native runtime. Existing dirty files were preserved and are outside this slice.

## Current architecture

- Python package: `src/pados/`
- API: FastAPI application factory in `src/pados/api/app.py`
- CLI: Typer entry point `pados.cli.main:app`
- Database: SQLAlchemy 2.x engine/session helpers in `src/pados/db/engine.py`
- Configuration: Pydantic Settings, environment-backed database settings
- Schema migrations: Alembic under `migrations/`, current/head `20260711_0009`
- Existing application layers: control, memory, loop, project-goal, worker and retrieval services
- Existing frontend surface: a small Obsidian control plugin; no browser control center existed
- Tests: pytest unit, integration, contract, security and PowerShell launcher tests

## Live database discovery

- Runtime mode: native local runtime
- Database: `pados_local_runtime`
- PostgreSQL: 18.3
- Host/port: localhost binding on port 5432
- pgvector: enabled
- Schemas: `public` plus PostgreSQL system metadata schemas
- Public tables: 48 application tables plus `alembic_version`
- Exact row counts at discovery: 166 projects, 85 memory records, 27 run events, 235 audit events, 162 control sync events, 3 agent runs, 18 context runs, 12 task loops, 27 tasks, 3 wiki pages, 9 memory artifacts
- Views: none discovered

The complete table list is exposed by the new `/api/database/tables` endpoint and the Database Explorer; it is deliberately derived from live metadata rather than hardcoded demo JSON.

## Reusable components

- `pados.control.service.list_projects` and `get_project` for project reads
- `pados.memory.retrieval.search_memory` for governed memory search
- `pados.db.get_engine` and `Session` for read-only SQLAlchemy access
- Alembic `ScriptDirectory` for current/expected migration comparison
- Existing `/health` route remains unchanged

## Implementation target

Add a local-only, read-only `/api/*` surface backed by the existing FastAPI process, plus a React/TypeScript/Vite browser surface under `web/`. Dynamic table and column access uses SQLAlchemy reflected metadata and validated identifiers. The browser never receives database credentials and never connects to PostgreSQL directly.

## Risk boundaries

- Do not modify migration history or create a migration for the read-only explorer.
- Do not introduce raw SQL input, arbitrary SQL execution, or mutation routes.
- Do not read or print `.env` contents or credentials.
- Do not alter existing project-goal, worker, release, or runtime artifacts.
- Existing dirty files at discovery time remain pre-existing and are not part of this feature.

## Recommended technology

FastAPI extension + SQLAlchemy reflection for the backend; React + TypeScript + Vite for the local desktop-first frontend; Vitest for frontend unit checks; pytest/TestClient for backend checks; Playwright for live browser verification.
