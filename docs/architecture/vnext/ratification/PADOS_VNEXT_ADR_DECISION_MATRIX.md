# PADOS vNext ADR Decision Matrix

Status: `RECOMMENDATION ONLY`. ADR source files remain `PROPOSED`; no ratification is implied.

| ADR | Title | Current | Recommended decision | Confidence | Evidence / missing evidence | Dependencies / conflicts | Migration and P6.11 impact | Security, data and operational impact | Rollback feasibility | Required conditions | Recommended human decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 001 | Harness as Sole Control Plane | PROPOSED | ACCEPT WITH CONDITIONS | High | P6 state machines/gates exist; missing complete mutation map | Foundation; conflicts with direct legacy service paths | Future boundary correction only; no P6.11 change now | Prevents bypass and creates command discipline | High before implementation | Inventory every current mutation and add contract tests | Accept conditionally |
| 002 | PostgreSQL as System of Record | PROPOSED | ACCEPT | High | Constitution and migrations already establish operational SQL state | Foundation; supports 009/010 | Logical ownership only; no P6.11 change | Protects data authority and recovery | High | Preserve artifact/object-storage boundary | Accept now |
| 003 | Modular Monolith First | PROPOSED | ACCEPT | High | Current FastAPI/Typer/PostgreSQL monolith and module layout | Depends on 001/002 | No P6.11 change; future extraction deferred | Reduces distributed-system operational risk | High | Enforce module dependency tests before extraction | Accept now |
| 004 | Context Service versus Knowledge Memory | PROPOSED | ACCEPT WITH CONDITIONS | High | P6.7 packet/retrieval/source functions identified; naming and ownership are mixed | Depends on 001/002/008; supports 012 | P6.11 packets protected; no rename or data rewrite now | Prevents memory engine authority creep | High before physical split | Compatibility alias, characterization tests, no GBrain coupling | Accept conditionally |
| 005 | Adapter-based Agent and Provider Execution | PROPOSED | ACCEPT WITH CONDITIONS | Medium | Codex/Claude/Ollama/mock adapters exist; execution semantics differ | Depends on 001/006/008/015 | P6.11 router behavior must be characterized first | Restricts agent authority; adds adapter operations | Medium | Standard adapter contract and no direct state mutation | Accept conditionally |
| 006 | Subscription versus API Execution | PROPOSED | REQUIRES EVIDENCE | Medium | Category distinction is sound; product terms and automation capabilities are unverified | Depends on 005/011/015 | No P6.11 change | Avoids unlawful/unsafe session automation | High because not implemented | Provider terms, supported automation, quota visibility evidence | Defer pending evidence |
| 007 | Outbound Client versus Inbound MCP Server | PROPOSED | ACCEPT WITH CONDITIONS | Medium | Boundary is architecturally clear; no MCP implementation evidence | Depends on 001/008/009/015 | No P6.11 change | High privilege-escalation and transport risk | High before server exists | Threat model, command boundary and scope tests before implementation | Accept conditionally |
| 008 | Project Isolation and Repository Boundary | PROPOSED | ACCEPT | High | Indexer and command root checks exist; security tests exist | Foundation; supports 004/005/007 | Strengthens protection of P6.11 worktree | Prevents cross-project data/path access | High | Retain fail-closed root behavior | Accept now |
| 009 | Append-only Audit and Event History | PROPOSED | ACCEPT WITH CONDITIONS | High | Multiple append-only triggers already exist; schemas are fragmented | Depends on 001/002 | Preserve P6.11 event streams; no rewrite | Supports forensics and governed integration | Medium | Current-state projection and event correlation contract | Accept conditionally |
| 010 | Artifact, Validation and Evidence Immutability | PROPOSED | ACCEPT WITH CONDITIONS | High | Hash, artifact, test and worker evidence mechanisms exist | Depends on 002/009/015 | P6.11 evidence remains protected | Release integrity and artifact retention implications | Medium | Object-storage boundary and retention policy | Accept conditionally |
| 011 | Provider Token, Quota and Credential Governance | PROPOSED | REQUIRES EVIDENCE | Medium | Existing resource limits only; no verified provider inventory or cost policy | Depends on 005/006/015 | No P6.11 change | Credential, budget and data classification risk | High before credentials exist | Credential model, provider telemetry and cost-center policy | Defer pending evidence |
| 012 | Knowledge Memory Provider Compatibility | PROPOSED | DEFER | High | Correct Phase-2 boundary; no provider selection/benchmark is authorized | Depends on 004/008 | No P6.11 change | Prevents GBrain lock-in and data-authority drift | High | Phase-2 provider evaluation and export criteria | Defer to Phase 2 |
| 013 | Obsidian Human Workspace and Projection | PROPOSED | ACCEPT | High | P6.8 control/projection artifacts and architecture documents support the direction | Foundation; depends on 001/002/008 | Existing P6.11 projections remain read/projection compatible | Prevents direct vault state mutation | High | Source-of-truth and drift markers | Accept now |
| 014 | Local, WSL, Docker, Cloud and Hybrid Deployment | PROPOSED | ACCEPT WITH CONDITIONS | Medium | Native/WSL/Docker runtime contract exists; cloud/hybrid unvalidated | Depends on 002/005/008 | No P6.11 deployment change | Mode drift and remote data risk | High before deployment changes | Capability matrix, offline and remote policy tests | Accept conditionally |
| 015 | Human Approval and High-risk Gates | PROPOSED | ACCEPT WITH CONDITIONS | High | Existing approvals, maker-checker and human gates exist | Depends on 001/002/008/009 | Preserve P6.11 gates; do not unify yet | Prevents agent self-approval and high-risk bypass | Medium | Approval scope, expiry, revocation and role tests | Accept conditionally |

## Decision grouping

- Recommended foundational acceptance now: ADR-002, ADR-003, ADR-008, ADR-013.
- Recommended foundational conditional acceptance: ADR-001, ADR-004.
- Recommended conditional architecture acceptance: ADR-005, ADR-007, ADR-009, ADR-010, ADR-014, ADR-015.
- Requires evidence: ADR-006, ADR-011.
- Deferred to Phase 2: ADR-012.

## A2 human ratification overlay

Decision ID: `PADOS-VNEXT-RATIFICATION-A2-OPTION-A`  
Decision date: `2026-07-13`  
Selected option: `OPTION_A_FOUNDATIONAL_ONLY`  
Implementation authorization: `NOT GRANTED`

| ADR ID | Recommended Decision | Human Ratification Decision | Effective Governance Status | Conditions | Implementation Authorized | Decision Date |
| --- | --- | --- | --- | --- | --- | --- |
| ADR-001 | ACCEPT WITH CONDITIONS | ACCEPTED WITH CONDITIONS | PROPOSED with recorded conditional ratification | Preserve P6.11; CR-01 remediation plan; contract tests; staged validation baseline; incremental path; no Phase B authority | No | 2026-07-13 |
| ADR-002 | ACCEPT | ACCEPTED | ACCEPTED | PostgreSQL remains formal operational authority | No | 2026-07-13 |
| ADR-003 | ACCEPT | ACCEPTED | ACCEPTED | Modular monolith is the initial delivery model | No | 2026-07-13 |
| ADR-004 | ACCEPT WITH CONDITIONS | ACCEPTED WITH CONDITIONS | PROPOSED with recorded conditional ratification | Preserve P6.7; compatibility contract/alias; inventory/tests; no GBrain coupling; Phase 2 remains separate | No | 2026-07-13 |
| ADR-005 | ACCEPT WITH CONDITIONS | NOT RATIFIED | PROPOSED | None accepted in A2 | No | 2026-07-13 |
| ADR-006 | REQUIRES EVIDENCE | NOT RATIFIED | PROPOSED | None accepted in A2 | No | 2026-07-13 |
| ADR-007 | ACCEPT WITH CONDITIONS | NOT RATIFIED | PROPOSED | None accepted in A2 | No | 2026-07-13 |
| ADR-008 | ACCEPT | ACCEPTED | ACCEPTED | Registered project and repository boundaries remain fail-closed | No | 2026-07-13 |
| ADR-009 | ACCEPT WITH CONDITIONS | NOT RATIFIED | PROPOSED | None accepted in A2 | No | 2026-07-13 |
| ADR-010 | ACCEPT WITH CONDITIONS | NOT RATIFIED | PROPOSED | None accepted in A2 | No | 2026-07-13 |
| ADR-011 | REQUIRES EVIDENCE | NOT RATIFIED | PROPOSED | None accepted in A2 | No | 2026-07-13 |
| ADR-012 | DEFER | DEFERRED | PROPOSED | Explicitly Phase 2 only | No | 2026-07-13 |
| ADR-013 | ACCEPT | ACCEPTED | ACCEPTED | Obsidian remains human workspace/projection, not database/runtime/memory engine | No | 2026-07-13 |
| ADR-014 | ACCEPT WITH CONDITIONS | NOT RATIFIED | PROPOSED | None accepted in A2 | No | 2026-07-13 |
| ADR-015 | ACCEPT WITH CONDITIONS | NOT RATIFIED | PROPOSED | None accepted in A2 | No | 2026-07-13 |
