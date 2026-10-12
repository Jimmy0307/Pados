# PADOS CI/CD V1.1: PR CI, RC CI, artifact-bound live acceptance and manual promotion

CI IS NOT PADOS AUTHORITY. CI SUCCESS != DEPLOYMENT AUTHORIZATION. A green run proves only that a tree passed the
gates below. It never publishes, deploys or authorizes anything. The authority boundary itself is enforced by
`docs/supply_chain/SECURE_CICD_V1.md` and `src/pados/release/workflow_policy.py`.

## Layers

| Layer | Workflow | Triggers | Packages? | What runs |
| --- | --- | --- | --- | --- |
| 1 — PR CI | `ci.yml` (workflow name `pr-ci`) | every pull request; pushes to every branch except `rc/**`; `workflow_call` from RC CI | **never** | ruff, mypy, unit and contract tests (`-rs`), D1/D2/D4 regression gates (Linux and Windows), offline migration-head gate, secret scan, public-export hygiene, npm CLI tests, frontend, macOS static/contract |
| 2 — RC CI | `rc-ci.yml` (`rc-ci`) | `workflow_dispatch`; push to `rc/**` | **only here** | the whole PR CI (called on the same commit), PostgreSQL integration gate, fresh-install migration gate, then `packaging.yml` (win32-x64, linux-x64, install acceptance), then the RC manifest |
| 3 — Live acceptance | `scripts/acceptance/live_acceptance.py` (operator) | release candidates only | consumes the RC artifact | D1 exploit replay, D2 one real local inference, D4 deterministic-then-model TaskLoop, on an installed RC artifact |
| Promotion | `scripts/release/promotion_check.py`, then a human decision | manual | — | eligible only with a verified RC artifact and a PASS live acceptance of the same commit and tree |

- macOS stays static/contract only: the darwin-arm64 runtime package is design-ready but not built or accepted.
- `ci.yml` keeps its file name because `src/pados/release/workflow_policy.py` requires the files `ci.yml`,
  `security.yml` and `release-candidate.yml`. It is the PR CI workflow (`name: pr-ci`).

## Boundaries (enforced by `tests/release_supply_chain/test_cicd_v1_1_boundaries.py`)

- **PR CI never packages.** `ci.yml` calls no reusable workflow, and it contains no packaging, provenance,
  PostgreSQL integration or fresh-install step.
- **RC CI packages only after every gate.**
  - `rc-ci.yml` triggers only on `workflow_dispatch` and `rc/**` pushes, never on `pull_request`.
  - Its `packaging` job `needs` `checks`, `postgres-integration` and `fresh-install-migration`.
  - Its `rc-manifest` job `needs` `packaging`.
- **Packaging is RC-only, enforced twice.**
  - `packaging.yml` has only a `workflow_call` trigger, and `rc-ci.yml` is its only caller.
  - Every packaging job also carries
    `if: github.event_name == 'workflow_dispatch' || (push to refs/heads/rc/*)`. Even a mistaken call from a
    pull-request event therefore skips packaging.
- **Nothing publishes.**
  - No workflow holds write permissions outside the CodeQL and release-attestation allowlist.
  - The policy checker refuses `npm publish`, `twine upload`, `gh release`, `git push`, deploy commands and
    secrets in every workflow.
- **PR CI success is never a deployment authorization.**
  - No workflow uses `workflow_run`.
  - `release-candidate.yml` triggers only on manual dispatch and `v*` tags, and is guarded to `main`.
  - Deployment verification rejects CI-originated approval (`CI_CANNOT_AUTHORIZE_DEPLOYMENT`).

## Fail-closed gates

**Regression gates** (`scripts/release/regression_gate.py`). A gate fails when:
- pytest fails;
- a listed path is missing or collects nothing;
- fewer than the accepted minimum tests pass;
- any test skips that is not explicitly allowed.

Reports record the source commit and tree. Minimum passing counts:

| Gate | Minimum |
| --- | --- |
| D1 | 24 |
| D2 | 50 (one allowed skip: internal M3 evidence is not in the public tree) |
| D4 | 17 |
| PostgreSQL (RC CI) | D1 binding 16, D4 handoff 6, residual 3, proof lane 17, P6.11 7, migration 5; zero skips |

**Migration head gate** (`scripts/release/migration_gate.py`):
- PR CI: the declared lane revisions (`pados.config`) must equal the Alembic heads.
- RC CI: a fresh install through the installer's own upgrade path must migrate to exactly those revisions.
- No revision id is pinned anywhere in CI.

## RC artifact: the packaged tree is the tested tree

**Per platform**, packaging records two files:
- `provenance-<platform>.json`: `SOURCE_COMMIT`, `SOURCE_TREE`, `BUILD_PLATFORM`, `BUILD_HOST`, `BUILD_TIMESTAMP`
  and `PACKAGE_SHA256` for every tarball.
- `SHA256SUMS-<platform>.txt`: LF line endings on every platform, in GNU `sha256sum -c` format.

Recording refuses unless HEAD is the run's commit and no tracked file differs from it.

**The `rc-manifest` job** then runs `scripts/release/rc_manifest.py write`. It:
- refuses non-RC events;
- re-verifies both platforms against the run's commit and the checkout's tree;
- writes `rc-manifest.json` with `RC_COMMIT`, `RC_TREE`, the producing run, the gates and every package hash.

The whole set is uploaded as **`pados-rc-artifact`**.

## Live acceptance (Layer 3): RC artifact only

1. **Download and verify.** Download `pados-rc-artifact` from a successful RC CI run, then run
   `python scripts/release/rc_manifest.py verify --rc-dir RC --expected-commit C --expected-tree T`.
2. **Install.** Install `RC/win32-x64/*.tgz` into a temporary npm prefix with a fresh `PADOS_HOME`. Run
   `pados init` (fresh databases only), then `pados doctor`.
3. **Prepare.** Run `python -I live_acceptance.py prepare --home H --out OUT --profile-report R --expected-profile-id ID`.
   - It uses the accepted profile and writes a project-scoped registration.
   - It prints the environment for `pados up`. `PADOS_CHAT_MODEL` is the profile's runtime tag, for example
     `qwen3:8b`.
4. **Start.** Export that environment and the node credential variable, then run `pados up` and `pados status`.
5. **Run.** `python -I live_acceptance.py run --home H --out OUT --rc-manifest RC/rc-manifest.json`.
   - It refuses unless the installed runtime's commit equals `RC_COMMIT`, the runtime is not dirty, and its
     package is part of the RC.
   - It runs D1, D2 and D4, bounded to one inference per model gate.
   - D2 evidence is read from `work_item.candidate_outputs[].measured_execution`.
6. **Clean up.** `python -I live_acceptance.py revoke --home H --out OUT`, then `pados down`.

## Release promotion (manual)

```
PR CI PASS  ->  RC CI PASS (gates + packaging + RC manifest)  ->  LIVE ACCEPTANCE PASS (same RC commit/tree)
            ->  promotion_check.py: PROMOTION_ELIGIBLE  ->  human release decision
```

`promotion_check.py --rc-dir RC --live-summary OUT/live-acceptance-summary.json`:
- re-verifies the RC artifact;
- requires a PASS live acceptance (d1, d2, d4) recorded for the same `RC_COMMIT` and `RC_TREE`, on a clean
  runtime.

PR CI success is not an input. No step publishes to npm.

## Dependency review: ENVIRONMENT_BLOCKER

- `actions/dependency-review-action` needs the repository's Dependency graph, which is currently disabled.
- While it is disabled, the repository SBOM endpoint answers 404 and `security.yml` reports
  `DEPENDENCY_REVIEW=ENVIRONMENT_BLOCKER` as a warning annotation and in the job summary. It is never reported
  as PASS.
- Any other dependency-review failure fails the job.
- The owner clears the blocker by enabling Dependency graph.

`main` branch protection: see `MAIN_BRANCH_PROTECTION.md`.
