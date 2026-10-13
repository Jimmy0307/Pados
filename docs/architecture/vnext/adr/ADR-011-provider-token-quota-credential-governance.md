# ADR-011: Provider Token, Quota and Credential Governance

Status: `PROPOSED`

## Context
Current resource limits exist but provider cost, quota and credential-mode governance does not.

## Decision
Provider Registry governs token sources, quota, rate limit, cost center, data classification, usage visibility and approved fallback; secrets are references only.

## Alternatives Considered
Static environment selection, untracked cost, secrets in prompts/config, or fallback without policy.

## Consequences
Every route is explainable and bounded; unavailable telemetry triggers conservative policy.

## Risks
Provider telemetry may be delayed or incomplete.

## Migration Impact
Add only logical contracts until credential governance design is accepted.

## Validation Criteria
Tests cover quota exhaustion, unknown visibility, budget breach, fallback eligibility and secret redaction.
