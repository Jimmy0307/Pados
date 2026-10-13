# ADR-006: Subscription Execution versus API Execution

Status: `PROPOSED`

## Context
Interactive subscriptions and metered APIs differ in credentials, billing, automation permission and usage visibility.

## Decision
Model interactive subscription, metered API and local compute as separate provider categories and execution modes.

## Alternatives Considered
Treat subscriptions as APIs, infer automation permission, or schedule user sessions headlessly.

## Consequences
Human-supervised interactive execution is first-class but cannot be impersonated by the scheduler.

## Risks
Provider product terms and capabilities can change.

## Migration Impact
Registry metadata is added only in an authorized future phase.

## Validation Criteria
Policy tests reject headless dispatch where `supports_headless_execution` is false or unknown.
