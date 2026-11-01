# PADOS Operability Residual Register

Sidecar to [`PADOS_ENGINEERING_OPERABILITY_AUDIT_2026-11-01.md`](PADOS_ENGINEERING_OPERABILITY_AUDIT_2026-11-01.md) and
the [remediation plan](../superpowers/plans/2026-11-01-pados-operability-remediation.md). The audit and the plan stay
frozen as imported (byte-identical to commit `9220e5d`). This file only adds tracking.

- Audit baseline: `6f5eecb6a98cc61edabdbba7a2a653ee0aa2d416`, as written in the plan.
- Implementation baseline for this round: `main` at `69716ed91237d0bc9ceba41404aedec42f4a7c3e`, the merge of PR #19 into
  the audit baseline. Findings that cite source lines refer to the audit baseline; this round changes the code at the
  implementation baseline.
- Not imported from `9220e5d`: the three architecture-explorer files under
  `runtime/artifacts/reports/pados-architecture-explorer-20261031/`. They are machine-local report artifacts, not
  product documentation. The audit's closing reference to that `index.html` points to the operator's local copy.

## OP numbering: audit backlog vs remediation plan

The audit backlog and the plan number two items differently. This round uses the **plan's** numbers for OP-001 to
OP-007, because the plan is the implementation contract, and the **audit's** numbers for OP-008 to OP-010, which exist
only in the audit.

| This round | Plan item | Audit backlog item |
|---|---|---|
| OP-001 | Cross-boundary context and error contract | OP-001 |
| OP-002 | Runtime validation gate | OP-002 |
| OP-003 | Health semantic convergence | OP-003 |
| OP-004 | Deadline, retry, idempotency and lease propagation | OP-004 |
| OP-005 | Failure injection and recovery runbooks | **OP-006** (recovery path) |
| OP-006 | CI operability gate | **OP-005** (CI proves no operability) |
| OP-007 | Browser request deadline and stale action | OP-007 |
| OP-008 | (not in plan) | OP-008 metrics and trace spans |
| OP-009 | (not in plan) | OP-009 pack/adapter operability contract |
| OP-010 | (not in plan) | OP-010 naming and documentation consistency |

## OP status (branch `feat/operability-remediation-20261101`)

"Implemented" means code, tests and docs are on the branch. It does not mean runtime acceptance: per-lane runtime
evidence for the final commit is listed under "Validation" below, and GitHub CI counts only once a run exists for
that commit.

| OP | Status | What landed | Remaining (with unblock condition) |
|---|---|---|---|
| OP-001 | Implemented | `pados.operability` context and `pados.error.v1` envelope across API, TaskLoop, worker jobs, capability executor, CLI, healthcheck and browser (OP-007). The trace is stored beside sealed content, never inside it. | None in scope. |
| OP-002 | Implemented | Lane registry, per-target gate (report and `--claim`), and an index builder and `verify` (`runtime_lane_index.py`) that take a PASS only from a machine-written runner record for the same commit. The doc states that the gate proves binding, not content truth. CI runs the report. | Required `production` lanes stay NOT_VALIDATED or FAIL until their evidence exists (see R19-3, R19-6, and the REA/OpenMAIC/AI-Scientist-v3 lanes). |
| OP-003 | Implemented | `/health/live` and `/health/ready` (`pados.health.v1`), bounded probes, and no driver text in `/health`. | Corpus lane readiness is not in `/health/ready` (gap C08 in the operability registry). |
| OP-004 | Implemented | Parent deadlines, bounded retry, and idempotency content conflicts for messages, work requests, approvals, task goals, task plans and loops. A route key reused for a different route is refused by the audited approval-binding check, not by a generic conflict. A mutation inventory (approval decisions are STATE_GUARDED). TaskLoop lease renewal and fencing for named owners; owner-less operator steps only observe the lease. | Fencing protects only callers that name themselves; a multi-instance scheduler must pass its instance id. Cross-database fencing (R19-1). |
| OP-005 | Implemented | Deterministic failure injection (`failure_injection` marker) and component recovery runbooks. | Runbooks for C06, C12, C13 and C17 stay listed gaps. |
| OP-006 | Implemented | `operability_gate.py` with 17 components, run in CI; dangling references fail and gaps stay INCOMPLETE. | 6 components INCOMPLETE with named gaps. |
| OP-007 | Implemented | `web/src/http.ts`: request id and envelope; a read deadline that covers the body; writes with no client deadline; REQUEST_TIMEOUT / WRITE_OUTCOME_UNKNOWN / SESSION_STALE; safe-read-only retry; duplicate-write protection. The remote CORS allow-list carries the new headers. | Remote PWA notices show the code, not the guidance text. |
| OP-008 | Foundation implemented | Bounded metrics catalog (method label limited to standard methods, a cap of 2048 series per metric), `/metrics`, and W3C `traceparent` propagation in logs and downstream headers. | Exporter, dashboards, alerts and span export need deployment infrastructure. Other processes do not serve `/metrics` yet. |
| OP-009 | Implemented | Cross-pack contract (`pack_operability.py`): a checker per output, retryable codes transient-shaped, refusals never retryable, health is a bool. | Retry limits and health intervals need a manifest schema v2: an owner decision, since it changes every pack digest. |
| OP-010 | Implemented | Glossary, and a doc reference lint over all of `docs/` (0 findings) run in CI. | Persisted field names are not renamed (by design). |

## Independent review (2026-11-02)

An independent review of `origin/main...feat/operability-remediation-20261101` found no breach of the context
invariant: trace stays beside sealed content and never enters receipts, ledger hashes or CAS. Its eight findings and
their outcomes:

| # | Severity | Finding | Outcome |
|---|---|---|---|
| 1 | HIGH | Owner-less loop steps renewed or revived any holder's lease, blocking takeovers | Fixed in `2262f95`, with PostgreSQL tests that failed before the fix |
| 2 | HIGH | Remote CORS refused the new browser headers, breaking the cross-origin Remote console | Fixed in `fed562e`, with a preflight test that failed before the fix |
| 3 | MEDIUM | Writes inherited the 15 s read deadline, and the budget header capped server model calls | Fixed in `76c2ba5`, with a test that failed before the fix |
| 4 | MEDIUM | The lane index took PASS, commit and freshness from the spec author | Fixed in `b5f1c8d`: PASS only from a machine-written runner record |
| 5 | MEDIUM | Task goal and loop creates silently returned the old row on key reuse | Fixed in `2262f95` (approval decisions reclassified), with tests that failed before the fix. A named route conflict added there was reverted: it pre-empted the audited RC2 D1 approval-binding refusal, and the VMware PostgreSQL gate caught it. |
| 6 | LOW-MEDIUM | Arbitrary request methods could fill the metrics series cap | Fixed in `f3a8765` |
| 7 | LOW | Each readiness request started a probe thread; a hung database accumulated them | Fixed in `f3a8765` (one probe in flight per app) |
| 8 | LOW | The browser read deadline stopped at the response headers | Fixed in `76c2ba5`, with a test that failed before the fix |

## Standard adoption checklist (§10), branch status

The checklist in [`PADOS_SERVICE_ENGINEERING_STANDARD.md`](../PADOS_SERVICE_ENGINEERING_STANDARD.md) is per component;
it stays unticked there. This is the status of the shared mechanisms each component now inherits. Per-component
coverage is the operability gate report (`scripts/release/operability_gate.py`).

| Checklist line | Status | Where |
|---|---|---|
| Purpose, I/O, state, dependencies, API, events, resources, security documented | Partial: contracts resolve for 17 components, with 6 listed gaps | `scripts/release/operability_contracts.v1.json` |
| Request/correlation/run/project ids preserved across boundaries | Met | OP-001; `docs/operations/CONTEXT_AND_ERROR_RUNBOOK.md` |
| Structured, redacted logs for outcome, refusal, retry, timeout, lease loss, recovery | Met for the shared paths | OP-001, OP-004, OP-005 |
| Metrics and trace spans with bounded labels and documented ownership | Foundation met; no exporter | OP-008; `docs/operations/METRICS_AND_TRACE.md` |
| Stable error codes, category, retryability, safe message, write/evidence semantics | Met | `pados.error.v1`; OP-001, OP-007 |
| Deadline, retry, idempotency, lease/heartbeat explicit and tested | Met in one database; cross-database open (R19-1) | OP-004 |
| Startup/liveness/readiness/degraded health tested | Met for the core API; corpus readiness is a gap | OP-003 |
| Failure injection and recovery tests, or N/A justified | Failure-injection references resolve for all 17 components; recovery runbooks are gaps for C06, C12, C13, C17 | OP-005, OP-006 |
| Runbook distinguishes evidence from assumptions | Met where present | `docs/operations/RECOVERY_RUNBOOKS.md` |
| CI/CD enforces the contract and reports skipped/not-validated lanes | Met | OP-002, OP-006, OP-010 CI steps |

## Residuals carried from PR #19 (still OPEN; merging PR #19 did not resolve them)

| ID | Residual | Status | Why it stays open | Tracked with |
|---|---|---|---|---|
| R19-1 | **Cross-database revocation race.** The Product Loop re-checks its approval (operational database) immediately before the state-proof CAS commit (proof database). The two checks and the commit are separate transactions, so a withdrawal in that instant is not caught. | OPEN | No cross-database atomicity exists. The OP-004 TaskLoop lease fences loop owners inside one database; it does not cover this race. **Unblock:** an owner-approved design where the proof-database commit is conditioned on an approval epoch token that a withdrawal increments, plus a PostgreSQL race test. The Product Loop stays disabled by default meanwhile. | OP-004 (lease/fencing); `docs/product/PUBLIC_API_BOUNDARY.md`, "Product Loop dispatch authority" |
| R19-2 | **Cloud egress classification covers only labels.** Cloud dispatch is gated on the project's recorded classification plus bundled enterprise evidence. Workspace file contents are not classified; the project label is the operator's statement about them. | OPEN | Content classification needs a policy decision and a classifier. Neither is authorized in this round. **Unblock:** an owner policy on what content classes block cloud egress, and an approved classifier. | Security backlog; `PUBLIC_API_BOUNDARY.md` |
| R19-3 | **Windows test residuals.** 126 full-suite failures and 25 `pack_focused` failures (28 test ids) are environment-dependent and identical on pristine `main`. Two timing-sensitive tests, unchanged since `main`, sometimes fail under load: `tests/local_intelligence/test_benchmark.py::test_total_deadline_is_total_not_per_read` (Windows) and `tests/local_intelligence/test_stage_c_llama_cpp_adapter.py::test_an_in_place_rewrite_that_restores_the_mtime_is_not_served_from_the_cache` (Linux). | OPEN | They are not fixed. Each run reports them by comparing failure sets, never as PASS. **Unblock:** per-test triage on a Windows host (most need POSIX semantics or are fail-closed by design on win32, e.g. CR13d-01 operator issuance). | OP-002: lane `windows-full-suite` (`known_status FAIL`, required for `production`); OP-006 |
| R19-4 | `pados init` in a home first initialized with the development-only `PADOS_CLI_DEV_*` overrides refuses with `RUNTIME_RECORD_MISSING`; `--adopt-runtime` is the consent. | RESOLVED IN BRANCH (`3ea37f4`), pending final validation | Documented in `docs/install/TROUBLESHOOTING.md` beside the development overrides. A doc-only change; the behaviour was already fail-closed. | Docs |
| R19-5 | `--adopt-runtime` combined with a version mismatch still refuses (`UPDATE_REQUIRED`); the behaviour is correct in code but not pinned by a test. | RESOLVED IN BRANCH (`3ea37f4`), pending final validation | `packages/cli/test/inst1-init-consent.test.js` now runs the refusal with `--adopt-runtime` and `--adopt-runtime --yes`. The code was already correct, so the test pins existing behaviour; it did not fail first. | CLI tests |
| R19-6 | Live local model: measured execution was validated on Ollama 0.40.13. Model output never becomes a ResultCandidate and never reaches the CAS; that path does not exist by design. | OPEN (as a limitation) | MODEL OUTPUT != TRUTH. Report it as `NOT_VALIDATED` for governed acceptance, never as PASS. **Unblock:** a governed acceptance path for model output, approved by the owner. It does not exist by design today. | OP-002: lanes `local-model-governed-acceptance` (`NOT_VALIDATED`, required for `production`) and `local-model-measured-execution` (`does_not_cover` lists ResultCandidate/CAS, REA, OpenMAIC, AI-Scientist-v3) in `scripts/release/runtime_lanes.v1.json` |
| R19-7 | Earlier accepted LOW findings: no POSIX owner-only bits for local secrets on Windows (profile ACLs instead); the reference probe has no FIFO guard on POSIX; a stop request issued during server startup can be dropped. | OPEN (LOW) | Accepted for local dogfood, not resolved. The "probe" in the original finding could not be pinned to one code path in this round, so nothing was changed on a guess. **Unblock:** name the exact file from the PR #19 review record; then open it with `O_NONBLOCK \| O_NOFOLLOW` and an `fstat` regular-file check, with a POSIX test on the VMware lane. The startup stop race needs a stop flag checked after start. Windows ACLs stay as they are. | Security backlog |
| R19-8 | GitHub reports Dependabot alerts on `main`. They predate PR #19 and were outside its scope. | OPEN | Not triaged; no dependency change is authorized. On 2026-11-02 GitHub listed 27 open alerts: 3 critical (`npm:tinypool`, `pip:certifi`), 12 high (`npm:vite`, `npm:source-map-js`, `pip:pypdf`), 10 medium and 2 low. **Unblock:** owner authorization for a dependency-upgrade branch with lockfile, regression and supply-chain gates. | Supply-chain backlog |

Rules: a residual leaves this table only with a commit that fixes it, a regression test that failed before the fix, and
validation evidence. Merging a PR closes nothing on its own.
