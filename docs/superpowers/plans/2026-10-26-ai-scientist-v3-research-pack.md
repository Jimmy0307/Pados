# AI-Scientist-v3 Research Capability Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a governed PADOS Research Capability Pack that can run a pinned AI-Scientist-v3 campaign, verify its evidence envelope, and return a PADOS ResultCandidate without granting the upstream runtime PADOS authority.

**Architecture:** V0 exposes one `RESEARCH_CAMPAIGN` capability backed by an isolated AI-Scientist-v3 runtime profile. The adapter stages bounded inputs, executes only a registered profile, builds a `ResearchRunManifest`, and returns artifact references for normal Capability Fabric verification/receipts. V1 later decomposes the upstream resumable phases into individual PADOS stages.

**Tech Stack:** Python 3, existing PADOS Capability Fabric, subprocess/container boundary, SHA-256 artifact manifests, pytest, upstream `agsci` CLI.

**Spec:** `docs/superpowers/specs/2026-10-26-ai-scientist-v3-research-pack-design.md`

## Global Constraints

- PADOS remains the only task/policy/resource/verification/receipt/CAS authority.
- Do not vendor or fork AI-Scientist-v3 into PADOS Core.
- No raw shell command, host path, URL, Docker flag, provider key, or environment-variable value is accepted from a task contract.
- Upstream coding agents must not run in the PADOS service process.
- Runtime profile identity, upstream revision, and image digest must be operator-controlled and pinned.
- Runtime verification proves evidence integrity, not scientific truth.
- Unknown upstream artifacts are not promoted automatically.
- No fallback may weaken classification, network, model, or resource policy.

## Review Focus

1. Task parameters that try to inject a command/path/env value must be rejected before dispatch.
2. Manifest paths containing `..`, absolute paths, symlink escapes, or files outside the owned run root must be rejected and outputs withheld.
3. A runtime profile that changes revision/image/agent allowlist after planning must fail the existing revalidation path rather than execute stale authority.
4. A successful upstream process with missing or hash-mismatched required evidence must fail verification.
5. Provider secrets or `.env` values must never appear in promoted manifests, receipts, event summaries, or failure messages.

---

### Pack V1 alignment (2026-10-31)

Tasks 1-6 of the original plan were written before Capability Pack V1 (PR #18) existed. They are **superseded** by
the V0 implementation below. The separate profile registry, bespoke registration and lifecycle are not built; Pack V1
provides them.

### V0 (implemented, test-first)

Files:
- `src/pados/packs/research/contracts.py`:
  - phases, phase states, artifact kinds, run status and claim status;
  - `CampaignRequest` and `ResumeSpec`;
  - `ResearchRunOutput`, the untrusted runtime return;
  - `ResearchRunManifest` and its entries.
- `src/pados/packs/research/manifest.py`:
  - `build_run_manifest` admits a run fail-closed: exit code, phase names, states and order, egress hosts, path admission (one unauthorized path refuses the whole run), quarantine of unknown roots, phase output envelopes, resume continuity, final-evaluation metrics read from the artifact, claims marked `EVIDENCED` or `NOT_EVIDENCED`;
  - `verify_run_manifest` re-derives everything.
- `src/pados/packs/research/pack.py`:
  - `ResearchRuntimeProfile` and `research_pack_manifest` (profile -> Pack V1 version);
  - the `ResearchRuntime` protocol (operator-supplied, untrusted);
  - `ResearchCampaignAdapter` (task-pinned request digest, agent allowlist, closed failures);
  - the SCHEMA and REFERENCE_CHECK checkers;
  - `research_projection`.

Tests (synthetic fixtures `tests/packs/research/_research_fixtures.py`, upstream-shaped run layout):
- `test_research_manifest.py` covers a complete run and these failure cases:
  - malformed output; non-zero exit; missing artifact; digest mismatch;
  - unauthorized paths; quarantine;
  - invalid phase or state; phase order; a partial or failed run; paper output without the paper phase;
  - resume mismatch; egress violation;
  - unevidenced claims and a forged claim upgrade.
- `test_research_pack.py` covers:
  - the profile -> Pack V1 manifest and invalid profiles;
  - a cloud profile (allowlist and secret refs);
  - runtime unavailable (unplannable, retryable); timeout (non-retryable); a crashing runtime leaks nothing;
  - runtime output violations; an agent outside the allowlist; a stale request digest;
  - classification above the ceiling;
  - E2E through the executor;
  - execution succeeds but verification fails;
  - a ResultCandidate via the existing work-protocol functions;
  - unevidenced claims never presented, and partial runs only as partial goals.
- `tests/capability/test_pack_boundaries.py` now also scans `pados.packs.research`.

Not built in V0, and kept as separate work:
- the container runner (`agsci` argv construction and isolation, original Task 4);
- the deterministic decomposition template (original Task 6).

Both plug in behind `ResearchRuntime` and the existing template registry without changing the contracts.

### Task 7: Standalone upstream fixture capture (blocked: provider credentials required)

**Files:**
- Add sanitized fixture under `tests/fixtures/research/agsci_circpack_real/` or a fixture-generation manifest if artifacts are too large.
- Update: `docs/packs/research/AI_SCIENTIST_V3_RESEARCH_PACK_V0.md`

**Interfaces:**
- Consumes: a real standalone reduced-budget `examples/circpack` run from pinned AI-Scientist-v3.
- Produces: sanitized immutable fixture/manifest used to test parser compatibility.

- [ ] **Step 1: Run upstream standalone with the pinned revision**

Use upstream `agsci doctor`, `--dry-run`, then the reduced-budget `circpack` command documented in the spec.

- [ ] **Step 2: Sanitize and hash the fixture**

Exclude credentials, host-specific paths, raw private logs, caches, and large unnecessary artifacts.

- [ ] **Step 3: Add a compatibility test that fails on the new fixture before parser changes if needed**

- [ ] **Step 4: Make the smallest compatibility change and verify GREEN**

- [ ] **Step 5: Commit**

`test(research): pin real agentic scientist circpack evidence fixture`

---

### Task 8: Live Docker UAT gate (`REAL_RUNTIME_NOT_VALIDATED`)

**Files:**
- Create: `scripts/validation/validate_research_pack_live.py`
- Update: `docs/packs/research/AI_SCIENTIST_V3_RESEARCH_PACK_V0.md`

**Interfaces:**
- Consumes: locally installed/pinned AI-Scientist-v3 Docker runtime and valid provider credentials.
- Produces: machine-readable gate output without committing secrets or raw private logs.

- [ ] **Step 1: Add validation-script tests for PASS/FAIL gate aggregation**

- [ ] **Step 2: Verify RED, implement, verify GREEN**

- [ ] **Step 3: Execute live UAT on the target machine**

Required gates:

`RESEARCH_PACK_V0_CONTRACTS=PASS`

`RESEARCH_PACK_V0_MANIFEST_VERIFICATION=PASS`

`RESEARCH_PACK_V0_POLICY_BOUNDARY=PASS`

`RESEARCH_PACK_V0_STANDALONE_CIRCPACK_FIXTURE=PASS`

`RESEARCH_PACK_V0_LIVE_DOCKER_UAT=PASS`

- [ ] **Step 4: Run the full repository test suite and record any pre-existing failures explicitly**

Run: `python -m pytest`

- [ ] **Step 5: Commit**

`test(research): add AI Scientist v3 live UAT gate`

---

### Task 9: V1 phase-level orchestration (separate milestone)

**Files:**
- Extend `src/pados/packs/research/` with phase descriptors/adapters.
- Add `tests/packs/research/test_phase_graph.py`.
- Create `docs/packs/research/AI_SCIENTIST_V3_RESEARCH_PACK_V1.md`.

**Interfaces:**
- Adds: `RESEARCH_TASK_DRAFT`, `RESEARCH_INVESTIGATE`, `RESEARCH_ABLATE`, `RESEARCH_FINAL_EVALUATE`, `RESEARCH_POSTHOC`, `RESEARCH_PAPER`, optional `RESEARCH_REVIEW`.
- Uses the same pinned runtime profiles and manifest schema from V0.

- [ ] **Step 1: Write a failing deterministic graph test**

- [ ] **Step 2: Add phase-level capabilities one at a time with RED/GREEN cycles**

- [ ] **Step 3: Verify resume binding to the same upstream run id and run-local task snapshot**

- [ ] **Step 4: Verify policy/resource changes between phases fail closed through normal Fabric re-planning/revalidation**

- [ ] **Step 5: Run full regressions and document V1 acceptance separately**

- [ ] **Step 6: Commit**

`feat(research): orchestrate agentic scientist phases through capability fabric`

## Self-review result

- Spec coverage: goals, authority boundary, profiles, secrets, artifacts, verification, failure semantics, V0 and V1 are mapped to tasks.
- Type consistency: V0 centers on `ResearchRuntimeProfile`, `ResearchCampaignRequest`, `ResearchRunManifest`, `ResearchCampaignAdapter`.
- Review Focus: each named high-risk condition is assigned to contract, manifest, profile, runner, or adapter tests.
- Scope: V0 is independently shippable and testable; V1 is explicitly a separate milestone.