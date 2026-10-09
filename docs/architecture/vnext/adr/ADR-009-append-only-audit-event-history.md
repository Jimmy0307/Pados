# ADR-009: Append-only Audit and Event History

Status: `PROPOSED`

## Context
P6.x already has append-only triggers for several event streams, but coverage is fragmented.

## Decision
Governed transitions emit append-only events with actor, correlation, policy, timestamp and integrity metadata; integration delivery uses transactional outbox.

## Alternatives Considered
Mutable log rows, provider logs as audit truth, or state-only persistence.

## Consequences
Storage grows and retention must be governed; investigations become reproducible.

## Risks
Inconsistent event schemas can reduce usefulness.

## Migration Impact
Preserve existing event streams and introduce correlation adapters incrementally.

## Validation Criteria
Update/delete rejection, ordered sequence, idempotency and outbox atomicity tests pass.
