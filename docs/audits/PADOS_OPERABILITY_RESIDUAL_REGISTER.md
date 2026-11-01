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

## Residuals carried from PR #19 (still OPEN; merging PR #19 did not resolve them)

| ID | Residual | Status | Why it stays open | Tracked with |
|---|---|---|---|---|
| R19-1 | **Cross-database revocation race.** The Product Loop re-checks its approval (operational database) immediately before the state-proof CAS commit (proof database). The two checks and the commit are separate transactions, so a withdrawal in that instant is not caught. | OPEN | No cross-database atomicity exists. Narrowing it further needs a fencing/lease design, not a retry. | OP-004 (lease/fencing); `docs/product/PUBLIC_API_BOUNDARY.md`, "Product Loop dispatch authority" |
| R19-2 | **Cloud egress classification covers only labels.** Cloud dispatch is gated on the project's recorded classification plus bundled enterprise evidence. Workspace file contents are not classified; the project label is the operator's statement about them. | OPEN | Content classification needs a policy decision and a classifier. Neither is authorized in this round. | Security backlog; `PUBLIC_API_BOUNDARY.md` |
| R19-3 | **Windows test residuals.** 126 full-suite failures and 25 `pack_focused` failures (28 test ids) are environment-dependent and identical on pristine `main`. Two timing-sensitive tests, unchanged since `main`, sometimes fail under load: `tests/local_intelligence/test_benchmark.py::test_total_deadline_is_total_not_per_read` (Windows) and `tests/local_intelligence/test_stage_c_llama_cpp_adapter.py::test_an_in_place_rewrite_that_restores_the_mtime_is_not_served_from_the_cache` (Linux). | OPEN | They are not fixed. Each run reports them by comparing failure sets, never as PASS. | OP-002: lane `windows-full-suite` (`known_status FAIL`, required for `production`); OP-006 |
| R19-4 | `pados init` in a home first initialized with the development-only `PADOS_CLI_DEV_*` overrides refuses with `RUNTIME_RECORD_MISSING`; `--adopt-runtime` is the consent. | OPEN (P3) | Fails closed, but not documented beside the development overrides. | Docs |
| R19-5 | `--adopt-runtime` combined with a version mismatch still refuses (`UPDATE_REQUIRED`); the behaviour is correct in code but not pinned by a test. | OPEN (P3) | No test pins it. | CLI tests |
| R19-6 | Live local model: measured execution was validated on Ollama 0.40.13. Model output never becomes a ResultCandidate and never reaches the CAS; that path does not exist by design. | OPEN (as a limitation) | MODEL OUTPUT != TRUTH. Report it as `NOT_VALIDATED` for governed acceptance, never as PASS. | OP-002: lanes `local-model-governed-acceptance` (`NOT_VALIDATED`, required for `production`) and `local-model-measured-execution` (`does_not_cover` lists ResultCandidate/CAS, REA, OpenMAIC, AI-Scientist-v3) in `scripts/release/runtime_lanes.v1.json` |
| R19-7 | Earlier accepted LOW findings: no POSIX owner-only bits for local secrets on Windows (profile ACLs instead); the reference probe has no FIFO guard on POSIX; a stop request issued during server startup can be dropped. | OPEN (LOW) | Accepted for local dogfood, not resolved. | Security backlog |
| R19-8 | GitHub reports Dependabot alerts on `main`. They predate PR #19 and were outside its scope. | OPEN | Not triaged in this round unless a dependency change is authorized. | Supply-chain backlog |

Rules: a residual leaves this table only with a commit that fixes it, a regression test that failed before the fix, and
validation evidence. Merging a PR closes nothing on its own.
