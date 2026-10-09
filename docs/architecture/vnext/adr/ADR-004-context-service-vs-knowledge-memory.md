# ADR-004: Context Service versus Knowledge Memory Boundary

Status: `PROPOSED`

Ratification Decision: `ACCEPTED WITH CONDITIONS`

Ratified On: `2026-07-13`

Decision Authority: `Human`

Implementation Authorization: `NOT GRANTED`

## Context
P6.7 combines source registration, retrieval, packet assembly and memory-like records under `memory`.

## Decision
P5.6 is Context Service: source registry, retrieval port, ranking, lineage, trust/freshness and immutable bounded Context Packs. Knowledge Memory is Phase 2 behind `MemoryProvider`.

## Alternatives Considered
Expand P6.7 as a memory engine, make GBrain the core, or let agents assemble arbitrary context.

## Consequences
Context Service controls pack selection; Memory providers only supply eligible candidates.

## Risks
Legacy naming and records can cause accidental scope expansion.

## Migration Impact
Compatibility mapping and characterization tests precede any physical split or rename.

## Validation Criteria
Tests prove project isolation, deterministic packet hashes, exclusions and no direct execution coupling.
