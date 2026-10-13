# ADR-005: Adapter-based Agent and Provider Execution

Status: `PROPOSED`

## Context
PADOS has initial Codex, Claude, Ollama and mock adapters but routing still has legacy assumptions.

## Decision
All agents and models execute through registered adapters with capability, health, estimate, execute, cancel, artifact and usage contracts.

## Alternatives Considered
Direct provider SDK use in domains, one universal agent implementation, or model-specific task states.

## Consequences
Execution contracts become provider-neutral and routing is policy-driven.

## Risks
Adapters expose unequal telemetry and control capabilities.

## Migration Impact
Wrap existing adapters; remove direct provider calls only after compatibility tests.

## Validation Criteria
Contract tests run every adapter against the same governed execution fixtures.
