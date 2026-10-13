# PADOS_RC2_LOCAL_DOGFOOD_V1: freeze manifest

This document versions the release evidence for the RC2 local-dogfood freeze. It describes a frozen state; it does
not change it. The tag below is immutable and must never be moved, re-created or rewritten.

## Identity

| Item | Value |
|---|---|
| Repository | `Jimmy0307/Pados` |
| Tag | `PADOS_RC2_LOCAL_DOGFOOD_V1` (annotated; tag object `30127a09ba3e43bb19bb8245b51b44397f66a2ca`) |
| Tagged commit (`main` merge commit of PR #2) | `9e376e6e69e48c48b10eed0c0421e0f47a3dd058` |
| Tree | `a82f482919c5ac5fd66c8ebd7777205964371428` |
| Merge parents | `ec618ce76b1810d0b7b4e7c23d973ad7913593ef` (previous `main`), `dd7283112a6476079162eec40b47b0ef3d079208` (tested head) |
| Tested commit / tree | `dd7283112a6476079162eec40b47b0ef3d079208` / `a82f482919c5ac5fd66c8ebd7777205964371428` |
| Tested tree equals released tree | YES |

The RC2 product baseline is `229e6b77b04ee3639d727dce0988b77cea12af15` (D1, D2 and D4 fixes). Between that commit and
the freeze, product source changed only in the dependency security re-spin (see `SECURITY_RESPIN.md`); every other
change is CI/CD tooling, tests and documentation.

## CI runs (GitHub Actions)

| Run | Workflow | Commit | Result |
|---|---|---|---|
| 38447098226 | `pr-ci` (pull request) | `dd72831` | success |
| 38447088490 | `pr-ci` (push) | `dd72831` | success |
| 38447098319 | `security` (pull request: CodeQL, secret scan and CI policy, dependency review) | `dd72831` | success |
| 38447092231 | `rc-ci` (PR checks, PostgreSQL integration, fresh-install migration gate, packaging, RC manifest) | `dd72831` | success |
| 38450843131 | `pr-ci` (push to `main`) | `9e376e6` | success |
| 38450843576 | `security` (push to `main`; dependency review runs on pull requests only) | `9e376e6` | success |

The CodeQL *results* check reports 7 known alerts and is not a required check (see `KNOWN_ISSUES.md`).

## RC artifact

`pados-rc-artifact` from RC CI run 38447092231. Its RC manifest binds every package to commit `dd72831` and tree
`a82f4829`.

| File | SHA-256 |
|---|---|
| `pados-rc-artifact` (zip, as downloaded) | `1c24252c1048bca994d887618eafa7251abf7e52b270fd97b2ba46d4c2d9c84d` |
| `rc-manifest.json` | `413a9261b1a0bbae09f26bf867e73c0101e897932beacba5027286c9884074d2` |
| `win32-x64/pados-ai-cli-0.1.0-alpha.1.tgz` | `bd5195acb09ce2b7b7ce46bd8a94ce87a42f8a673ba4c139580575f9233001c9` |
| `win32-x64/pados-ai-runtime-win32-x64-0.1.0-alpha.1.tgz` | `bc469ba0a0023497c5d0be9b3aea2f1582077d5cef914d1226c3a3babe60e6dc` |
| `linux-x64/pados-ai-cli-0.1.0-alpha.1.tgz` | `f3123632bc1c599d3d3a9257a82ac32ab57c5b10021c0f358292bae51974caaa` |
| `linux-x64/pados-ai-runtime-linux-x64-0.1.0-alpha.1.tgz` | `609d824a058ffa9d2c6318d7cd8401f812008b3a57a6dcc25990d2aaaef6d8fa` |

The linux-x64 set also contains synthetic `0.1.0-alpha.99` tarballs built only for the upgrade test. They are hashed
and bound like the rest, but they are test inputs, not release packages.

## Gate results

| Gate | Result |
|---|---|
| PR CI (Linux, Windows, macOS static/contract) | PASS |
| PostgreSQL integration (D1 binding, D4 handoff, residual, proof lane, P6.11, migrations; zero skips) | PASS |
| Migration heads (declared = Alembic = fresh install: 0019 / 0011) | PASS |
| Dependency review | PASS (`SECURITY_RESPIN.md`) |
| npm audit release impact | NO BLOCKER (`NPM_AUDIT_RELEASE_IMPACT_TRIAGE.md`) |
| Fresh install of the RC artifact (`npm install`, `pados init`, `doctor`, `up`, `status`) | PASS; `doctor` reports WARNING for known non-blocking items; `/ready` is 503 because no proof database is configured (expected) |
| D1 live: out-of-scope and unbound approvals refused, no job created, refusals audited | PASS |
| D2 live: one local inference, measured binding applied, context window 8192 equal to the loaded runtime, reasoning disabled | PASS |
| D4 live: deterministic step then model step, loop COMPLETED with exactly 2 jobs and no pause | PASS |
| PDF regression on the installed artifact (OCR-01 budget enforced) | PASS |
| Unauthorized execution / unintended cloud route | 0 / 0 |

## Publication status

- npm published: NO.
- Public release: NO.
- Scope: local, supervised, non-sensitive dogfood only (`DOGFOOD_BOUNDARY.md`).

## Related documents

- `SECURITY_RESPIN.md`: anyio and pypdf remediation, and the OCR-01 Configuration migration.
- `NPM_AUDIT_RELEASE_IMPACT_TRIAGE.md`: the frontend audit findings and why they do not ship.
- `KNOWN_ISSUES.md`: open P1 defects, open review threads and documentation drift.
- `DOGFOOD_BOUNDARY.md`: what this freeze may and may not be used for.
