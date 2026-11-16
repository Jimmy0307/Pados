# Product Acceptance Report: integrated product stack (2026-11-16)

Scope: the stacked draft PRs #35 (identity sessions) → #36 (product work loop UI) → #37 (Work Map and Resources) →
#38 (provider setup and enterprise gateway adapter), integrated at `1af18ba` on `feat/runtime-provider-setup-20261116`,
plus this documentation PR (zh-TW operating guide, Windows launcher, this report). **Nothing is merged.**
**Production: NOT_READY.** Human code review of the stack: not performed.

## How to read this report

| Status | Meaning |
| --- | --- |
| **PASS** | The acceptance item is met and the evidence named in the row exists. The evidence class says how. |
| **PARTIAL** | Part of the item is evidenced; the row says what is missing. |
| **NOT_LIVE_VALIDATED** | Implemented and tested against stand-ins only; never run against the real external system. |
| **BLOCKED** | Cannot be validated now; the row names the blocker. |

| Evidence class | Meaning |
| --- | --- |
| **LIVE** | Real processes on a real host: `pados serve`, a private PostgreSQL 18 cluster, a real browser (Playwright). Data is synthetic. |
| **SYNTHETIC** | Unit or integration tests, Vitest, mocked Playwright, a fake OIDC issuer, a simulated gateway or the software WebAuthn authenticator. |
| **NOT_LIVE_VALIDATED** | No real external system (identity provider, enterprise gateway) was contacted. |

**Where the live runs were made.** The live end-to-end runs were made on each PR's branch head *before* the integration
merges (commits are named per row). The integrated head `1af18ba` is covered by the unit, integration and web suites
listed under F1 and F2, and by the launcher run in this PR. The full Windows and Linux numbers at the integrated head
are recorded in [FINAL VALIDATION](#final-validation-to-be-filled), which is still open.

**Evidence locations.** Committed evidence is in the PR bodies, the docs named below and `docs/product/screenshots/`.
Machine-local evidence is under the validation worktree's `runtime/artifacts/reports/` folder, written below as
`<evidence>/…`. It is not committed.

**Item names.** The identifiers follow the mandate's groups (identity A, product B, runtime C, Work Map D, resources E,
tests F). A2–A4 match the security-closure commits of PR #35. For the other rows, the one-line definitions were written
for this report from the PR bodies. The owner should check that they match the mandate's wording.

Diagram of the integrated stack: [product-convergence-2026-11-16.mmd](../architecture/public/product-convergence-2026-11-16.mmd).
Operator guide (zh-TW): [OPERATING_GUIDE_zh-TW.md](OPERATING_GUIDE_zh-TW.md).

## Summary

| Item | Status | Evidence class |
| --- | --- | --- |
| A1 Device pairing and server-side sessions | PASS | LIVE (Windows 11, VMware Linux) |
| A2 Least privilege for device sessions | PARTIAL | SYNTHETIC; live re-run owed |
| A3 Owner-only identity store (Windows DACL) | PASS | Windows 11 real ACLs in integration tests |
| A4 Single sign-on in the Control Center | NOT_LIVE_VALIDATED | SYNTHETIC |
| B1 Create a project from the browser | PASS | LIVE |
| B2 Markdown documents in the project | PASS | LIVE |
| B3 Request → plan → approval → TaskLoop → Evidence | PASS | LIVE |
| B4 ResultCandidate and the WebAuthn decision | PARTIAL | LIVE (waiting, no bypass); decision step not in this flow |
| C1 Enterprise gateway adapter | PASS | SYNTHETIC (ADAPTER_READY) |
| C2 Simulated gateway end to end | PASS | LIVE processes, simulated gateway (SIMULATED_E2E_PASS) |
| C3 Real enterprise gateway | NOT_LIVE_VALIDATED | none (ENTERPRISE_LIVE_NOT_VALIDATED) |
| C4 AI 模型與連線 (Providers) setup and two-level status | PASS | LIVE (simulated gateway, real Ollama diagnostics) |
| C5 Real local model under governance | PASS | LIVE (one Windows host, Ollama 0.40.16) |
| D Work Map | PASS | LIVE |
| E Resources and storage | PASS | LIVE |
| F1 Python unit and integration suites | PASS | SYNTHETIC (3 known baseline failures in `tests/security`) |
| F2 Web suites | PASS | SYNTHETIC |
| F3 Live E2E at the integrated head | PARTIAL | branch heads LIVE; integrated head owed |
| F4 Linux (VMware) validation | PARTIAL | identity chain only |
| F5 Windows full validation and launcher | PARTIAL | launcher PASS; full validation at the integrated head owed |

## A. Identity and sessions (PR #35)

| Item | Status | Evidence |
| --- | --- | --- |
| **A1** One-time device pairing (`pados device pair`, **Pair this browser**); HttpOnly server-side session; reload restores it; Work Requests and the task loop through the session only; the raw control token never reaches the browser; logout, revoke, idle expiry, token rotation | **PASS** (LIVE) | Windows 11 identity E2E at `eaeb9f4`: **26 PASS, 0 FAIL**, 1 NOT_LIVE_VALIDATED (`oidc_live_sign_in`), including `pairing_code_powershell51`, `work_request_via_session_only`, `step_up_cannot_be_bypassed_by_session`, `idle_expiry` (real 130 s), `control_token_rotation_ends_sessions` and `no_control_token_or_session_secret_leak` (`<evidence>/pados-identity-20261116/windows-identity-e2e-eaeb9f4/IDENTITY_E2E_RESULT.json`). VMware Ubuntu 24.04 at `298d32f`: **23 PASS**, 2 SKIPPED (PowerShell 5.1 is Windows only; the VM's browser build is older than the lock), 1 NOT_LIVE_VALIDATED. Live Playwright: 1 passed. Launcher run at `1af18ba` (this PR): code minted with the printed command, paired through the web origin, `GET /identity/v1/session` authenticated. |
| **A2** A session may call only the allowlisted `(method, route template)` pairs; everything else is `SESSION_SCOPE_DENIED`; the token keeps full authority; result acceptance stays refused | **PARTIAL** | SYNTHETIC: `test_session_scopes.py` (61 cases; every `control_auth` route classified). At `1af18ba` the scope, OP-004, gateway and provider tests pass (130). The provider `PUT` is token-only. Missing: no live E2E has run with the allowlist in place, because every live run above predates `b5aa3e3`. Owed in FINAL VALIDATION. |
| **A3** The identity store is owner-only from creation (Windows: protected DACL from the process token SID; POSIX 0700/0600); every load fails closed with `IDENTITY_STORE_INSECURE`; sessions off loopback need the `__Host-` Secure cookie | **PASS** (Windows 11) | `test_store_protection.py` and `test_transport_and_cross_port.py` with real ACLs (`icacls`, SIDs) on Windows 11: `tests/identity` **316 passed**, 7 skipped (POSIX-only and symlink privilege). Linux: the POSIX branches are covered by tests that skip on win32 and by `mypy --platform linux`; not re-run on the VM. This is not a Windows user authentication. |
| **A4** 單一登入 (SSO) section: provider links, zh-TW return messages, actor view, actor-only logout | **NOT_LIVE_VALIDATED** | SYNTHETIC: `tests/identity/oidc` **155 passed**; Vitest `sso.test.ts` 27; mocked Playwright `control-center-identity.spec.ts`. **Microsoft Entra ID and Google with real tenants: NOT_LIVE_VALIDATED.** `approver` grants nothing yet. |

Not implemented and out of scope: **Windows Hello as an identity factor and trusted Windows SID binding:
NOT_IMPLEMENTED** (identity design §5). Whether sessions should be on by default for new installs is an owner
decision. The default stays `PADOS_IDENTITY_SESSIONS_ENABLED=false`.

## B. Product work loop (PR #36)

Live run: `scripts/dogfood/product_flow_e2e.py --playwright` at `aafb2a8`, Windows 11, real PostgreSQL 18 and
`pados serve`: **PASS, 15/15**, plus a live Playwright demo with 7 screenshots
(`<evidence>/pados-product-20261116/product_flow_e2e/PRODUCT_FLOW_E2E_RESULT.json`). All writes went through the paired
session with CSRF, browser storage was empty, and the token did not leak.

| Item | Status | Evidence |
| --- | --- | --- |
| **B1** Create a project with a name only (`POST /control/projects`); the folder is placed under `PADOS_PRODUCT_WORKSPACE_ROOT`; idempotent; closed error codes | **PASS** (LIVE) | Step `project_created_with_name_only`; screenshot `docs/product/screenshots/01-project-created.png` |
| **B2** Markdown documents: `.md` only, at most 256 KiB, inside the project root, atomic and audited writes, no silent overwrite (SHA-256 precondition), preview | **PASS** (LIVE) | Step `markdown_added_inside_the_project`. Known risk (PR #36): if the database commit fails after the file write, the file exists without its audit event. |
| **B3** 工作需求 → readable plan labelled 確定性範本，未使用 AI → 核准／修改／拒絕 on the WORK_ROUTE approval → TaskLoop with Pause/Resume/Cancel → Evidence and receipts | **PASS** (LIVE) | Steps `plan_approved_and_run`, `evidence_names_the_broken_link` and `fixed_document_check_completes`; screenshots `02-plan-review.png` and `03-evidence-broken-link.png` |
| **B4** 提交候選結果: the candidate waits for the Remote WebAuthn step-up and is never shown as accepted unless the backend recorded the decision; a session cannot decide it | **PARTIAL** | LIVE: steps `unfinished_check_cannot_become_a_candidate`, `result_candidate_waits_for_approval` and `step_up_cannot_be_bypassed_by_session` (the approval stays PENDING); screenshot `04-candidate-waiting-for-step-up.png`. Missing from this flow: the decision itself. The step-up decision through the Remote console and the single CAS is evidenced separately by `local_dogfood_e2e.py`, with the software authenticator (SYNTHETIC device, not a phone or hardware key). The "no presence device enrolled" state is unit-tested only. The demo order (fix, then recheck, then submit) follows the governance rule that a candidate needs a COMPLETED loop; changing it is an owner decision. |

## C. AI runtime and providers (PR #38)

| Item | Status | Evidence |
| --- | --- | --- |
| **C1** `enterprise_gateway` backend: private declarative contract; https only (http on loopback only); key by reference (`secretref://env/NAME`); closed `GATEWAY_*` codes; retries only for reads or for chat with an honoured idempotency key; circuit breaker; a content-free receipt per attempt; output is `NON_AUTHORITATIVE_OBSERVATION` | **PASS** (SYNTHETIC, **ADAPTER_READY**) | `tests/compute_node/test_gateway_contract.py`, `tests/compute_node/test_gateway_adapter.py` against the simulated gateway over loopback HTTP and HTTPS; `tests/compute_node` + `tests/local_intelligence` **371 passed** at the branch head |
| **C2** End to end with real processes and a simulated gateway | **PASS** (**SIMULATED_E2E_PASS**) | `scripts/dogfood/gateway_simulated_e2e.py` run r3: **15/15** (discovery, auth failure, rate limit, a timeout that is never re-sent, a dropped connection without a duplicate side effect, an honoured idempotency retry, classification refusal, circuit open and recovery, 14 receipts, `pados serve` routes, live Playwright Providers page): `<evidence>/pados-runtime-20261116/gateway-simulated-e2e-r3/GATEWAY_SIMULATED_E2E_RESULT.json`. Runs r1 and r2 failed (the spec opened the wrong page; a disabled provider still showed a cached state, fixed) and are kept. |
| **C3** A real enterprise gateway | **NOT_LIVE_VALIDATED** (**ENTERPRISE_LIVE_NOT_VALIDATED**) | No enterprise gateway was contacted. The owner procedure, with a placeholder-only contract template, is [ENTERPRISE_GATEWAY_LIVE_ACCEPTANCE.md](../local-compute/ENTERPRISE_GATEWAY_LIVE_ACCEPTANCE.md). It is blocked on VPN access, which is the owner's to provide. |
| **C4** AI 模型與連線 (Providers): two-level status (reachability, then per-model governance); "可連線 ≠ 可用於受治理的工作"; owner setup file holds env-var names only and can only narrow; sessions may read, diagnose, enable and disable; `PUT` is token-only | **PASS** (LIVE) | Live Playwright step `playwright_live_providers_page` in C2; route tests at `1af18ba` (130 passed, with the session classification). Screenshots stay local because they list the host's installed models (`<evidence>/pados-runtime-20261116/gateway-simulated-e2e-r3/screenshots/`). |
| **C5** A real local model runs governed work through the measured path | **PASS** (LIVE, one Windows 11 host) | Ollama 0.40.16. The earlier profiles were for older Ollama versions and would be refused (`RUNTIME_VERSION_NOT_MEASURED`), so `qwen3:8b` was re-measured: M2 identity PASS, then the M3 profile, registered but not authorized. The existing M4.1 governed run then gave **REAL_TASK_DOGFOOD=PASS**: context 8192, temperature 0, seed 42, JSON schema output; a temporary explicit registration, removed afterwards; receipt SUCCEEDED; the validator checked 5 of 5 fields; `semantic_authority` NONE, never submitted to the CAS. Archives: `<evidence>/pados-runtime-20261116/m2-ollama-0.40.zip` (SHA-256 `e3772c57…a738`) and `m4_1-governed-run-0.40.zip` (SHA-256 `516141e7…de39`). The integrity probe was skipped to limit memory use on the host. |

Known limits (PR #38): no streaming; system proxy settings are ignored; a gateway model cannot be measured yet.

## D. Work Map (PR #37)

| Item | Status | Evidence |
| --- | --- | --- |
| **D** 工作心智圖: Project → Goal → Task → Subtask → Execution → Evidence → Decision as a mind map (SVG) or list (ARIA tree); search; a detail panel with status, blocker, next step, dependencies and history; "從這裡提出工作需求" through the existing routes; read-only projection (`GET /control/projects/{id}/work-map`); accepted only when an APPROVE decision is recorded | **PASS** (LIVE) | `scripts/dogfood/work_map_resources_e2e.py --playwright` at `3e0b25a`: **PASS, 18/18** (`<evidence>/pados-mindmap-20261116/work_map_resources_e2e/WORK_MAP_RESOURCES_E2E_RESULT.json`). The demo task appears with its evidence SHA-256, and the candidate is WAITING_STEP_UP while the backend says PENDING. No writes and no browser storage. Screenshot `docs/product/screenshots/05-work-map-mindmap.png`. Design: [WORK_MAP_AND_RESOURCES_V1.md](../architecture/WORK_MAP_AND_RESOURCES_V1.md). |

## E. Resources and storage (PR #37)

| Item | Status | Evidence |
| --- | --- | --- |
| **E** 資源與儲存 (`GET /control/resources`, read-only): every metric has a source, a unit and is MEASURED with a time or UNKNOWN with a reason; budget defaults (warning at 15 % free or 85 % used, critical at 5 % or 95 %); cleanup **preview only** (evidence, decisions, audit records and unique copies are never candidates; no execute action; 需要 Owner 授權) | **PASS** (LIVE) | Steps `resources_summary_measures_or_says_unknown` and `cleanup_plan_is_a_preview_that_protects_evidence` in the D run. Screenshots stay local because they show the host's memory and disk sizes (`<evidence>/pados-mindmap-20261116/screenshots/06-resources.png`). Not done: storage-size history (`HISTORY_NOT_RECORDED`), per-project budgets, compute-node model memory, cleanup execution. |

## F. Tests and validation

| Item | Status | Evidence |
| --- | --- | --- |
| **F1** Python unit and integration suites | **PASS** (SYNTHETIC) | At `1af18ba`: scope, OP-004, gateway and provider tests **130 passed**; ruff clean; mypy 468 files clean. Earlier on the stack: `tests/identity` 316 passed / 7 skipped; `tests/identity/oidc` 155 passed; `tests/identity` + `tests/control` 395 passed / 7 skipped (at `3fcbe21`); `tests/security` + OP-004 **447 passed**, 13 skipped, **3 failed** (the known `test_p6_6_adversarial.py` cases that need a password-protected PostgreSQL on 5432; identical on `main`). |
| **F2** Web suites | **PASS** (SYNTHETIC) | At `1af18ba`: Vitest **233 passed**; typecheck, lint and build pass. Mocked Playwright identity spec: 6/6 after `--repeat-each=3` (one `page.goto` timeout under host memory pressure on the full run). |
| **F3** Live E2E at the integrated head | **PARTIAL** | LIVE on the branch heads: identity (A1), product flow (B), Work Map and Resources (D, E), simulated gateway (C2), governed local model (C5). At `1af18ba` only the launcher run below has been made. The identity, product, Work Map and gateway E2Es at the integrated head are owed in FINAL VALIDATION. Known flake (PR #36): on a cold start the Vite proxy dropped the `/pair` reply in 2 of 5 runs; the live spec re-pairs up to 3 times; root cause unknown. |
| **F4** Linux (VMware Ubuntu 24.04, offline) | **PARTIAL** | At `298d32f` (identity only): `vm_online_chain.sh` **19/19 gates**, local dogfood E2E PASS, full pytest 0 failures, identity E2E 23 PASS. PRs #36 to #38 have not run on Linux. |
| **F5** Windows full validation (`scripts/validation/windows_validate.py --profile full`, Pester and Playwright included), and the launcher | **PARTIAL** | Launcher (this PR): Pester `tests/launcher/Start-PADOS.Windows.Tests.ps1` **22 passed, 0 failed** under Windows PowerShell 5.1. It covers dry-run planning, the missing venv, node_modules and `.env` messages, an unreachable PostgreSQL, port-in-use detection, `-Stop` with a fake PID file (and a reused PID never stopped), `-Status` and the refusal to start twice. Live launcher run at `1af18ba`, on its own ports (API 18621, web 18622, PostgreSQL 18620), with a temporary runtime and a private cluster: start in 6 s, `/health/ready` OK, `operator.html` 200, pairing code and web-origin pairing, second start refused (`ALREADY_RUNNING`), `-Stop` with both ports released and no leftover process, and `DEFAULT_CONTROL_TOKEN_REFUSED` reported as `BACKEND_START_REFUSED` with cleanup. Full validation was last run at `eaeb9f4` (identity head): every step through Pester passed except `full_pytest` (163 baseline failures, plus 4 environment-only document-intelligence cases, plus the OP-004 gap fixed in `298d32f`); it was stopped for low host memory before its Playwright steps, which then passed separately. **A complete run at the integrated head is owed.** |

Release checks on this documentation PR (public export check, secret scan, doc lint): see the PR body.

## Screenshots: committed and local only

Committed (checked: synthetic project names and device ids only, no host paths, no host sizes, no model inventory, no
PNG text chunks): `docs/product/screenshots/01-project-created.png`, `02-plan-review.png`,
`03-evidence-broken-link.png`, `04-candidate-waiting-for-step-up.png` and `05-work-map-mindmap.png`.

Kept local, for the reason given:

- Providers pages (`<evidence>/pados-runtime-20261116/gateway-simulated-e2e-r3/screenshots/`): they list locally installed models.
- Resources pages (`<evidence>/pados-mindmap-20261116/screenshots/06…08`): they show host memory and disk sizes.
- The other product and Work Map views: kept as evidence only.

## Owner decisions open

1. Whether `PADOS_IDENTITY_SESSIONS_ENABLED` defaults to true for new installs.
2. Whether the session allowlist (identity design §3.8) matches the Control Center the owner wants.
3. HTTPS beyond single-user hosts.
4. The live enterprise gateway acceptance, once VPN access exists (C3).
5. Whether the demo order for candidates (fix, recheck, submit) stays as it is.
6. Merge order and review of #35 to #38 and of this PR. **Do not merge without owner review.**

## FINAL VALIDATION (to be filled)

<!-- Filled from the final validation run at the integrated head. Do not edit the rows above to match; add results here. -->

| Run | Head | Platform | Result | Evidence |
| --- | --- | --- | --- | --- |
| `windows_validate.py --profile full` (incl. Pester `tests/launcher`, Playwright) | _to be filled_ | Windows 11 | _to be filled_ | _to be filled_ |
| Identity E2E (`scripts/dogfood/identity_e2e.py`) | _to be filled_ | Windows 11 | _to be filled_ | _to be filled_ |
| Product flow E2E (`scripts/dogfood/product_flow_e2e.py --playwright`) | _to be filled_ | Windows 11 | _to be filled_ | _to be filled_ |
| Work Map and Resources E2E (`scripts/dogfood/work_map_resources_e2e.py --playwright`) | _to be filled_ | Windows 11 | _to be filled_ | _to be filled_ |
| Simulated gateway E2E (`scripts/dogfood/gateway_simulated_e2e.py --playwright`) | _to be filled_ | Windows 11 | _to be filled_ | _to be filled_ |
| VMware Linux chain and E2Es | _to be filled_ | Ubuntu 24.04 | _to be filled_ | _to be filled_ |

Rows A2, B4, F3, F4 and F5 change status only from what is recorded in this table.
