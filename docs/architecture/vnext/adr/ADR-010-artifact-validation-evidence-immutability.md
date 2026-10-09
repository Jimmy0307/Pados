# ADR-010: Artifact, Validation and Evidence Immutability

Status: `PROPOSED`

## Context
P6.x records hashes, artifacts, test runs and worker outputs in separate structures.

## Decision
Artifacts are versioned and hash-addressed; finalized validation results, evidence records and release manifests are immutable.

## Alternatives Considered
Mutable artifact pointers, unverified file paths, or human/provider PASS assertions.

## Consequences
Evidence can support release gates and later audits without changing historic claims.

## Risks
Object-store retention and hash verification need operational discipline.

## Migration Impact
Map legacy artifacts/test runs before consolidation; do not rewrite historical rows.

## Validation Criteria
Hash mismatch, missing artifact, mutation rejection and release-evidence linkage tests pass.
