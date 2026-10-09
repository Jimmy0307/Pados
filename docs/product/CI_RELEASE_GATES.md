# CI release gates (public expectations)

**Workflows.** `.github/workflows/ci.yml`, `security.yml` and `release-candidate.yml` are hardened (the GitHub Pages
workflow is an inactive template under `deploy/github-pages/`) per
`docs/supply_chain/SECURE_CICD_V1.md` but have **not** been executed on GitHub yet. The equivalent commands run
locally as the productization gate matrix (`runtime/artifacts/reports/productization-v1/GATES.json`).

**Authority.** CI IS NOT PADOS AUTHORITY. CI SUCCESS != DEPLOYMENT AUTHORIZATION. BUILD SUCCESS != TRUST. A green
gate is evidence; it never writes PADOS state and never deploys. There is no deploy job in any workflow. Release
candidates are pulled and verified by the Home Base and deployed only after a human approval bound to the manifest
hash (`pados.release.deploy_verify`).

| Gate | Command | Workflow | Linux | Windows | macOS |
|---|---|---|---|---|---|
| CI authority boundary policy | `python scripts/release/ci_policy_check.py OUT.json` | ci, security, release-candidate | ✅ | — | — |
| Python unit/contract tests | `pytest tests --ignore=tests/integration` | ci | ✅ | ✅ (Windows-native suites, lifecycle/storage, learning, security, local intelligence with a fake runtime; 3 DB-backed tests deselected) | ✅ (no DB) |
| Supply-chain tests | `pytest tests/release_supply_chain` | ci, release-candidate | ✅ | ✅ | ✅ |
| DB integration | `pytest tests/integration` with a disposable pgvector service container | ci | ✅ | — | — |
| Ruff | `ruff check src tests` | ci | ✅ | — | ✅ |
| mypy strict | `mypy src`; `mypy --platform win32 …` | ci | ✅ | ✅ (win32 platform check) | ✅ |
| Frontend | `npm ci --ignore-scripts && npm run typecheck && npm test && npm run lint && npm run build` | ci | ✅ | — | — |
| Playwright (mocked) | `npx playwright test e2e/operator.spec.ts e2e/enterprise-mission-control.spec.ts e2e/compute-nodes-mission-control.spec.ts` | local | ✅ | — | — |
| CodeQL | `github/codeql-action` for `python` and `javascript-typescript` | security | ✅ | — | — |
| Dependency review | `actions/dependency-review-action` (`fail-on-severity: high`) on PRs | security | ✅ | — | — |
| Secret / credential scan | `python scripts/release/secret_scan.py OUT.json`; release scanner (`tests/unit/test_release_package.py`); GitHub secret scanning + push protection (repository settings) | ci, security, release-candidate | ✅ | ✅ | — |
| Public export hygiene | `python scripts/release/public_export_check.py` (private markers only where the untracked marker file exists) | ci | ✅ | ✅ | — |
| Public export build | `python scripts/release/build_public_export.py COMMIT OUT` (refuses without the marker file; re-scans the tree); `prepare_public_branch.py` (local branch only, never pushes) | local | ✅ | ✅ | — |
| Migration checks | `alembic heads`; offline SQL; up/down/up on a disposable DB; heads recorded offline in the release manifest | local, release-candidate | ✅ | — | — |
| Package build | `pip wheel . --no-deps` plus wheel content check; release job: `pip install -c requirements.lock -r scripts/release/build-requirements.lock` then `python -m build --no-isolation --sdist --wheel`, with the `pip freeze` inventory recorded | ci, release-candidate | ✅ | ✅ | ✅ |
| SBOM | `python scripts/release/sbom.py OUT_DIR` (CycloneDX 1.5 + SPDX 2.3, offline, deterministic) | release-candidate | ✅ | — | — |
| Release manifest + provenance | `python scripts/release/release_manifest.py RELEASE_DIR` (in-toto v1 / SLSA provenance v1, `UNSIGNED_LOCAL_PROVENANCE`) in the no-OIDC build job; `actions/attest-build-provenance` (signed) only in the separate download-hash-attest job | release-candidate | ✅ | — | — |
| Home Base pull verification | `python scripts/release/verify_release.py DIR --staging-root DIR --expect-manifest-sha256 HEX` (offline; read-only staged copy; `STAGED_VERIFIED` only with a manifest digest pin) | Home Base, never CI | — | — | — |
| Deployment readiness | signed single-use approval (`issue_deploy_approval.py`, HMAC key via SecretRef, registered approver, bound to manifest + staged digest + target, <= 24h) then `verify_release.py ... --approval ... --consume`; refused inside GitHub Actions | Home Base, never CI | — | — | — |
| Deterministic vertical slice | `bash scripts/demo/run_demo.sh` (docker) | local | ✅ | — | — |
| MLX | **None.** There is no Apple Silicon runner; static and contract coverage only. Live MLX is never faked | — | — | — | static |

Runner policy: `PUBLIC_PR_RUNNER_POLICY = HOSTED_EPHEMERAL_ONLY`; `HOME_BASE_SELF_HOSTED_RUNNER =
PROHIBITED_FOR_UNTRUSTED_WORKLOADS`. Supply-chain posture: `SLSA_L2_STYLE_TARGET` (no certification claimed; see the
mapping table in `docs/supply_chain/SECURE_CICD_V1.md`).
