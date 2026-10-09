# ADR-002: PostgreSQL as System of Record

Status: `ACCEPTED`

Ratified On: `2026-07-13`

Decision Authority: `Human`

Implementation Authorization: `NOT GRANTED`

## Context
PADOS already designates PostgreSQL for operational state while files, wiki and provider outputs also carry information.

## Decision
PostgreSQL is authoritative for projects, tasks, executions, approvals, releases, validation, evidence, audit, provider metadata and knowledge metadata.

## Alternatives Considered
Obsidian vault, GBrain, MCP server, Git files, object storage or provider dashboards as formal state.

## Consequences
External systems are projections, indexes or artifact stores; reconciliation is explicit.

## Risks
Database availability and migration discipline become critical operational concerns.

## Migration Impact
No current schema change; future aggregates receive explicit ownership and reconciliation rules.

## Validation Criteria
Recovery and projection tests prove state can be reconstructed from PostgreSQL and immutable artifacts.
