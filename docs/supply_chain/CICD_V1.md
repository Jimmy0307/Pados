# PADOS CI/CD V1: invariant gates, provenance and release promotion

CI IS NOT PADOS AUTHORITY. CI SUCCESS != DEPLOYMENT AUTHORIZATION. A green run proves that a tree passed the
gates below; it never publishes, deploys or authorizes anything. The authority boundary itself is enforced by
`docs/supply_chain/SECURE_CICD_V1.md` and `src/pados/release/workflow_policy.py`.

## Layers

| Layer | Where | When | What |
| --- | --- | --- | --- |
| 1 — PR CI | `ci.yml` jobs `linux`, `windows`, `macos-static` | every push and pull request | ruff, mypy, unit and contract tests (`-rs`), D1/D2/D4 regression gates, migration-head gate, secret scan, public-export hygiene, npm CLI tests, frontend |
| 2 — RC / merge CI | `ci.yml` job `postgres-integration`, then the reusable `packaging.yml` (called with `needs:`) | same workflow run | PostgreSQL integration (D1 approval binding, D4 loop handoff, residual amendment, proof lane, P6.11, migrations 0017/0018), fresh-database migration bootstrap, win32-x64 and linux-x64 runtime build, pack, install acceptance, provenance and SHA-256 manifests |
| 3 — Live acceptance | `scripts/acceptance/live_acceptance.py`, run by an operator | release candidates only, never per commit | D1 exploit replay, D2 one real local inference, D4 deterministic-then-model TaskLoop, against an installed PADOS on real hardware |

macOS remains static/contract only: the darwin-arm64 runtime package is design-ready but not built or accepted.

## Fail-closed gates

- **Regression gates** (`scripts/release/regression_gate.py`) run a named list of test files. A gate fails if
  pytest fails, a path is missing or collects nothing, fewer than the accepted minimum pass, or any test skips
  that is not explicitly listed with `--allow-skip`. Each gate writes a JSON report with the source commit and
  tree; the reports are uploaded as `gates-*` workflow artifacts.

  | Gate | Tests | Minimum |
  | --- | --- | --- |
  | D1 approval subject binding | `tests/unit/test_rc2_approval_subject.py`, `tests/contract/test_rc2_route_surface_contract.py` | 24 |
  | D2 measured execution binding | `tests/local_intelligence/test_rc2_execution_binding.py`, `tests/local_intelligence/test_measured_execution_contract.py`, `tests/unit/test_rc2_graph_measured_binding.py` | 50 (one allowed skip: the accepted M3 profile evidence is not in the public tree) |
  | D4 TaskLoop model handoff | `tests/unit/test_rc2_worker_model_executor.py`, `tests/unit/test_p6_10_loop_engine.py` | 17 |
  | PostgreSQL (`postgres_integration_gate.py`) | D1 binding 16, D4 handoff 6, residual 3, proof lane 17, P6.11 7, migration 5 | as listed, zero skips |

- **Migration head gate** (`scripts/release/migration_gate.py`): the runtime's declared lane revisions
  (`pados.config`: operational, corpus, proof) must equal the Alembic script heads, and `--fresh-install`
  bootstraps two new disposable lane databases through the installer's own upgrade path and requires their
  `alembic_version` to equal the declared revisions. CI pins no revision id anywhere.

## Provenance: the packaged tree is the tested tree

`packaging.yml` runs only as `needs: [linux, windows, postgres-integration]` of the same `ci.yml` run, so it
packages exactly the commit those jobs tested. After packing, `scripts/release/package_provenance.py record`
refuses to continue unless the checkout HEAD equals the run's commit and no tracked file differs from it, then
writes, per platform:

- `provenance-<platform>.json`: `SOURCE_COMMIT`, `SOURCE_TREE`, `BUILD_PLATFORM`, `BUILD_HOST`,
  `BUILD_TIMESTAMP` and `PACKAGE_SHA256` for every tarball;
- `SHA256SUMS-<platform>.txt` in `sha256sum` format.

Both platforms upload `npm-packages-<platform>` (tarballs, sidecars, provenance, manifest; 30 days).
`package_provenance.py verify --expected-commit C --expected-tree T` re-hashes a downloaded set before any
install.

## Dependency review: ENVIRONMENT_BLOCKER, not a red check

`actions/dependency-review-action` needs the repository's Dependency graph. When it is disabled the
`security.yml` job reports `DEPENDENCY_REVIEW=ENVIRONMENT_BLOCKER` as a warning annotation and in the job
summary on every pull request; any other dependency-review failure fails the job. The blocker is cleared only
by the repository owner enabling Dependency graph (Settings > Code security); it is never reported as PASS.

## Release promotion (manual)

```
CI PASS  ->  PACKAGING PASS (same run)  ->  RC artifact (npm-packages-*, provenance verified)
         ->  LIVE ACCEPTANCE PASS (Layer 3, on the RC artifact)  ->  manual release promotion
```

No workflow publishes to npm. `release-candidate.yml` (attested sdist/wheel/SBOM) stays restricted to `main`
and `v*` tags.

## Layer 3: live acceptance procedure

1. Download and verify the RC artifact:
   `python scripts/release/package_provenance.py verify --platform win32-x64 --dist DIR --expected-commit C --expected-tree T`.
2. Install it into a temporary npm prefix and a fresh `PADOS_HOME`; `pados init`, `pados doctor`.
3. With the installed runtime's interpreter, isolated:
   `python -I scripts/acceptance/live_acceptance.py prepare --home H --out OUT --profile-report R --expected-profile-id ID`
   (imports the ACCEPTED model capability profile and writes a project-scoped registration naming its model).
4. Export the printed environment (`PADOS_CHAT_PROVIDER`, `PADOS_CHAT_MODEL`, `PADOS_MODEL_CATALOG`,
   `PADOS_COMPUTE_NODES_CONFIG`) and the node credential variable, then `pados up` and `pados status`.
5. `python -I scripts/acceptance/live_acceptance.py run --home H --out OUT` (D1, D2, D4; bounded: one model
   inference per model gate, no benchmark loops).
6. `python -I scripts/acceptance/live_acceptance.py revoke --home H --out OUT`, then `pados down`.

The evidence files `OUT/live-acceptance-*.json` record the installed runtime identity and every API exchange.
