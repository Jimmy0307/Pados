# ADR-012: Knowledge Memory Provider Compatibility Contract

Status: `PROPOSED`

## Context
Future providers may offer semantic search, graphs, indexing and ingestion with different semantics.

## Decision
Use a provider-neutral capability-discovery contract for Native, GBrain and future memory providers; defer implementation to Phase 2.

## Alternatives Considered
Embed GBrain schema/SDK in Harness, select a provider now, or use no abstraction.

## Consequences
PADOS retains control and can exit a provider through export and metadata lineage.

## Risks
The lowest common denominator may limit advanced features.

## Migration Impact
No current data or runtime change; provider adoption requires a dedicated ADR update.

## Validation Criteria
Capability, isolation, export, lineage, health and offline-mode contract tests pass.
