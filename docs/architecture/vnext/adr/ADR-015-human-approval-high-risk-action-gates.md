# ADR-015: Human Approval and High-risk Action Gates

Status: `PROPOSED`

## Context
P6.x has work-request approvals, maker-checker logic and P6.11 human gates.

## Decision
High-risk, destructive, external-side-effect, release, scope-expansion, budget-expansion and privilege-expansion actions require immutable, scoped and expiring human approval.

## Alternatives Considered
Agent self-approval, blanket approval, non-expiring approval or implicit approval from task creation.

## Consequences
Automation pauses safely and captures the exact decision context.

## Risks
Human gates can reduce throughput if policy is too broad.

## Migration Impact
Unify legacy approval forms behind policy evaluation after compatibility tests.

## Validation Criteria
Tests reject forged, stale, mismatched-scope, revoked and insufficient-role approvals.
