# PADOS Operability Remediation Plan

Status: queued for the current capability/runtime development round
Baseline: `6f5eecb6a98cc61edabdbba7a2a653ee0aa2d416`
Owner: current PADOS implementation pass (Claude/Codex)
Source of truth: `docs/audits/PADOS_ENGINEERING_OPERABILITY_AUDIT_2026-11-01.md`

## Objective

Close the P0/P1 Engineering Operability gaps identified in the audit while preserving PADOS's existing fail-closed authority, single scheduler, approval boundaries, evidence/CAS ownership, and platform-specific validation rules.

This plan is intentionally additive. It turns the audit into implementation work for the current development round; it does not authorize a new scheduler, router, policy engine, semantic acceptance layer, or hidden retry loop.

## Non-negotiable constraints

- Do not bypass or duplicate capability authority, approval, TaskLoop ownership, evidence receipts, CAS/state-proof, or semantic acceptance.
- Do not convert `SKIPPED`, `NOT_VALIDATED`, or mocked provider results into `PASS`.
- Do not claim REA, OpenMAIC, AI-Scientist-v3, Kubernetes, or external-provider production readiness without fresh raw evidence.
- Keep Windows and VMware/Linux evidence separate. A skipped or blocked lane remains explicit.
- Preserve existing user-generated evidence and audit artifacts. Add sidecars or patches; do not rewrite frozen results.
- Every new retry, timeout, lease, or recovery behavior must be bounded, observable, and covered by a deterministic failure test.

## P0 work items — acceptance blockers

### OP-001: Cross-boundary context and error contract

Add a shared request/operation context and safe error envelope at existing boundaries:

- API ingress and `/remote/v1` listener;
- TaskLoop and job/provider dispatch;
- capability execution receipts/evidence;
- browser responses where the API already returns an error;
- CLI/healthcheck output where a request or run is available.

Required fields: `request_id`, `correlation_id`, `run_id`/`job_id`, `project_id`, `component`, `outcome`, `error_code`, `retryable`, `write_occurred`, and bounded duration/dependency data. Do not log credentials, raw prompts, private keys, or unrestricted payloads.

Acceptance evidence:

- one versioned error response model with explicit HTTP mapping;
- stable `UPPER_SNAKE_CASE` code and retryability for every new boundary failure;
- a test proving the same context reaches response, structured log/receipt, and downstream call;
- no new authority or scheduler path;
- a redacted example in the runbook.

### OP-002: Runtime validation gate

Make runtime status machine-readable and release-visible for every external or optional lane:

- `PASS`, `FAIL`, `SKIPPED`, and `NOT_VALIDATED` are distinct;
- required unvalidated lanes block production-readiness wording;
- raw evidence path, exact commit, platform, command, exit code, and hash are retained;
- mocked/contract tests are never presented as live runtime validation.

Acceptance evidence:

- CI/release artifact containing per-lane status and provenance;
- negative test for a missing or stale runtime evidence record;
- PR #16/PR #17 runtime limitations remain visible until actually validated.

## P1 work items — production baseline

### OP-003: Health semantic convergence

Define one versioned health schema for startup, liveness, readiness, required dependencies, and optional degraded dependencies. Converge `/health`, `/ready`, and `system_health` without changing their authority:

- liveness must not claim dependency readiness;
- required dependency failure returns non-ready/503;
- optional dependency failure is `DEGRADED` with a safe reason;
- probes are bounded and non-mutating;
- credentials and raw connection strings never appear in health output.

Add route and failure tests for database unavailable, optional provider unavailable, slow probe, and healthy startup.

### OP-004: Deadline, retry, idempotency, and lease propagation

Use the existing TaskLoop idempotency/lease and capability revalidation mechanisms. Add shared metadata or adapters rather than another scheduler:

- parent deadline and remaining budget are passed to DB/provider/worker calls;
- retries are bounded by attempts and elapsed budget with jitter;
- authorization, validation, authority, schema, and content conflicts are never auto-retried;
- mutating calls require an idempotency key or documented reconciliation path;
- lease acquisition, heartbeat, expiry, fencing, and publication refusal are observable.

Add deterministic tests for duplicate mutation, same-key/different-content conflict, provider timeout, retry exhaustion, lease loss, and restart/resume.

### OP-005: Failure injection and recovery runbooks

For Core API, TaskLoop, workers/providers, Remote, Compute Node, Corpus, and Evidence/CAS, add a minimal runbook:

`symptom → error code → log/metric/trace/receipt → safe recovery → verification`

Add local deterministic failure injection for DB disconnect, provider timeout, malformed response, partial evidence, stale session, and stuck/expired lease. Recovery must state whether retry, reconciliation, human approval, rollback, or forward-fix is safe.

### OP-006: CI operability gate

Extend existing `.github/workflows` gates with a lightweight checklist/validator that checks:

- component contract record exists;
- stable error codes and health schema are present;
- failure-injection marker/test exists for applicable components;
- skipped/not-validated lanes are reported separately;
- artifact manifests and hashes are consistent;
- no public export or generated report includes secrets/local paths.

Keep the existing Linux/Windows lanes and report platform evidence separately. Do not weaken a failing gate to obtain a green result.

### OP-007: Browser request deadline and stale-action behavior

In the existing `web/src/api.ts` request helper and remote client:

- add an abort/deadline budget;
- preserve server error code and `request_id` when present;
- retry only explicitly safe/idempotent reads;
- show stale-session, timeout, and write-unknown outcomes distinctly;
- add tests for abort, timeout, server error envelope, and duplicate action protection.

## Implementation order

1. Define the shared field/error schemas and evidence vocabulary.
2. Implement API/remote context propagation and error response mapping.
3. Thread deadline/retry/idempotency metadata through existing loop/provider boundaries.
4. Converge health semantics and add failure tests.
5. Add runbooks and the CI operability gate.
6. Add browser deadlines and stale-action tests.
7. Re-run static gates, targeted tests, platform lanes, and only then update runtime validation evidence.

## Definition of done for this development round

- [ ] P0 items have machine-readable evidence and no hidden production claims.
- [ ] Core API, Remote, TaskLoop, capability execution, provider/job, and browser paths preserve context and stable errors.
- [ ] Health semantics and deadline/retry/idempotency/lease behavior have deterministic tests.
- [ ] Failure runbooks exist for the highest-risk recovery paths.
- [ ] CI reports operability contract status without replacing existing authority/evidence gates.
- [ ] Windows and VMware/Linux results remain separate and explicitly labelled.
- [ ] Ruff, mypy, targeted tests, and applicable frontend checks are fresh; failures remain visible.
- [ ] No merge/push is claimed as validation; release readiness remains tied to raw evidence.

## Handoff instructions for Claude

Work from the audit and this plan in small, reviewable increments. At the start of each implementation task, state the contract field being closed, the existing authority boundary being preserved, and the evidence/test that will prove it. At the end, report changed files, exact commands, PASS/FAIL/SKIPPED/NOT_VALIDATED, and remaining risks. Do not silently broaden scope into a new scheduler, generalized runtime isolation, provider implementation, or deployment migration.
