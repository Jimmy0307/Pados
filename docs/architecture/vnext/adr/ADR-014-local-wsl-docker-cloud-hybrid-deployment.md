# ADR-014: Local, WSL, Docker, Cloud and Hybrid Deployment

Status: `PROPOSED`

## Context
The runtime contract already supports native Windows, future WSL and optional Docker.

## Decision
Keep capability-based runtime adapters for Local Windows, WSL, Docker, Linux, Cloud, Hybrid, Offline and Remote modes.

## Alternatives Considered
Docker-only deployment, cloud-only control plane, or one host-specific architecture.

## Consequences
Harness contracts remain portable; provider and storage policy varies by mode.

## Risks
Mode-specific behavior can drift.

## Migration Impact
No deployment mutation now; future adapters must declare capabilities and evidence.

## Validation Criteria
Mode matrix tests prove Docker absence does not fail native/offline profiles and remote calls remain policy-gated.
