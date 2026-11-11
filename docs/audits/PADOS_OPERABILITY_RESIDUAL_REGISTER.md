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

"Implemented" means code, tests and docs are on the branch. It does not mean runtime acceptance. Per-lane runtime
evidence binds to one exact commit and is machine-local: the PR description lists it for the validated head, and
GitHub CI counts only once a run exists for that commit. "Partial" means part of the item is implemented and the
rest is listed with its limit.

| OP | Status | What landed | Remaining (with unblock condition) |
|---|---|---|---|
| OP-001 | Implemented | `pados.operability` context and `pados.error.v1` envelope across API, TaskLoop, worker jobs, capability executor, CLI, healthcheck and browser (OP-007). The trace is stored beside sealed content, never inside it. | None in scope. |
| OP-002 | Implemented | Lane registry, per-target gate (report and `--claim`), and an index builder and `verify`. Each lane's runner-record format and complete set of required checks are fixed in the registry (`source`), and `runtime_lane_sources.read_source` is the single interpreter: the gate re-reads the record on every report and `--claim`, so a hand-edited index (PASS / exit 0 over a failing record) or a spec that narrows the checks gives no PASS. Sources: Windows gate summary, VMware chain summary, E2E result (runner contract v1: the registry's exact step list, each once and PASS, a `full` run; a reduced run is NOT_VALIDATED), the PostgreSQL gate's v1 record (suite reports re-hashed, each bound to its own gate name and exact test paths, per-suite minimums), and a GitHub Actions bundle of the unmodified check-runs, jobs, workflow-runs and annotations responses (each required check bound to its workflow file, job and executed steps in this repository; freshness from the oldest required run; a dependency review that reports an environment blocker is NOT_VALIDATED). A completed failure is always FAIL, never hidden behind a not-validated reason. Every record must carry its own finish time and platform. A lane with no machine source can never PASS. The doc states that the gate proves binding, not content or authorship. CI runs the report. | Required `production` lanes stay NOT_VALIDATED or FAIL until their evidence exists (R19-3, R19-6, and the REA/OpenMAIC/AI-Scientist-v3 lanes). The record bytes are not authenticated (no signature): a forged record in the right format still binds; a SHA-256 proves byte identity, not who wrote the bytes or that the tests ran (pinned by a test). The GitHub workflow identity is the file's path at the commit; what it does is code under review. The Windows and VMware runners are in `scripts/validation/`; a test pins their gate names to the registry (L5). |
| OP-003 | Implemented | `/health/live` and `/health/ready` (`pados.health.v1`), bounded probes, no driver text in `/health`, and an optional `corpus_database` check (probed only once the operational lane answers; an absent database is NOT_CONFIGURED). | C07 (remote listener), C12 (learning) and C17 (optional projections) have no health contract of their own; C08 still lacks a contract record. |
| OP-004 | Partial | Parent deadlines reach PostgreSQL statements (`statement_timeout` per transaction, honest `write_occurred`), registered command workers, model provider calls and compute-node calls; bounded retry; idempotency content conflicts for messages, work requests, approvals, task goals, task plans and loops, and a replay records no second capture event. A route key reused for a different route is refused by the audited approval-binding check, not by a generic conflict. A mutation inventory (approval decisions are STATE_GUARDED). TaskLoop lease renewal and fencing for every step: an owner-less operator step acts as the loop's starting owner and is fenced like any other; a deferred model result is recorded only while the stepping owner still holds the lease (otherwise the job goes back to ROUTED for the holder, `LOOP_LEASE_LOST`), and the resumed iteration re-checks it; `LOOP_STARTED` names only an owner that got the lease. | Deadline limits: persisted job deadlines do not take the parent budget (by design), sessions outside `session_scope` and work handed to another thread carry none, and the statement timeout is per statement. The lease is not renewed during a model call longer than five minutes (a takeover is safe, not prevented). Owner-less steps share the starting owner's identity, so two operator surfaces on one loop are serialised (row lock, single active iteration) but not fenced from each other; a multi-instance scheduler must pass its instance id. Cross-database fencing (R19-1). |
| OP-005 | Implemented | Deterministic failure injection (`failure_injection` marker) and recovery runbooks for every component, including the Product Loop, learning loop, capability packs and optional projections. | Where the code offers no recovery tool (truncating an unanchored advice-log entry, reclassifying a project), the runbook says so. |
| OP-006 | Implemented | `operability_gate.py` with 17 components, run in CI; dangling references fail and gaps stay INCOMPLETE. | 4 components INCOMPLETE with named gaps: C07 and C12 and C17 (health), C08 (contract record). COMPLETE means the records exist; C06 and C13 still report their runtime lanes as NOT_VALIDATED. |
| OP-007 | Implemented | `web/src/http.ts`: request id and envelope; a read deadline that covers the body; writes with no client deadline; REQUEST_TIMEOUT / WRITE_OUTCOME_UNKNOWN / SESSION_STALE; safe-read-only retry; duplicate-write protection scoped per client (two remote sign-ins never share a request or a step-up challenge). The remote CORS allow-list carries the new headers, and remote PWA notices give per-outcome guidance with the request id. | None in scope. |
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

## Pre-merge review (2026-11-03)

Six problems were found in the review of the branch before merge. The two P1 bypasses were reproduced on copies of
the `402aa45` evidence before the fix: the hand-edited index gave `PASS / VERIFIED`, and the `ruff`-only spec built a
PASS. Each fix has a regression test that failed on the previous code. The one exception, R19-7's Windows run, is
noted below.

| Finding | Fix |
|---|---|
| P1: the lane gate trusted a hand-edited index, and a spec could choose the gates that make a lane PASS | `39269ee`: registry-bound sources, re-read at the gate |
| P1: owner-less TaskLoop steps were never fenced; the deferred model completion dropped the owner | `577603c`, refined in `1a7d957` |
| P2: the remote single-flight map was shared between clients | `b1ec08f` |
| P2: the parent deadline did not reach PostgreSQL or command workers | `6a58380`, refined in `4e83d5d`. OP-004 is marked Partial with its limits |
| P2: an idempotent replay recorded a second capture event | `7cccfa7`, with a cross-transaction case in `15a29b4` |
| R19-7 candidate: the Markdown reference probe blocked on a FIFO | `785fc28`. VMware/Linux: the old probe hit the 20 s guard and the fix passed. On Windows the test is skipped (no FIFOs) |

An independent review of `402aa45..59ab085` found no CRITICAL or HIGH issues:

| # | Severity | Finding | Outcome |
|---|---|---|---|
| M1 | MEDIUM | A GitHub check-runs record from a fork (same SHA, same app) read as PASS | Fixed in `a8ab8c4`: the repository is bound and the app id pinned |
| M2 | MEDIUM | `write_occurred` was NO after an earlier commit in the same scope | Fixed in `4e83d5d` |
| M3 | MEDIUM | A fenced owner wrote FAILED onto the new holder's job | Fixed in `1a7d957`: the job is handed back as ROUTED, re-run and completed |
| M4 | MEDIUM | The optional Corpus probe could starve the required readiness checks | Fixed in `8e39a8f` |
| L1–L4 | LOW | A default platform, an mtime fallback for staleness, no suite minimums, and WSL invisible to the PostgreSQL record | Fixed in `a8ab8c4` |
| L5 | LOW | The Windows and VMware runners are not in the repository, so the gate names are not cross-checked | Fixed (branch `feat/runtime-hardening-20261111`): `scripts/validation/` holds the Windows runner, its lane-record converter and the VMware chain; `tests/release_supply_chain/test_validation_runners.py` keeps their gate names equal to the registry's required checks |
| L6 | LOW | `LOOP_STARTED` could name an owner that never got the lease | Fixed in `1a7d957` |
| L7 | LOW | The statement timeout is per statement | Documented (OP-004, Partial) |
| L8 | LOW | `xmax = 0` replay was tested only within one transaction | Test added in `15a29b4` |

## Pre-merge review, round 2 (2026-11-04)

Codex re-checked `9c91a88`. The hand-written index and narrowed-gate fixes held; three binding gaps remained. Each
was reproduced on the real `9c91a88` evidence before the fix (a 24-step Windows E2E record cut to one step, the
residual suite report copied into the migration slot and re-hashed, one required check 40 days old: all read as
PASS) and refused after it.

| Finding | Fix and test |
|---|---|
| E2E required steps were not bound; a `--skip-acceptance-suite` run could stand in for the full lane | `c6ef880`: the registry lists the step list (a test keeps it equal to the runner's), the runner records `runner_contract` and `run_mode`; missing, repeated, unexpected and renamed steps and a reduced run are refused (`test_op002_lane_source_identity.py`) |
| A PostgreSQL suite report was bound by hash, commit and counts only | `c6ef880`: gate name and exact test paths per suite, none missing or empty (`SUITE_PATHS`, kept equal to the registry by a test); cross-suite copy, wrong, missing, extra and reordered paths, too few tests |
| GitHub freshness came from the newest check, and a check was matched by name, repository, commit and app only | `c6ef880`: per-check freshness (oldest decides; any future run fails), workflow file, job and step identity, `SOURCE_CHECK_NOT_EXECUTED` and `SOURCE_CHECK_ENVIRONMENT_BLOCKED`. PR #21's dependency review at `3b01111` really executed and still reads as PASS |

Independent review of this round (no CRITICAL or HIGH):

| ID | Severity | Finding | Status |
|---|---|---|---|
| R2-M1 | MEDIUM | A failed required GitHub check behind an incomplete one, or a failed reduced E2E run, read as NOT_VALIDATED | Fixed in `ae1a3c4` (failed parts first; RED on the previous reader, 5 tests) |
| R2-L3 | LOW | Re-run attempts relied on the API default | `ae1a3c4`: the collector asks `filter=latest` |
| R2-L4 | LOW | Duplicate check-run ids were accepted | `ae1a3c4`: refused |
| R2-L5 | LOW | How the jobs API reports a failed `continue-on-error` step is unconfirmed | Both outcomes are NOT_VALIDATED (documented); not pinned with a captured failing payload |

Running the Playwright suite (not part of CI) also showed two Remote console regressions from OP-007: network loss and
timeouts arrive as `RemoteApiError`, so the console never showed OFFLINE or RECONNECTING. Fixed in `5ca7711`
(`homeBaseUnreachable`); the refused-task spec now expects the OP-007 wording.

## Standard adoption checklist (§10), branch status

The checklist in [`PADOS_SERVICE_ENGINEERING_STANDARD.md`](../PADOS_SERVICE_ENGINEERING_STANDARD.md) is per component;
it stays unticked there. This is the status of the shared mechanisms each component now inherits. Per-component
coverage is the operability gate report (`scripts/release/operability_gate.py`).

| Checklist line | Status | Where |
|---|---|---|
| Purpose, I/O, state, dependencies, API, events, resources, security documented | Partial: contracts resolve for 17 components, with 5 listed gaps across 4 components | `scripts/release/operability_contracts.v1.json` |
| Request/correlation/run/project ids preserved across boundaries | Met | OP-001; `docs/operations/CONTEXT_AND_ERROR_RUNBOOK.md` |
| Structured, redacted logs for outcome, refusal, retry, timeout, lease loss, recovery | Met for the shared paths | OP-001, OP-004, OP-005 |
| Metrics and trace spans with bounded labels and documented ownership | Foundation met; no exporter | OP-008; `docs/operations/METRICS_AND_TRACE.md` |
| Stable error codes, category, retryability, safe message, write/evidence semantics | Met | `pados.error.v1`; OP-001, OP-007 |
| Deadline, retry, idempotency, lease/heartbeat explicit and tested | Partial: met in one database with the listed deadline limits; cross-database open (R19-1) | OP-004 |
| Startup/liveness/readiness/degraded health tested | Met for the core API, including the optional Corpus lane; C07, C12 and C17 have no health contract | OP-003 |
| Failure injection and recovery tests, or N/A justified | Failure-injection references and recovery runbooks resolve for all 17 components | OP-005, OP-006 |
| Runbook distinguishes evidence from assumptions | Met where present | `docs/operations/RECOVERY_RUNBOOKS.md` |
| CI/CD enforces the contract and reports skipped/not-validated lanes | Met | OP-002, OP-006, OP-010 CI steps |

## Local usability (branch `feat/local-usability-20261103`, PR #21)

Scope: one user, this computer, supervised, synthetic or non-sensitive data. Found by walking the journeys on
Windows 11 from a fresh clone (round 1), then by Codex review of `3b01111` (round 2).

| ID | Finding | Status |
|---|---|---|
| F1 | `pados doctor --json` printed a log event after the JSON | Fixed (`39789de`, `c1e90d6`): CLI log events go to standard error |
| F2 | The quickstart's web URL is the Remote sign-in | Fixed (`b08cf6f`, `3b01111`): the sign-in links the local console; the launcher opens `/operator.html` |
| F3 | An empty project list said "Loading projects…" forever | Fixed (`b08cf6f`) |
| F4 | The control token could decide a ResultCandidate's approval and strand the candidate (no CAS bypass) | Fixed (`2d4937f`): `RESULT_ACCEPTANCE_REQUIRES_STEP_UP` |
| F5 | A step on a paused or cancelled loop said only "conflict" | Fixed (`eea457a`): `LOOP_NOT_RUNNING: the loop is …` |
| F6 | The E2E never checked `/health/live` and `/health/ready` | Fixed (`a4da7e7`) |
| U1 | The inbox's Open for a result acceptance pointed at an API route | Fixed (`5603cf7`): inbox items carry `open`; it opens the Remote console's Actions view, whose sign-in names what is needed (a Remote sign-in and an enrolled WebAuthn device). Playwright and unit tests |
| U2 | Page links were absolute and left a sub-path build (`/Pados/`) | Fixed (`5603cf7`): joined to Vite's `BASE_URL`; checked in Chromium on a `/Pados/` build |
| U3 | An unknown `PADOS_LOG_LEVEL` crashed every command, `--help`, `init` and `doctor` included | Fixed (`929aacf`, value no longer echoed in `fc95ac4`); `doctor` outside the checkout now says `WORKTREE_IMPORT_PATH_MISMATCH` |
| U4 | Comments said the step-up proof is consumed in the decision transaction | Corrected (`93d6af7`, `fc95ac4`): consumption commits on its own, the approval and candidate decision commit together later, and a REJECT has a third step. Not atomic; a PostgreSQL test pins the failure between them (proof spent, nothing decided, replay refused, a fresh step-up decides) |
| U5 | The web launcher ran uvicorn directly, skipping `pados serve`'s start-up refusals | Fixed (`1544b76`, `fc95ac4`): it runs `pados serve`; Pester covers the default token, a non-loopback host and a unique token (Pester is not part of CI) |
| U6 | Work requests and approvals needed API scripts | Fixed (`a86d860`): the Control Center creates work requests and approves, rejects or revises work package approvals through the existing `/control` routes and control token, after the person confirms the exact approval; result approvals stay step-up only |
| U7 | A work request made in the Control Center had no TaskEnvelope, so after its approval a plan bound to a product goal (the ResultCandidate path) was refused with `TASK_ENVELOPE_REQUIRED_FOR_RUNTIME_BINDING` (found in the 2026-11-09 human walkthrough) | Fixed: the form asks for the capability the work needs; `POST /control/work-requests` with `requested_capability` captures the envelope server-side through the existing intake helper (as the Remote console does), bound to the project's classification and the current ScopeVersion; a retry with the same key (also a concurrent one) returns the stored record. Requests without a capability are stored as before. The capability only narrows: a bound step needing another one is refused before it routes (`CAPABILITY_BINDING_INVALID`). The Remote console task list now selects remote intents only, so local tasks cannot fill its limit. Limits: the envelope keeps its capture-time ScopeVersion (a stale request needs a new one); locally captured tasks do not appear in the Remote console task list. Unit, PostgreSQL (`d1-binding`) and Playwright tests |
| U8 | The live dogfood E2E gave each `/api/health` probe 2 seconds, but one answer takes about 2-3.5 s on the validation hosts (it opens fresh engines for two databases and reads metrics), so a healthy server could fail the run (VMware `-claude-20261110-r2` at `af6d0b3`: `pados serve did not become healthy`) | Fixed in the E2E harness: a probe may wait up to 30 s for its answer, the whole wait is bounded at 300 s, and a server that exits is still refused at once. Product behavior is unchanged. Unit tests. The slow endpoint itself was then fixed in branch `feat/runtime-hardening-20261111` (`ed21a75`):
  - One engine per database.
  - The table list is read once.
  - Plain `count(*)` with no table reflection. |
| U9 | When the live dogfood E2E failed while starting `pados serve`, it left that server running: the caller's cleanup had no handle to it. The server kept the API port, and the next run's health probe was answered by it (VMware `-claude-20261111` at `eb112a3`: 403 for every token, not ready, against a removed database) | Fixed in the E2E harness: a server that does not become healthy is stopped before the error is raised, and a run refuses to start when anything already listens on its API port, so a run only ever talks to the server it started. Product behavior is unchanged. Unit tests |
| R2-M2 | (review) A build served from another host would have sent the typed control token there | Fixed (`448427f`): the token field and every control request refuse off loopback (`CONTROL_TOKEN_LOCAL_ONLY`) |
| R2-L6–L9 | (review) the reject step, the echoed log value, a `localhost` backend, one idempotency key per request and late approval responses | Fixed (`fc95ac4`, `448427f`) |

Still open for local use (each needs a product or owner decision, not a code fix in scope):

| Gap | Why | Unblock |
|---|---|---|
| Approval requests (WORK_ROUTE subjects), goals, plans and loops have no form | Writing the subject (capability, command, worker, maximum risk) decides what work may run | A product decision on which commands a person may request from the console, then a form over `POST /control/approvals` |
| Project creation is CLI-only | No API route creates a project | Decide whether projects are created from the web |
| A result is accepted only in the Remote console | Needs `PADOS_REMOTE_CONFIG`, an actor and an enrolled WebAuthn device | The owner configures Remote Operations and enrols a device |
| The control token is pasted into the console each time | Kept in page memory only, by design | A local session exchange would be new authority design |
| Graceful stop (Ctrl+C) was not exercised; only a hard stop and restart | Interactive | A manual check by the owner |
| Real local model run (`qwen3:8b`) | Needs a new, exact, temporary `model_ref` authorization | The owner's decision (model, project, purpose, expiry, revocation) |

## Residuals carried from PR #19 (still OPEN; merging PR #19 did not resolve them)

| ID | Residual | Status | Why it stays open | Tracked with |
|---|---|---|---|---|
| R19-1 | **Cross-database revocation race.** The Product Loop re-checks its approval (operational database) immediately before the state-proof CAS commit (proof database). The two checks and the commit are separate transactions, so a withdrawal in that instant is not caught. | OPEN | No cross-database atomicity exists. The OP-004 TaskLoop lease fences loop owners inside one database; it does not cover this race. **Unblock:** an owner-approved design where the proof-database commit is conditioned on an approval epoch token that a withdrawal increments, plus a PostgreSQL race test. The Product Loop stays disabled by default meanwhile. | OP-004 (lease/fencing); `docs/product/PUBLIC_API_BOUNDARY.md`, "Product Loop dispatch authority" |
| R19-2 | **Cloud egress classification covers only labels.** Cloud dispatch is gated on the project's recorded classification plus bundled enterprise evidence. Workspace file contents are not classified; the project label is the operator's statement about them. | OPEN | Content classification needs a policy decision and a classifier. Neither is authorized in this round. **Unblock:** an owner policy on what content classes block cloud egress, and an approved classifier. | Security backlog; `PUBLIC_API_BOUNDARY.md` |
| R19-3 | **Windows test residuals.** 126 full-suite failures and 25 `pack_focused` failures (28 test ids) are environment-dependent and identical on pristine `main`. Two timing-sensitive tests, unchanged since `main`, sometimes fail under load: `tests/local_intelligence/test_benchmark.py::test_total_deadline_is_total_not_per_read` (Windows) and `tests/local_intelligence/test_stage_c_llama_cpp_adapter.py::test_an_in_place_rewrite_that_restores_the_mtime_is_not_served_from_the_cache` (Linux). The redaction performance tests in `tests/consolidation/test_clean_room_performance.py` (a 1.0 s limit per call) also fail on both platforms (Windows and VMware runs at `eb112a3`): measured idle on 2026-11-11, `redact` on 2 MiB of spaces around a bearer token takes 0.99-1.5 s on the VMware VM and up to 0.8 s on Windows, almost all in the first pattern (the `name = value` keyword detector, about 1.0 s alone), whose leading lookbehind and case-insensitive keyword alternation give the regex engine no literal prefix to skip ahead with. Addressed in branch `feat/runtime-hardening-20261111` (`24fc8d6`), with the limit unchanged:
  - Every detector now starts on a literal the regex engine can skip to.
  - `tests/consolidation/test_redaction_equivalence.py` requires the same decision as the previous patterns, kept
    verbatim as an oracle.
  - The Windows run at `74d8e7a` still failed its own 0.5 s check (0.58 s): measured idle afterwards, 2 MiB of
    whitespace took 0.22-0.5 s on Windows, almost all in the stem gate. A regex over per-letter classes gives the
    engine nothing to skip to. The gate is now a substring search after folding the four IGNORECASE extras and
    lower-casing (`_has_credential_stem`). A test checks every code point that IGNORECASE matches against a stem
    letter folds to that letter. 400,000 generated inputs gave the same decision as the oracle. The same inputs
    now take 0.03-0.13 s on Windows.
  - Still open: text that holds a stem (for example `token`) followed by MiBs of other text reaches the assignment
    pattern itself, which takes about 0.6 s per MiB on Windows. No current test or contract covers that shape.
  - The benchmark fake runtime now sends its first essay chunk at once (`fb59c62`), so the deadline tests no
    longer depend on a 0.5 s margin. | OPEN | They are not fixed. Each run reports them by comparing failure sets, never as PASS. **Triage (2026-11-11, Windows run at `034fce6`, 165 failing cases):**
  - 135 are OCR isolation: `tests/packs/ocr` and the `tests/pipeline` suites. `_isolated.run_bounded` refuses
    on win32 (`OCR_ISOLATION_UNAVAILABLE`) because no Job Object containment exists, so every isolated
    adapter reports unavailable and plans are `NO_FEASIBLE_CANDIDATE`.
  - 28 are operator decision issuance (`tests/capability/test_fabric_cr13*`): `OPERATOR_PLATFORM_UNSUPPORTED`
    on win32.
  - 2 are the benchmark timing tests.
  The first two groups fail closed by design.
  **Unblock:** an owner decision to build Windows Job Object containment for the isolated runner (process,
  memory and CPU limits, kill-on-close, and bounded pipe reads without `selectors`) and a Windows operator
  issuance path. Until then they stay FAIL, never PASS. | OP-002: lane `windows-full-suite` (`known_status FAIL`, required for `production`); OP-006 |
| R19-4 | `pados init` in a home first initialized with the development-only `PADOS_CLI_DEV_*` overrides refuses with `RUNTIME_RECORD_MISSING`; `--adopt-runtime` is the consent. | RESOLVED IN BRANCH (`3ea37f4`), pending final validation | Documented in `docs/install/TROUBLESHOOTING.md` beside the development overrides. A doc-only change; the behaviour was already fail-closed. | Docs |
| R19-5 | `--adopt-runtime` combined with a version mismatch still refuses (`UPDATE_REQUIRED`); the behaviour is correct in code but not pinned by a test. | RESOLVED IN BRANCH (`3ea37f4`), pending final validation | `packages/cli/test/inst1-init-consent.test.js` now runs the refusal with `--adopt-runtime` and `--adopt-runtime --yes`. The code was already correct, so the test pins existing behaviour; it did not fail first. | CLI tests |
| R19-6 | Live local model: measured execution was validated on Ollama 0.40.13. Model output never becomes a ResultCandidate and never reaches the CAS; that path does not exist by design. | OPEN (as a limitation) | MODEL OUTPUT != TRUTH. Report it as `NOT_VALIDATED` for governed acceptance, never as PASS. **Unblock:** a governed acceptance path for model output, approved by the owner. It does not exist by design today. | OP-002: lanes `local-model-governed-acceptance` (`NOT_VALIDATED`, required for `production`) and `local-model-measured-execution` (`does_not_cover` lists ResultCandidate/CAS, REA, OpenMAIC, AI-Scientist-v3) in `scripts/release/runtime_lanes.v1.json` |
| R19-7 | Earlier accepted LOW findings: no POSIX owner-only bits for local secrets on Windows (profile ACLs instead); the reference probe has no FIFO guard on POSIX; a stop request issued during server startup can be dropped. | OPEN (LOW) | The strongest candidate for the probe item, `src/pados/worker/probes/markdown_references.py` `_read_bounded`, was a real defect. On the VMware VM the old probe blocked on a FIFO named `*.md` (killed by the test's 20 s guard). It is fixed in `785fc28` (`O_NONBLOCK`, `fstat` regular-file and size checks). Neither PR #19's GitHub comments nor its reviews name the file, so the original item is not closed on a guess. The startup stop race and the Windows ACL note are unchanged. **Unblock:** confirm from the original review that the probe meant was `markdown_references.py`, then close that part; The startup stop race is fixed in branch `feat/runtime-hardening-20261111` (`1426937`): at startup the server
clears only a stop request older than its own process, so a `pados down` issued while it starts is honoured. | Security backlog |
| R19-8 | GitHub reports Dependabot alerts on `main`. They predate PR #19 and were outside its scope. | OPEN | Not triaged; no dependency change is authorized. On 2026-11-02 GitHub listed 27 open alerts: 3 critical (`npm:tinypool`, `pip:certifi`), 12 high (`npm:vite`, `npm:source-map-js`, `pip:pypdf`), 10 medium and 2 low. **Unblock:** owner authorization for a dependency-upgrade branch with lockfile, regression and supply-chain gates. | Supply-chain backlog |

Rules: a residual leaves this table only with a commit that fixes it, a regression test that failed before the fix, and
validation evidence. Merging a PR closes nothing on its own.
