# PADOS vNext Conditional Acceptance Criteria

Status: `RECOMMENDATION ONLY`.

| ADR | Required condition | Evidence owner | Target phase | Acceptance evidence |
| --- | --- | --- | --- | --- |
| 001 | Complete mutation map and Harness command boundary | Architecture / Builder | B | Contract tests prove agent, UI and adapter cannot transition state directly |
| 004 | P6.7 compatibility alias and Context ownership map | Architect / QA | D | Characterization suite, packet hash/provenance tests, no GBrain import |
| 005 | Standard adapter contract and execution observation boundary | Architect / QA | C | Adapter contract suite; no adapter writes task/execution state |
| 007 | Inbound/outbound threat model, scopes and application-command mapping | Security / Architect | F | Authz, rate-limit, transport, failure and direct-write denial tests |
| 009 | Current-state projection and cross-stream correlation contract | Architect / QA | B | Append-only, idempotency, sequence and outbox tests |
| 010 | Artifact storage, retention, hash and evidence-link policy | Release / QA | B | Hash mismatch, immutable result and release linkage tests |
| 014 | Runtime capability matrix for local, offline, WSL, Docker and remote | Runtime / QA | C or G | Mode-specific health and policy-gate tests; Docker not required for native |
| 015 | Approval scope, expiry, revocation, role and evidence rules | Policy / QA | B | Forged/stale/mismatched approval rejection tests |

Conditions are not implementation tickets. Their purpose is to define the evidence that must exist before an ADR may be marked `ACCEPTED` or used as an implementation mandate.
