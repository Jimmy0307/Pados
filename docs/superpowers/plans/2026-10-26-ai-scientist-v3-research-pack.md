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

### Task 1: Research pack contracts and runtime-profile schema

**Files:**
- Create: `src/pados/packs/research/__init__.py`
- Create: `src/pados/packs/research/contracts.py`
- Create: `tests/packs/research/test_contracts.py`

**Interfaces:**
- Produces: `ResearchRuntimeProfile`, `ResearchCampaignRequest`, `ResearchArtifactEntry`, `ResearchRunManifest`, validation helpers.

- [ ] **Step 1: Write failing contract tests**

Cover a valid CPU Docker profile; rejection of raw executable/command/env values from request data; normalized allowlisted agent/model/preset identifiers; rejection of absolute or parent-traversing artifact paths; manifest schema/version and SHA-256 validation.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/packs/research/test_contracts.py -q`

Expected: failures because `pados.packs.research` contracts do not exist.

- [ ] **Step 3: Implement minimal immutable contracts**

Keep runtime execution details in operator-owned `ResearchRuntimeProfile`; keep caller-selectable data in `ResearchCampaignRequest`. Use closed schemas consistent with the existing Capability Fabric contract style.

- [ ] **Step 4: Run focused tests and verify GREEN**

- [ ] **Step 5: Commit**

`feat(research): add governed research runtime contracts`

---

### Task 2: Manifest builder and verifier

**Files:**
- Create: `src/pados/packs/research/manifest.py`
- Create: `tests/packs/research/test_manifest.py`
- Add fixture tree: `tests/fixtures/research/agsci_circpack_minimal/`

**Interfaces:**
- Consumes: `ResearchRunManifest`, owned run root, expected selected phases.
- Produces: deterministic manifest and verification result/evidence suitable for the existing adapter verification hook.

- [ ] **Step 1: Write failing tests**

Cover valid bounded artifacts, SHA mismatch, missing required artifact, path escape, unexpected promoted file, optional degraded phase, required final-evaluation absence, and secret-pattern exclusion from promoted metadata.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/packs/research/test_manifest.py -q`

- [ ] **Step 3: Implement deterministic manifest collection/verification**

Promote only an explicit allowlist of logical artifact kinds. Hash files with SHA-256, record relative paths only, and keep large/raw logs outside receipt payloads.

- [ ] **Step 4: Verify GREEN**

- [ ] **Step 5: Commit**

`feat(research): verify agentic scientist evidence manifests`

---

### Task 3: Registered runtime profile store

**Files:**
- Create: `src/pados/packs/research/profiles.py`
- Create: `tests/packs/research/test_profiles.py`

**Interfaces:**
- Produces: `ResearchRuntimeProfileRegistry` with operator-only registration and immutable profile digest lookup.
- Consumes later: adapter receives a resolved registered profile, never caller-supplied execution configuration.

- [ ] **Step 1: Write failing tests**

Cover duplicate registration refusal, profile digest stability, changed revision/image digest producing a new identity, unknown profile refusal, and caller inability to override executable/image/env.

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement minimal registry**

Follow existing PADOS registry/event/digest conventions where practical without creating a second global authority. This registry is pack configuration, not capability authorization.

- [ ] **Step 4: Verify GREEN**

- [ ] **Step 5: Commit**

`feat(research): add pinned research runtime profiles`

---

### Task 4: Isolated AI-Scientist-v3 runner

**Files:**
- Create: `src/pados/packs/research/runner.py`
- Create: `tests/packs/research/test_runner.py`

**Interfaces:**
- Consumes: resolved `ResearchRuntimeProfile`, `ResearchCampaignRequest`, owned staging/output directories, narrow SecretRef resolver callback.
- Produces: `ResearchProcessResult` containing exit status, bounded diagnostics, upstream run root, and no secret values.

- [ ] **Step 1: Write failing tests using a fake executable fixture**

Test exact argv construction, no `shell=True`, no arbitrary caller command fragments, owned cwd/output roots, timeout behavior, non-zero exit, output-size cap, and environment allowlist behavior.

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement minimal runner**

Construct argv only from profile constants plus validated allowlisted identifiers. Never interpolate a shell command. Resolve only the profile's named SecretRefs immediately before execution and do not retain their values in result objects.

- [ ] **Step 4: Verify GREEN**

- [ ] **Step 5: Commit**

`feat(research): add isolated agentic scientist runner`

---

### Task 5: Capability adapter and registration

**Files:**
- Create: `src/pados/packs/research/adapters.py`
- Create: `src/pados/packs/research/registry.py`
- Create: `tests/packs/research/test_adapter.py`
- Modify: `src/pados/packs/research/__init__.py`

**Interfaces:**
- Produces: `ResearchCampaignAdapter` and `register_research_pack(...)`.
- Uses: existing `CapabilityAdapter`, descriptor, verification, ArtifactRef, policy, and receipt conventions.

- [ ] **Step 1: Write failing tests**

Test capability id `RESEARCH_CAMPAIGN`, EXPERIMENTAL lifecycle, policy-blocked execution never invoking the runner, success returning only manifest/artifact refs, upstream failure withholding promoted outputs, malformed manifest failing verification, and no automatic semantic acceptance.

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement adapter and registration**

The adapter stages input, invokes the runner, builds/verifies the manifest, and returns bounded artifact refs. It never receives CAS/database/registry mutation authority.

- [ ] **Step 4: Verify focused GREEN**

- [ ] **Step 5: Run existing Capability Fabric regression tests**

Run at minimum: `python -m pytest tests/capability tests/packs/research -q`

- [ ] **Step 6: Commit**

`feat(research): register AI Scientist v3 campaign capability`

---

### Task 6: Deterministic decomposition template and end-to-end V0 test

**Files:**
- Create: `tests/packs/research/test_research_campaign_e2e.py`
- Create or modify the pack registration/template file selected by the existing template pattern.
- Create: `docs/packs/research/AI_SCIENTIST_V3_RESEARCH_PACK_V0.md`

**Interfaces:**
- Input task type: proposed `RESEARCH_COMPUTATIONAL_CAMPAIGN`.
- Graph: one `RESEARCH_CAMPAIGN` stage in V0.
- Output: verified `ResearchRunManifest` artifact ref and normal PlanReceipt/ResultCandidate path.

- [ ] **Step 1: Write failing end-to-end Fabric test with fake runtime**

Assert TaskContract -> deterministic graph -> plan -> policy -> adapter -> manifest verification -> StageReceipt/PlanReceipt. Assert result is still a candidate and no semantic commit occurs in the pack.

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Register the deterministic template and capability**

- [ ] **Step 4: Verify GREEN and full relevant regressions**

Run: `python -m pytest tests/capability tests/packs/research tests/pipeline -q`

- [ ] **Step 5: Document operator setup and exact trust boundary**

- [ ] **Step 6: Commit**

`feat(research): complete governed research campaign v0`

---

### Task 7: Standalone upstream fixture capture

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

### Task 8: Live Docker UAT gate

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