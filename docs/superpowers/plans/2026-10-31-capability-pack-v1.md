# Capability Pack V1 and Structured Problem Solving — implementation plan

Spec: `docs/superpowers/specs/2026-10-31-capability-pack-v1-design.md`.
Base: `main@e550141` (P7 Authority Baseline). Branch: `feat/capability-expansion-v1`.
Recovered work: unpublished `feat/capability-pack-v1` (`2c54760`, `d100efa`, `c5fba53`, `c28b370`, base `464b385`);
re-applied with changes, never merged or pushed as-is.

Rules for every step: RED test first and confirm it fails for the intended reason → minimal implementation → focused
PASS → broader regression → small commit. Nothing below adds a scheduler, router, CAS, task model, registry, executor,
receipt type, store or semantic authority. Core (`work_protocol`, `loop`, `state_proof`, `capability.registry`,
`capability.executor`) is not modified.

## Step 1 — Pack V1 contract (`src/pados/capability/pack.py`)

Interfaces: `CapabilityPackManifest` (+ `PackProvider`, `PackProvenance`, `PackRuntime`, `PackSecurity`,
`FailureCodeSpec`, `CheckerSpec`, `PackRemoval`), `validate_pack(data) -> CapabilityPackManifest`,
`PackManifestError(code)`, `GovernedPackAdapter(manifest, implementation)`,
`register_pack(registry, manifest, implementations, *, operator) -> PackRegistration`,
`disable_pack(registry, manifest, *, operator)`, `propose_pack_transition(...)`, `PackIndex.add/versions/attributions`,
`outputs_digest(refs)`. Reference pack: `src/pados/packs/reference/pack.py` (`REFERENCE_DIGEST`).

Tests: `tests/capability/test_pack_v1_contract.py` (validation matrix, content addressing, registration grants
nothing, execution only via `CapabilityExecutor`, evidence/failure boundary, lifecycle proposals, disable, version
index), `tests/capability/test_pack_boundaries.py` (AST: no authority, provider, work-protocol, learning,
fabric-internal or side-effect import from packs; no stage-binding call; no acceptance/CAS name; Core never imports
packs).

Commit: `feat(capability): add governed pack contract`.

## Step 2 — runtime enforcement and registration hygiene (same module)

RED tests in `tests/capability/test_pack_v1_governance.py`:
request above the pack ceiling → `PACK_CLASSIFICATION_CEILING_EXCEEDED`; wrong capability → `PACK_CAPABILITY_MISMATCH`;
raising availability probe → `available() is False` and plan INFEASIBLE; second registration →
`PACK_STALE_REGISTRATION` with zero registry events; redefined capability → `PACK_CAPABILITY_CONFLICT` with zero
events; new version with new adapter ids reuses the identical capability; disabled pack INFEASIBLE; rollback = disable
+ re-register previous version into a fresh registry, receipts attributable; implementation emitting authority →
`PACK_ADAPTER_FAILED`; malformed output with complete evidence → `VERIFICATION_FAILED`, no outputs.

Implementation: checks in `GovernedPackAdapter.execute/available`; `PackRegistry` protocol gains the read-only
`capability()` / `is_installed()` already on `CapabilityRegistry`; `register_pack` pre-checks.

Commit: `feat(capability): enforce pack manifests at runtime and reject stale registration`.

## Step 3 — Structured Problem Solving contracts and pipeline (`src/pados/packs/problem_solving/`)

Files: `contracts.py` (`EvidenceItem` with `source_id`, `EvidencePack` with duplicate refusal, `Claim`, `ClaimSet`,
profiles `EIGHT_D` and `A3`, `ProblemSolvingReport`, statuses incl. `ABSTAINED`), `pipeline.py` (`admit_evidence`,
`parse_claims`, `ground_claims`, `build_report`, `verify_report`, `result_projection`, advisory `CriticVerdict`).

Tests (synthetic fixtures in `tests/packs/problem_solving/_ps_fixtures.py`):
`test_ps_pipeline.py` (complete, missing → abstain, contradictory → conflicted, cross-case leakage → case mismatch,
unsupported generated claim, generation cannot rewrite evidence, critic advisory, malformed provider output, unknown
section) and `test_ps_evidence_admission.py` (stale digest, unauthorized source, no sources, duplicate evidence,
insufficient → ABSTAINED, A3 profile, every supported claim maps to cited same-case evidence over all fixtures,
projection never completes a goal short of COMPLETE).

Commit: `feat(problem-solving): add structured evidence contracts and grounding pipeline`.

## Step 4 — governed execution (`src/pados/packs/problem_solving/pack.py`)

`ProblemSolvingAdapter(provider, *, critic=None)` behind `GovernedPackAdapter`; task-owned parameters
`profile`, `evidence_pack_digest`, `authorized_sources` (comma-separated; flat scalars); SCHEMA
(`report_output_valid`) and REFERENCE_CHECK (`report_reference_check`, re-derivation) checkers.

Tests: `test_ps_pack.py` (E2E through the executor with re-derivation, provider unavailable retryable, malformed
provider/critic output, crashing provider leaks nothing, critic disagreement, tampered report refused) and
`test_ps_governed_execution.py` (stale digest / unauthorized source / malformed parameters at the adapter,
insufficient evidence abstains, disabled pack INFEASIBLE, execution succeeds but verification fails, verified report
→ `ResultCandidate` via existing `normalize_capability_result` → `build_coverage` → `compose_results`, staying
`PENDING_GOVERNED_ACCEPTANCE`).

Commit: `feat(problem-solving): add governed structured problem solving pack`.

## Step 5 — AI Scientist (draft PR #17)

Only after this PR merges: merge `main` into `feature/ai-scientist-v3-research-pack`, replace its runtime-profile
registry, lifecycle, adapter registration and verifier hook with Pack V1 concepts, implement `RESEARCH_CAMPAIGN` V0
test-first; `REAL_RUNTIME_NOT_VALIDATED` without a live runtime.

## Verification commands (Windows, from the worktree root, `PYTHONUTF8=1`)

```
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m mypy src
.venv\Scripts\python -m compileall -q src tests
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests/capability tests/packs tests/pipeline/test_pipeline_boundaries.py
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests/capability_loop tests/learning tests/security
.venv\Scripts\python scripts/release/public_export_check.py <out>/public-export.json
.venv\Scripts\python scripts/release/secret_scan.py <out>/secret-scan.json
```

plus the repository gates D1/D2/D4, migration gate and the full deterministic suite; Linux on the VMware VM at the
same commit and on GitHub CI.
