# PLANS

Date: 2026-07-10
Project branch: `codex/p6-9-worker-router`

## Active Project Type

Personal Codex autonomous development operating layer.

## Risk Level

Current risk: MEDIUM after P6.10 completed with full validation.

Reason: P6.10 adds one approved Task executed through bounded deterministic iterations. P6.10 validation passes; P6.11 is unblocked but remains future work. Docker Desktop remains optional.

## Required Checks

Every sprint checkpoint must include:

- Implemented, non-empty functionality.
- Tests or executable validation.
- Error fixes from validation.
- Acceptance report with commands, exit codes, test result summary, and log paths.
- Checkpoint commit on a non-main branch.

## Forbidden Actions

- Do not read, modify, or copy existing files outside this project directory.
- Do not modify global PATH.
- Do not modify global Codex settings.
- Do not modify Windows system settings.
- Do not install or upgrade global packages.
- Do not merge, push, force-push, or modify `main`.
- Do not claim PASS without real command output, exit codes, result summary, and log paths.

## P6.5 Status

Status: COMPLETED

Result: `REJECTED_PENDING_FIXES`

P6.5 findings are the canonical input for P6.6 remediation.

## P6.6 Status

Status: `COMPLETED`

Completed in code:

- H1: release ZIP preserves repository-relative tree paths.
- H2: release ZIP excludes logs, pycache, runtime, work, outputs, and cache content.
- H3: credential-looking fixture is excluded from release.
- H4: task transition policy is enforced in domain service.
- H5: production loop no longer hardwires `MockProvider`.
- H6: repository indexing validates requested root against registered project root.
- Canonical planning documents added under `docs/`.

Blocked validation:

- Docker daemon returns `Docker Desktop is unable to start`, but Docker is an optional runtime adapter and does not block native core validation.
- Local PostgreSQL 18 is running on port `5432`, and project credentials are accepted for `pados_p6_6_validation`.
- pgvector `0.8.5` is available in source and restore databases.
- Full pytest, DB-backed adversarial tests, `pados doctor`, Alembic up/down/up, native lifecycle, backup/restore, and release regression pass.

## P7 Status

P7 completed its P6.7 Context Memory Foundation slice.

## P6.7 Status

Status: `COMPLETED`

Scope:

- Project Registry
- Run and Event Memory
- Decision Memory
- Verified Fact Memory
- Artifact Registry
- Context Source Registry
- Context Retrieval
- Context Budgeting
- Context Packet Assembly
- Freshness, staleness, evidence and provenance

Exit criteria:

- Governed memory records are project-scoped.
- Run events are append-only and ordered.
- Decisions and supersession preserve history.
- Artifacts are stored as path/hash pointers with integrity validation.
- Hybrid retrieval prefers verified/current/canonical sources.
- Context budgeting is deterministic and records exclusion reasons.
- Context Packet JSON and Markdown projections are stable and auditable.
- Cross-project leakage, path escape, secret leakage, event mutation and hash mismatch tests pass.
- Alembic up/down/up, doctor, full pytest, backup/restore and release regression pass.

Future sequence:

- P6.7 Context Memory Foundation
- P6.8 Obsidian Control Surface
- P6.9 Worker Router
- P6.10 Deterministic Task Loop
- P6.11 Project Goal Loop
- P6.12 Portfolio Loop

SQL Context Memory is built before Loop Engineering because the Loop requires durable Goal, Plan, event, decision, observation and evidence memory.

## P6.8 Status

Status: `COMPLETED`

P6.8 Obsidian Control Surface is completed.

## P6.9 Status

Status: `COMPLETED`

Scope:

- Capability Registry
- Explainable Worker Router
- Worker discovery and health
- Worker Job data model and append-only events
- Context Handoff Packet
- Command Registry and deterministic execution
- Control API worker/job endpoints
- Obsidian worker status projection fields
- Release packaging and audits

Exit criteria:

- Approved Work Request routed to Deterministic Tool Worker.
- Context packet handoff generated with hash and provenance.
- Registered command executed with stdout/stderr capture.
- Worker job persisted as `SUCCEEDED` with validation `PASSED`.
- Worker events and artifacts persisted.
- Worker unavailability and unsafe commands fail closed.
- Alembic up/down/up, doctor, full pytest, backup/restore and release regression pass.

Future sequence:

- P6.10 Deterministic Task Loop
- P6.11 Project Goal Loop
- P6.12 Portfolio Loop

## P6.10 Status

Status: `COMPLETED`

Scope:

- Versioned Task Goal contract
- Versioned Task Plan and Plan Step contract
- Deterministic Loop state machine
- Progress Snapshot and Progress Delta
- Stop Policy and Human Gate decision table
- SQL migration for goals, plans, steps, loops, iterations, leases, snapshots, deltas, and append-only loop events
- Control API and CLI loop surfaces
- Obsidian loop status projection fields
- P6.9 Worker Router integration for one-step iteration execution

Validated:

- Focused ruff, mypy, unit tests, security adversarial tests, and CLI/API contract tests pass.
- Native PostgreSQL acceptance is blocked because the local PostgreSQL credential available to this run failed password authentication.

Completed acceptance gates include native PostgreSQL, Alembic up/down/up, DB-backed deterministic
success and no-progress demonstrations, native lifecycle, backup/restore, plugin validation, full
pytest, release packaging, ZIP audit, release secret scan, and clean extract.

Final remediation:

- The two authorized P6.6 evidence logs were replaced with sanitized canonical evidence.
- Tracked, staged, retained-log, report, release, plugin, and clean-extract secret scans pass with zero findings.
- P6.10 executes one approved Task through bounded deterministic iterations.

## P6.11 Status

Status: `UNBLOCKED_FOR_PROJECT_GOAL_LOOP`

P6.11 may now implement persistent Project Goal governance across multiple P6.10 Task Loops.
P6.11 is not implemented in this run.
