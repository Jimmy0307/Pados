# ADR-013: Obsidian as Human Workspace and Projection Layer

Status: `ACCEPTED`

Ratified On: `2026-07-13`

Decision Authority: `Human`

Implementation Authorization: `NOT GRANTED`

## Context
P6.8 provides a control surface while vNext expands human planning and visualization needs.

## Decision
Obsidian provides editing, Canvas, planning, documentation, visualization and boards as a projection plus command client.

## Alternatives Considered
Obsidian vault as database, runtime, or memory engine.

## Consequences
Sync is one-way projection plus governed command submission; drift is detectable.

## Risks
Users may edit projected status text manually.

## Migration Impact
Keep existing projections while adding explicit source-of-truth labels.

## Validation Criteria
Tests prove no vault edit directly changes operational state and projections carry version/correlation markers.
