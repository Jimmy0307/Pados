# AI-Scientist-v3 Research Capability Pack Design

Date: 2026-10-26
Status: V0 implemented as a Capability Pack V1 consumer (2026-10-31, on `main@6f5eecb`); live runtime `REAL_RUNTIME_NOT_VALIDATED`
Upstream: `SakanaAI/AI-Scientist-v3` (`The Agentic Scientist`)

## Intent

Add AI-Scientist-v3 to PADOS as a governed research runtime without importing its orchestration authority into PADOS Core.

PADOS remains the authority for task intent, capability selection, policy, resource eligibility, artifact provenance, verification evidence, receipts, governed acceptance, CAS, and outcome learning. AI-Scientist-v3 remains an external research execution engine responsible for its own research workflow: task drafting, investigation, ablations, sealed final evaluation, post-hoc analysis, paper generation, and optional review.

The integration exists to make automated computational research a first-class PADOS capability while preserving the existing principle:

`MODEL OUTPUT != TRUTH` and `EXECUTION COMPLETE != SEMANTIC ACCEPTANCE`.

## Alignment with Capability Pack V1 (2026-10-31)

Capability Pack V1 (`pados.capability.pack.v1`, PR #18,
`docs/superpowers/specs/2026-10-31-capability-pack-v1-design.md`) is now the only packaging, registration and extension
convention. The research pack is a **consumer** of it and keeps no parallel abstraction. This section supersedes any
conflicting statement below:

| Earlier proposal | Now |
|---|---|
| `ResearchRuntimeProfileRegistry` with its own digest identity | Removed. An operator `ResearchRuntimeProfile` becomes **one content-addressed Pack V1 version** via `research_pack_manifest(profile)` and registers through the existing `CapabilityRegistry` (`register_pack`). |
| `register_research_pack()` / bespoke adapter registration | `ResearchCampaignAdapter` wrapped by `GovernedPackAdapter`, executed only by the existing `CapabilityExecutor`. |
| Registry lifecycle `EXPERIMENTAL -> ACTIVE` gated by live UAT | Capability-loop lifecycle: the pack starts `EXPERIMENTAL`; a recorded live-UAT outcome is the `validation_outcome_ref` an operator needs to propose `VALIDATED` (`propose_pack_transition`). |
| Adapter-specific verification hook | Pack V1 verification hooks in the existing `ValidatorRegistry`: SCHEMA `run_manifest_valid`, REFERENCE_CHECK `run_manifest_reference_check` (re-derives the manifest from its own outputs). |
| Profile network class, temp root, secrets | Pack V1 security: `egress` `NONE` (offline) or `ALLOWLIST` (public hosts only), `allowed_roots` logical names, `secret_refs` as `secretref://` only, classification ceiling `INTERNAL`. Runtime `CONTAINER`: code not reviewed in-tree never runs `IN_PROCESS`. Provenance `OPERATOR_SUPPLIED`, `source_digest` = the pinned image digest. |
| Failure semantics prose | Pack V1 closed failure vocabulary (`RESEARCH_*`); only `RESEARCH_RUNTIME_UNAVAILABLE` is retryable; a timeout never retries with a weaker network or model class. |

Research-specific contracts stay in `src/pados/packs/research/`:
- `CampaignRequest`, pinned by digest as a task parameter;
- `ResearchRunOutput`, the untrusted runtime return;
- `ResearchRunManifest`, which PADOS derives;
- `research_projection`, which hands off to the existing `normalize_capability_result` -> `compose_results` path. The resulting candidate stays `PENDING_GOVERNED_ACCEPTANCE`.

## Goals

- Let PADOS launch an AI-Scientist-v3 campaign through the existing Capability Fabric.
- Keep the upstream runtime independently upgradeable and replaceable.
- Preserve PADOS policy, classification, network, resource, receipt, and CAS boundaries.
- Capture the upstream run as immutable, hash-addressed research evidence.
- Support a low-friction V0 that can be dogfooded quickly.
- Evolve to phase-level orchestration in V1 by reusing AI-Scientist-v3's existing phase/resume interface.
- Make results usable by PADOS Work Map, Outcome Learning, and later evidence/claim graph work.

## Non-goals

- Do not vendor or fork AI-Scientist-v3 into PADOS Core.
- Do not let the upstream runtime install, authorize, select, or accept PADOS capabilities.
- Do not let an AI-Scientist-v3 paper, score, reviewer output, or final-evaluation status directly commit semantic state.
- Do not expose provider secrets in PADOS task contracts or frontend payloads.
- Do not add a second scheduler, router, or semantic commit path.
- Do not reimplement AI-Scientist-v3's internal research loop inside PADOS.

## Approaches considered

### A. Vendor/fork AI-Scientist-v3 inside PADOS

Rejected. It creates dependency and license-maintenance coupling, duplicates orchestration concerns, makes upstream upgrades expensive, and risks turning PADOS Core into a monolithic research agent.

### B. One opaque external runtime forever

Useful for a spike, but insufficient as the final architecture. PADOS would only see a single long-running action and would lose stage-level policy, resource routing, failure isolation, and useful Work Map lineage.

### C. External Research Pack with V0 opaque campaign and V1 phase-level stages

Selected.

V0 proves the runtime boundary quickly with one governed `RESEARCH_CAMPAIGN` capability. V1 maps AI-Scientist-v3's existing resumable phases into PADOS `CapabilityGraph` stages without changing the upstream research logic.

## Architecture

```text
PADOS Task / Goal
        |
        v
TaskContract + project grant + classification + network policy
        |
        v
Capability Fabric
        |
        +--> V0: RESEARCH_CAMPAIGN
        |         |
        |         v
        |   AI-Scientist-v3 container
        |   `agsci run <task> ...`
        |
        +--> V1 CapabilityGraph
                  |
                  +--> RESEARCH_TASK_DRAFT
                  +--> RESEARCH_INVESTIGATE
                  +--> RESEARCH_ABLATE
                  +--> RESEARCH_FINAL_EVALUATE
                  +--> RESEARCH_POSTHOC
                  +--> RESEARCH_PAPER
                  +--> RESEARCH_REVIEW (optional)

AI-Scientist-v3 results/<task>/<agent>/<run_id>/
        |
        v
ResearchRunManifest + hashes + bounded metadata
        |
        v
PADOS Verification / StageReceipt / PlanReceipt
        |
        v
ResultCandidate
        |
        v
existing governed acceptance -> ResultReceipt -> CAS
```

## Integration boundary

The adapter is a subprocess/container adapter. It may invoke only a configured executable/runtime image and may only read/write explicitly staged run directories. The adapter does not receive the PADOS registry, policy engine, grants, CAS service, database connection, or semantic state writers.

AI-Scientist-v3 itself warns that its coding agents run with full tool permissions. Therefore PADOS must treat the runtime as untrusted execution and default to isolation. The first implementation must not run the upstream coding agents directly in the PADOS service process.

## V0 capability

Capability id: `RESEARCH_CAMPAIGN`

Purpose: launch one complete AI-Scientist-v3 run and return a manifest of evidence artifacts.

### Inputs

- `research_task_ref`: PADOS artifact reference to a staged AI-Scientist-compatible task directory or bounded research task bundle.
- `runtime_profile`: operator-registered profile name; not an arbitrary executable path.
- `agent_name`: allowlisted coding-agent identifier.
- `agent_model`: optional allowlisted model identifier.
- `preset`: optional allowlisted upstream preset.
- `phase_policy`: defaults to the upstream full six-phase pipeline; may restrict phases but cannot add arbitrary commands.
- `budget_profile`: operator-defined idea/ablation/post-hoc budget profile.

No credentials, raw shell commands, host paths, Docker flags, URLs, or environment-variable values are allowed in the task contract.

### Outputs

- `research_run_manifest.json`
- content hashes for the run-local task snapshot and allowed result artifacts
- phase status summary
- upstream run id
- selected agent/model identifiers
- final evaluation status when available
- paper PDF/manuscript refs when available
- review JSON ref when explicitly enabled
- telemetry summary derived from upstream structured event/cost files when present

Large raw run directories remain artifact storage; receipts carry references and hashes, not arbitrary logs.

## V1 phase capabilities

V1 uses the upstream phase interface and shared `paths.run_dir` resume anchor. Each PADOS stage executes only the named phase(s) and emits a manifest that becomes the next stage's input.

Proposed capability ids:

- `RESEARCH_TASK_DRAFT`
- `RESEARCH_INVESTIGATE`
- `RESEARCH_ABLATE`
- `RESEARCH_FINAL_EVALUATE`
- `RESEARCH_POSTHOC`
- `RESEARCH_PAPER`
- `RESEARCH_REVIEW`

The PADOS template remains deterministic: it does not ask a model to decide the graph. Optional review is selected by an operator/template policy, not by upstream code.

## Runtime profiles

The pack uses operator-managed profiles, for example:

- `agsci.docker.cpu.default`
- `agsci.docker.gpu.2`
- `agsci.docker.cpu.codex`
- `agsci.docker.cpu.claude`

A profile binds:

- upstream version or commit
- container image digest
- executable entrypoint
- allowed agent backends
- GPU requirement
- network class
- temporary-storage root
- maximum runtime/output budget
- allowed environment SecretRefs

Task callers select only a registered profile id. They cannot substitute an image, executable, command, mount, or secret.

Since Pack V1 there is no profile registry: each profile is one Pack V1 version (`research_pack_manifest`). The example profile names above are illustrative; no profile with real digests is committed. Tests use synthetic profiles.

## Policy and secrets

- Provider keys stay server-side as `SecretRef`-backed configuration.
- The adapter materializes only the provider credentials required by the selected allowlisted agent.
- Network policy must intersect the task policy and runtime profile policy.
- A no-network task cannot silently escalate to a cloud coding agent.
- GPU profiles must pass the existing ResourceInventory/worker-routing checks before dispatch.
- The adapter must never copy `.env` contents into receipts or manifests.

## Artifact and evidence model

The adapter publishes a bounded `ResearchRunManifest` rather than trusting the upstream directory structure implicitly.

Minimum manifest fields:

- schema version
- PADOS task id / plan hash / stage id
- upstream repository identity and pinned revision
- runtime profile id and image digest
- upstream run id
- phase list and phase states
- task snapshot digest
- artifact entries: logical kind, relative path, size, SHA-256
- final-evaluation summary when present
- degraded/failure state
- timestamps/durations

Allowed artifact kinds initially:

- task snapshot
- investigation summary/metrics
- ablation aggregate/manifest
- final-evaluation result
- post-hoc manifest
- paper source/PDF/manifest
- explicit review JSON
- structured event/cost summaries

Unknown files are retained only in quarantined raw storage and are not promoted into trusted PADOS evidence automatically.

## Verification

V0 verification is structural and provenance-oriented, not scientific truth validation.

The verifier checks:

1. the runtime exited under the registered profile;
2. run root stayed inside the owned output directory;
3. the manifest contains only allowed relative paths;
4. every promoted artifact exists and matches its SHA-256/size;
5. the run-local task snapshot is present when required;
6. claimed phases have the expected phase output envelope;
7. final-evaluation claims are supported by the corresponding upstream artifact;
8. paper/review refs are only present when those phases produced them;
9. no secret values are copied into promoted manifest fields.

A PASS means `research runtime evidence is internally consistent`, not `the scientific conclusion is true`.

Scientific/claim verification is a later layer and must remain separate from runtime verification.

## Failure semantics

- runtime unavailable -> capability unavailable / no execution
- non-zero upstream exit -> failed stage with no accepted research outputs, but diagnostic refs may be retained under policy
- timeout/resource exhaustion -> failed stage; no permissive fallback to a weaker network or model class
- malformed manifest -> verification failure; outputs withheld
- hash mismatch/path escape -> verification failure; outputs withheld
- missing optional ablation/post-hoc/review output -> degraded only when the selected upstream policy permits degradation
- missing required final evaluation -> fail when the task contract requires final evaluation

## V0 -> V1 migration

V0 intentionally treats the upstream campaign as one adapter invocation. The `ResearchRunManifest` schema is designed so V1 can reuse it per phase. V1 adds phase adapters and a deterministic decomposition template; it does not change the upstream runtime or CAS boundary.

## Direct dogfood target

Before PADOS integration is marked live, run AI-Scientist-v3 standalone on its CPU-only `examples/circpack` example with reduced budgets. Capture the resulting run directory and use that frozen directory as the first fixture for manifest parsing and verification tests.

## Acceptance gates

`RESEARCH_PACK_V0_CONTRACTS = PASS`

`RESEARCH_PACK_V0_MANIFEST_VERIFICATION = PASS`

`RESEARCH_PACK_V0_POLICY_BOUNDARY = PASS`

`RESEARCH_PACK_V0_STANDALONE_CIRCPACK_FIXTURE = PASS`

`RESEARCH_PACK_V0_LIVE_DOCKER_UAT = PASS`

Status on 2026-10-31:

| Gate | Status |
|---|---|
| `RESEARCH_PACK_V0_CONTRACTS` | PASS (synthetic) |
| `RESEARCH_PACK_V0_MANIFEST_VERIFICATION` | PASS (synthetic, incl. mutation checks) |
| `RESEARCH_PACK_V0_POLICY_BOUNDARY` | PASS (agent allowlist, egress, classification, path admission, boundary tests) |
| `RESEARCH_PACK_V0_STANDALONE_CIRCPACK_FIXTURE` | **NOT RUN**: blocked externally (see below) |
| `RESEARCH_PACK_V0_LIVE_DOCKER_UAT` | **`REAL_RUNTIME_NOT_VALIDATED`** |

External blocker: upstream `agsci run examples/circpack` needs coding-agent provider credentials. The README asks
for `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` in a `.env`, and warns that agents inherit them with full tool
permissions. No such credential is available to the implementing session, and none may be committed. Docker itself
is available on the VMware Linux VM. The circpack fixture and live UAT therefore wait for an operator-provided
credential.

Only after those gates should the capability move from EXPERIMENTAL toward ACTIVE. V1 phase-level orchestration is a separate acceptance milestone.