# PADOS local-supervised-dogfood baseline on `main` (2026-11-14)

This is the release baseline after the PR #20 → #25 stack landed on `main` atomically through PR #26, plus the
follow-up security fixes. It records what was decided, by whom, what was verified, and what remains open.

**Scope:** local, single-user, supervised dogfood with synthetic or non-sensitive data, loopback only.
**Production: NOT_READY.** Nothing here is a production-readiness claim.

## 1. What landed

| Item | Value |
| --- | --- |
| `main` before | `69716ed91237d0bc9ceba41404aedec42f4a7c3e` (PR #19 merge) |
| Release candidate | PR #26, head `202df5cc562bff6fd55b6b415d34a55430b116d5` (109 commits, 195 files) |
| RC merge | `1fd5608c70fe84912a91614281aab147d989d4ec`: **Create a merge commit**, parents `69716ed` + `202df5c`; tree `d7982f0` identical to the RC tree |
| Superseded PRs | #21, #22, #24, #25 closed as landed via #26 (each head is an ancestor of `1fd5608`); #20 marked merged by GitHub automatically (its head is in `main`). Branches kept. |
| Follow-up merges | #27 `2515c8a`, #30 `282d343`, #28 `0f8d00b`, #29 `f397135`, #32 `95dcaa0`, #31 `f6db272`, #33 `7043c63` (all merge commits, in that order) |
| `main` at this baseline | `7043c63dae4796c0c62cfff2d94ec9f20cdc26fc` |

No squash, rebase or force push was used. Branch protection was not changed.

## 2. Owner authorization and review status

- The repository owner gave written authorization for the release-convergence workflow, for local-supervised
  dogfood only, and recorded risk acceptance (PR #26 comment, 2026-11-14).
- **Human code review of the cumulative 195-file diff: NOT PERFORMED.** The owner stated the authorization does not
  certify a personal review. Branch protection requires 0 approving reviews and no GitHub approval was created.
- **Independent review:** agent reviews only (PR #26 rounds 1–3; one independent security review per follow-up PR).
  These are inputs, not a human approval.

### Recorded decisions

| # | Decision | Status |
| --- | --- | --- |
| 1 | DI-9 Core artifact-read bound (`capability/artifacts.py`) | Accepted for local dogfood, subject to the boundary tests |
| 2 | P2-01, P2-03, P2-05, N-1, N-2 | Accepted for local use with documented limits; **N-1 since fixed (PR #27)** |
| 3 | P2-11 in-process `pypdf` | Accepted temporarily while untrusted PDF/URL ingestion was out of the supported workflow; **remediated by PR #32** (merged `95dcaa0`; Windows kernel memory cap still open) |
| 4 | Opt-in model execution, OCR rasterization, experimental retrieval | **Disabled by default** |
| 5 | Reranking | **SHADOW**; no automatic ACTIVE promotion |
| 6 | Deferred P2/P3 | Tracked in §6 with acceptance criteria |
| 7 | Production | **NOT_READY** |

## 3. Post-merge validation, bound to `1fd5608`

All evidence is new and bound to the merge commit; nothing was reused from the RC runs. Raw evidence is kept outside
the repository (machine-local evidence directory), as for earlier rounds.

| Lane | Result | Detail |
| --- | --- | --- |
| GitHub CI (push to `main`) | **PASS** for every executed check | pr-ci linux/windows/macos, CodeQL python and js/ts, secret scan and CI policy |
| GitHub `rc-ci` (dispatched, read-only) | **PASS** | packaging linux-x64 and win32-x64, fresh-install migration gate, PostgreSQL integration, RC manifest |
| Windows 11: 18 CI gates | **PASS** 18/18 | `windows_validate.py --profile full` |
| Windows native E2E | **PASS** | private PostgreSQL 18 cluster |
| Windows PostgreSQL integration gate | **PASS** | D1, D4, residual, proof lane, P6-11, migration |
| Windows Pester / Playwright | **PASS** | launcher and web |
| Windows full pytest | **FAIL (baseline-identical)** | 4924 tests, 163 failures, identical by test id to RC `202df5c` and to the `205543a` baseline; 0 new, 0 resolved (R19-3 win32 fail-closed cases). Not reported as PASS. |
| VMware Ubuntu 24.04: 19 gates | **PASS** 19/19 | unmodified `vm_online_chain.sh` (blob `48d7828`), offline recipe |
| VMware Linux E2E | **PASS** | including restart persistence and governed acceptance |
| VMware Linux full pytest | **PASS** | 0 failures |
| Runtime lane gate, `local-supervised-dogfood` | **NOT_READY (tool verdict)** | Only failing required lane: `github-ci`, because the required `dependency review` job is pull-request-only and is skipped on a push to `main`. See OWNER-GATE-1. At the tree-identical RC `202df5c` the same gate was READY. |
| Runtime lane gate, `production` | **NOT_READY** | model, REA, OpenMAIC and AI-Scientist-v3 lanes NOT_VALIDATED |

### Final validation at `main` `7043c63` (after PRs #27 to #33)

| Lane | Result | Detail |
| --- | --- | --- |
| GitHub CI (push) and `rc-ci` | **PASS** | pr-ci, CodeQL, secret scan; packaging, migrations, PostgreSQL integration, manifest |
| Windows 11: gates, E2E, PostgreSQL, Pester, Playwright | **PASS** | 34 of 35 steps exit 0 |
| Windows full pytest | **FAIL (baseline-identical)** | 5024 tests (100 new), 163 failures, identical by test id to `1fd5608`; 0 new |
| VMware run r1 | **18/19, E2E PASS** (kept as evidence) | `frontend` failed: the offline npm cache had no vite 7.3.7 (`ENOTCACHED`), an environment gap after PR #28 |
| VMware run r2 | **PASS 19/19, E2E PASS** | same SHA and unmodified chain; npm cache seeded with the vite 7.3.7 and source-map-js 1.2.2 tarballs, wheelhouse with the new pypdf, certifi and Mako wheels |
| Runtime lane gate | local-supervised-dogfood **NOT_READY (tool)**, production **NOT_READY** | same single cause as at `1fd5608`: `dependency review` skipped on push (OWNER-GATE-1); every other lane PASS/VERIFIED except the baseline-identical Windows full suite |

## 4. Local-supervised-dogfood user journey (real code, executable)

`scripts/dogfood/local_dogfood_e2e.py` runs over real processes, HTTP and PostgreSQL on both platforms. The capability
executed is deterministic (no provider credentials) and the WebAuthn device is the repository's software
authenticator (a real P-256 key), so the journey is **validated synthetic**, not production-validated.

| Journey step | E2E check(s) | Windows | VMware Linux |
| --- | --- | --- | --- |
| Install and initialization | `pados_init_unique_token`, `migrations`, `pados_doctor` | PASS | PASS |
| Refuse unsafe start | `serve_refuses_default_token`, `control_auth` | PASS | PASS |
| Health and error visibility | `health_live_and_ready` | PASS | PASS |
| Project creation | `project_create` | PASS | PASS |
| Work request and governed approval | `approval_required_before_route`, `human_approval` | PASS | PASS |
| Capability planning and execution | `plan_visible`, `execution_completed` | PASS | PASS |
| Evidence and receipt inspection | `execution_receipt` | PASS | PASS |
| ResultCandidate handling | `result_candidate_verified_waiting_approval` | PASS | PASS |
| Fail-closed negatives | broken reference, uncovered step, finished loop, revoked/consumed approval | PASS | PASS |
| WebAuthn step-up | `presence_device_enrolled`, `standing_session_cannot_decide`, `governed_acceptance_live_cas`, `consumed_proof_cannot_be_replayed` | PASS | PASS |
| Final state verification | `governed_acceptance_suite` (ScopeVersion 0 → 1, single CAS) | PASS | PASS |
| Restart and recovery | `restart_persistence`, `acceptance_persists_after_restart` | PASS | PASS |

## 5. Security remediation (separate PRs)

| Track | PR | Status | Summary |
| --- | --- | --- | --- |
| N-1 schema guard | #27 | Merged `2515c8a` | Existence probes resolve on the `search_path` like the queries after them; tested in default and non-default schemas, end to end through `decide_approval` |
| P2-11 PDF parsing | #32 | Merged `95dcaa0` | pypdf 6.20.1 → 6.21.4 (14 advisories); untrusted PDF parsing moved out of the API process into a bounded `python -I` worker (deadlines, size/page/decode/text budgets, Linux rlimits, RSS watchdog, process-tree kill, closed codes) |
| Python pins | #30 | Merged `282d343` | certifi 2026.10.23 (critical emSign advisory), Mako 1.4.3 |
| Web dev toolchain | #28 | Merged `0f8d00b` | vite 7.3.7, source-map-js 1.2.2 (shipped bundle byte-identical) |
| `.env` and control token permissions | #29 | Merged `f397135` | Credentials written owner-only from creation via an atomic replace (CodeQL #8 adjacent gap) |
| Security register | #31 | Merged `f6db272` | `docs/security/SECURITY_REGISTER_2026-11-14.md` |
| Model worker OS isolation | #33 | Merged `7043c63` | Still opt-in. When enabled: Windows Job Object (commit cap, sealed process limit, kill-on-close, suspended start) and Linux RLIMIT_AS plus an unprivileged seccomp filter denying sockets, io_uring, ptrace, process_vm and pidfd; isolation failures fail closed. Windows network isolation is an owner decision |

Alert counts at this baseline: Dependabot 27 → 19 open. The 14 remaining pypdf alerts point at
`scripts/stage_b/requirements-validation.txt`, which pins `pypdf==6.21.4` on `main` since PR #32; they await
Dependabot's next dependency-graph scan (last run at `282d343`, before #32). The other 5 (vitest, @vitest/mocker,
tinypool; dev-only) need the vitest 4 major upgrade (OWNER-DEP). CodeQL: 5 open, all triaged in the security register.

## 6. Residual register (open items)

| ID | Item | Severity | Disposition | Acceptance criteria to close |
| --- | --- | --- | --- | --- |
| P2-01 | Runtime lane sources accept hand-written runner JSON | P3 local / P2 for production | Accepted local | Signed or CI-produced runner records before any production claim |
| P2-03 | `/health`, `/metrics` exposure | P3 | Accepted local (loopback enforced) | Owner decision before any non-loopback bind |
| P2-05 | Redaction word list gaps | P3 | Accepted local | Structured secret typing replaces the word list |
| P2-11 residual | Windows PDF worker has no kernel memory/CPU cap (RSS poll only) | P2 | Accepted local | Windows Job Object containment (OWNER-WIN-JOB) |
| N-2 | `/health` returns masked DSN and PostgreSQL version | P3 | Accepted local | Remove or gate behind the control token |
| N-3, N-4, N-5, P2-02, P2-04, P2-08, P2-12 | Deferred findings from the RC review | P2/P3 | Deferred | Each fixed in its own PR or explicitly accepted by the owner |
| P2-06/07/09/10, DI-1, DI-2, DI-4, DI-10 | Opt-in DI/OCR/worker paths | P2 | Accepted **only while disabled** | Owner decides each before an operator enables the extra, the worker or the raster path |
| DI-7 | Rerank ACTIVE without a real corpus | — | SHADOW | Real-corpus benchmark shows a gain |
| DEP-VITEST | vitest 3 → 4 needed for tinypool/vitest/mocker advisories (dev-only) | P3 | Accepted residual | Owner approves the major upgrade, or dismisses as tolerable |
| CQ-1/2/3/4/7 | CodeQL alerts triaged as false positives | — | Documented in the security register | Owner dismisses in GitHub |
| GAP-1 | Dependabot does not read `requirements.lock` | P3 | Accepted | Owner chooses how runtime pins are surfaced to Dependabot |
| R19-3 | 163 Windows full-suite failures (win32 fail-closed cases) | P2 | Baseline-tracked | Windows containment for the OCR worker, or tests marked platform-specific by the owner |
| OPS-PORTS | VM chain fixed ports inside the ephemeral range (one-off bind conflict) | P3 | Open | Reserve or move the ports |

## 7. Owner-only decisions and blockers

| ID | Decision |
| --- | --- |
| OWNER-REVIEW | Optional human review of the cumulative diff (recorded as not performed) |
| OWNER-GATE-1 | Whether a pull-request-only required check (`dependency review`) may be satisfied for a push commit by the PR run on an identical tree, or the registry marks it PR-only. The gate rule was **not** changed. |
| OWNER-WIN-JOB | Windows Job Object containment for the OCR and PDF workers and the model worker (memory/CPU kernel caps) |
| OWNER-NET | OS-level network isolation for workers (Windows Firewall/AppContainer or a Linux network namespace needs privileges) |
| OWNER-DI | DI-1 (in-process ONNX vs subprocess worker), DI-4 (scanned OCR), default-on proposals |
| OWNER-DEP | vitest 4 major upgrade; CodeQL dismissals; Dependabot coverage of the runtime lock |
| OWNER-VM | VM internet (Wi-Fi/VMnet8 subnet collision), Tesseract on the VM (needs sudo) |
| OWNER-PUBLIC | Public release: legal review of copyright ownership and the CLA |

## 8. Rollback

`git revert -m 1 <merge commit>` through a PR to `main` (never a force push). The RC stack has no database
migrations, so no downgrade is needed. Opt-in features are off by default. Rollback triggers: a lane FAIL on `main`
that does not reproduce at the tree-identical RC, or any regression in the governed approval or step-up path.

## 9. What did not change

No new scheduler, router, CAS, authority path or Core architecture. Company-proprietary assets were not copied.
