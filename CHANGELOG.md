# CHANGELOG

## Unreleased

### Windows launcher, zh-TW operating guide and product acceptance report (docs PR on the #35–#38 stack)

- `scripts/windows/Start-PADOS.ps1` (and `Start-PADOS.cmd`), Windows PowerShell 5.1:
  - finds the checkout and checks the venv, npm, `web\node_modules`, `.env`, PostgreSQL reachability and free ports,
    printing the exact one-line fix for each problem;
  - starts `pados serve` and the Vite dev server, with logs in `runtime\logs\` and PIDs in
    `runtime\local\pados-launcher.json`, waits for `/health/ready`, opens `/operator.html` and prints the pairing
    command;
  - `-Status`, `-Stop` (stops only processes it started, checking PID and start time) and `-DryRun`;
  - never runs `pados init` and never edits `.env` or a database;
  - Pester tests in `tests/launcher/Start-PADOS.Windows.Tests.ps1`: 22 passed.
- `docs/product/OPERATING_GUIDE_zh-TW.md`: from first start to rollback, with real UI labels, a plain-language entry for
  every common error code, and SYNTHETIC / LIVE / NOT_LIVE_VALIDATED marked per feature.
- `docs/product/PRODUCT_ACCEPTANCE_REPORT_2026-11-16.md`: acceptance items A1–A4, B1–B4, C1–C5, D, E and F1–F5 with
  status and evidence; the final validation section is still open.
- Architecture diagram `docs/architecture/public/product-convergence-2026-11-16.mmd`. Security register additions for
  identity, providers, the gateway and the launcher.

### AI providers and enterprise gateway adapter (draft PR #38)

- AI 模型與連線 (Providers) page and `/control/providers`: reachability and governed use (measured and authorized) are
  shown separately. The owner setup file holds env-var names only and can only narrow. Sessions may read, diagnose,
  enable and disable; `PUT` is token-only.
- `enterprise_gateway` compute-node backend, with these statuses:
  - ADAPTER_READY;
  - SIMULATED_E2E_PASS (15/15);
  - **ENTERPRISE_LIVE_NOT_VALIDATED**.
- A real governed local model run passed: `qwen3:8b` on Ollama 0.40.16, re-measured, with a temporary explicit
  authorization.

### Product work loop in the Control Center (draft PR #36)

- In zh-TW: create a project with a name only; Markdown documents; request → deterministic plan → approval → TaskLoop →
  Evidence → ResultCandidate waiting for the WebAuthn step-up.
- New routes: `POST /control/projects`, the project documents routes, `GET /control/projects/{id}/work` and the bounded
  evidence content. No migration.

### Work Map and Resources (Control Center, read-only)

- 工作心智圖 (Work Map): Project → Goal → Task → Subtask → Execution → Evidence → Decision as a mind map or list, from
  `GET /control/projects/{id}/work-map` (PROJECTION ONLY). New work still goes through the work-request and approval
  routes.
- 資源與儲存 (Resources): `GET /control/resources` measures RAM, model memory (configured loopback runtime only),
  PostgreSQL, documents, cache, evidence, execution growth, tokens and execution time, each with source, unit, time and
  MEASURED/UNKNOWN; storage budget (optional `PADOS_STORAGE_POLICY`) and a cleanup plan preview that never selects
  evidence, decisions, audit records or unique copies and never executes. See
  `docs/architecture/WORK_MAP_AND_RESOURCES_V1.md`.

### Unified Identity & Session v1 (feature flag, off by default)

- One-time device pairing (`pados device pair`) and server-side HttpOnly sessions for the Local Control Center, so a
  paired browser no longer needs the control token on reload. Since the security closure (A2) the session has less than
  the control-token authority: an allowlist of Control Center routes, with everything else `SESSION_SCOPE_DENIED`. It
  never decides result acceptance (WebAuthn step-up unchanged). The store is owner-only from creation (A3), and there is
  an SSO section in the UI (A4, validated synthetic only).
- OIDC sign-in adapter (Microsoft Entra ID, Google, generic; Authorization Code + PKCE, state, nonce, RS256) with
  PADOS actor and project-role bindings. **NOT_LIVE_VALIDATED** against real tenants.
- Flags, rollback, threat model and tests: `docs/identity/` (`RELEASE_NOTES_IDENTITY_V1.md`, `THREAT_MODEL.md`,
  `SECURITY_TESTS.md`).

### 2026-11-14 release baseline (local-supervised dogfood; production NOT_READY)

- PR #26: the PR #20 to #25 stack lands on `main` as merge commit `1fd5608` (tree identical to the validated RC `202df5c`). Owner-authorized; human code review of the cumulative diff not performed.
- PR #27: result-acceptance step-up guard and other existence probes resolve on the `search_path` (N-1).
- PR #28: vite 7.3.7, source-map-js 1.2.2 (dev toolchain).
- PR #29: `pados init` writes `.env` and the control token owner-only from creation (atomic replace).
- PR #30: certifi 2026.10.23 (critical emSign advisory), Mako 1.4.3.
- PR #31: security register for CodeQL and Dependabot alerts.
- PR #32: pypdf 6.21.4; untrusted PDFs parsed in a bounded isolated worker (P2-11).
- PR #33: OS-enforced isolation for the opt-in model worker (Windows Job Object; Linux RLIMIT_AS and seccomp). Still disabled by default.
- Release baseline, residual register and owner decisions: `docs/releases/PADOS_LOCAL_DOGFOOD_BASELINE_2026-11-14.md`.
- Final engineering report: `docs/releases/PADOS_FINAL_ENGINEERING_REPORT_2026-11.md`.

- Document Intelligence packs (PR #24, stacked on #22). Pinned local embedding (bge-small-en-v1.5) and
  cross-encoder (ms-marco-MiniLM-L-6-v2) engines via the optional `document-intelligence` extra. Governed
  `PASSAGE_RETRIEVAL_DENSE`, `PASSAGE_RANK_FUSION`, `PASSAGE_RERANK_CROSS_ENCODER` and `PASSAGE_EVIDENCE_PACK`
  stages, with templates from PDF or `ocr.document/v1` to evidence, and from evidence to a grounded
  ResultCandidate. Added an operator model-fetch script, a synthetic benchmark (`BENCHMARK_2026-11-14.md`) and
  residuals DI-1 to DI-10.

- Imported governing specification.
- Added P0 application skeleton, governance docs, Docker Compose, scripts, migrations, tests, and validation evidence.

