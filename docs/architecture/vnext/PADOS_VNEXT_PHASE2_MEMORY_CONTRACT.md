# PADOS vNext Phase 2 Memory Contract

Status: `PROPOSED`; Phase 2 only. No provider is selected or installed.

## Provider-neutral capability matrix

Each `MemoryProvider` declares whether it supports: `semantic_search`, `keyword_search`, `graph_query`, `entity_lookup`, `ingestion`, `summarization`, `supersession`, `deletion`, `source_lineage`, `versioning`, `project_isolation`, `access_control`, `offline_mode`, `export`, `health`, and `capability_discovery`.

Candidate provider implementations are `NativeMemoryProvider`, `GBrainMemoryProvider` and `FutureMemoryProvider`.

## Non-negotiable boundary

- A Memory Provider returns governed retrieval or knowledge operations through a port.
- PADOS retains project policy, source authorization, Context Pack selection, approval, task state, execution state, evidence and audit ownership.
- No GBrain-specific schema, identifier, SDK type or deployment assumption enters the Harness domain model.
- A provider’s graph/index is an implementation index; PostgreSQL remains authoritative for PADOS operational metadata and control state.

## Adoption gate

A Phase-2 provider can be considered only after capability discovery, isolation tests, source-lineage preservation, export/exit feasibility, offline behavior, health semantics, cost/privacy review and provider-neutral contract tests are accepted.
