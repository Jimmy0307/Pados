# ADR-001: Harness as Sole Control Plane

Status: `PROPOSED`

Ratification Decision: `ACCEPTED WITH CONDITIONS`

Ratified On: `2026-07-13`

Decision Authority: `Human`

Implementation Authorization: `NOT GRANTED`

## Context
P6.x distributes workflow effects across control, loop, worker, provider and memory services.

## Decision
Only Harness application commands may transition governed state, dispatch execution, approve work, seal evidence or release artifacts.

## Alternatives Considered
Agent-owned workflow, MCP-owned workflow, Memory-owned workflow, or direct UI/database mutation.

## Consequences
Adapters become observers/executors; a command boundary and outbox are required.

## Risks
Initial orchestration refactoring is substantial; temporary compatibility facades are needed.

## Migration Impact
Characterize existing transitions, then route each legacy mutation through the Harness.

## Validation Criteria
Tests prove adapters, Obsidian and MCP cannot mutate governed tables directly.
