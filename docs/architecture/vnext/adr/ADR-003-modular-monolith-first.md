# ADR-003: Modular Monolith First

Status: `ACCEPTED`

Ratified On: `2026-07-13`

Decision Authority: `Human`

Implementation Authorization: `NOT GRANTED`

## Context
PADOS is currently a local FastAPI/Typer/PostgreSQL application with mixed but bounded modules.

## Decision
Adopt DDD bounded contexts in a modular monolith; use ports, transactional outbox and stable events to remain microservice-ready.

## Alternatives Considered
Immediate microservices, a single unbounded service layer, or provider-specific applications.

## Consequences
One deployment and transaction boundary remain initially; module dependency rules must be enforced.

## Risks
Poor module discipline can recreate a distributed monolith internally.

## Migration Impact
Introduce application/port boundaries before extracting workers, Context Service or MCP Gateway.

## Validation Criteria
Architecture tests forbid domain imports of provider, MCP, UI and database-framework adapters.
