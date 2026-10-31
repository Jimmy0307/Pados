# Capability Pack V1 — design

Date: 2026-10-31. Status: IMPLEMENTED on `feat/capability-expansion-v1` (base `main@e550141`, the P7 Authority
Baseline). Plan: `docs/superpowers/plans/2026-10-31-capability-pack-v1.md`. Recovered from the unpublished
`feat/capability-pack-v1` design (base `464b385`) and re-applied on the post-P7 baseline with the changes below.

## Problem

PADOS already has every *execution* abstraction an extension needs: `CapabilityDescriptor` /
`AdapterDescriptor` (`pados.capability.model`), the operator-driven `CapabilityRegistry` (installed ≠ registered ≠
authorized), the `CapabilityAdapter` protocol (`available()` / `execute(StageRequest, context=StageContext)`), the
single execution path `CapabilityExecutor.execute` (plan reproduction → policy → `bind_stage` → `run_bound_stage`
→ verification → hash-chained `StageReceipt`s), and per-output-contract deterministic verification hooks
(`ValidatorRegistry`). What it does **not** have is a *versioned unit of extension*: today every pack (OCR,
algorithms, extraction, retrieval, intelligence) is a module of constants wired in by hand, with no pack identity,
version, provenance, runtime requirements, evidence requirements, failure vocabulary, lifecycle, or removal story.
Third-party capabilities (8D problem solving, AI Scientist research runs, OCR engines, CAD tools…) therefore have no
contract to be reviewed against.

## Decision

Capability Pack V1 is a **versioned extension contract, not an authority**: one immutable, content-addressed
manifest (`CapabilityPackManifest`, schema `pados.capability.pack.v1`) that *declares* a set of existing
`CapabilityDescriptor`s and `AdapterDescriptor`s plus the metadata above, validated fail-closed, and projected onto
the existing registry and verification hooks. It is implemented as one module inside the existing fabric package,
`src/pados/capability/pack.py` — no new package, registry, scheduler, router, executor, receipt type, store or CAS.

```
pack manifest ──validate──► PackIndex (pure, in-memory; descriptor digests ↔ pack identity)
     │
     ├─ operator registers via EXISTING CapabilityRegistry (install / register_capability / register_adapter)
     ├─ evidence + output hooks via EXISTING ValidatorRegistry (REFERENCE_CHECK per output contract)
     └─ execution ONLY via EXISTING CapabilityExecutor  → StageReceipt (existing) ──linkage──► pack@version
```

Invariants (each has a test):

* **Installation ≠ authorization.** A pack carries no grant; use still needs the project-bound grant
  `CAPABILITY:<id>` through `pados.execution.authorization`. The manifest only *states* which grants a capability
  needs, derived from its descriptors.
* **Registration ≠ selection.** Registering a pack's adapters only makes them plannable; selection stays with
  `CapabilityPlanner` / the Worker Router.
* **Execution success ≠ acceptance.** Pack output is `StageOutput(semantic_authority="NONE")`; verification yields
  evidence only; acceptance stays with `work_protocol` `ResultCandidate` → governed acceptance → existing CAS.
* **Pack ≠ task store.** A pack module may not import scheduler, router, CAS, approval, state, memory, DB, or the
  executor/registry internals (`tests/capability/test_pack_boundaries.py`).

## Contract (`CapabilityPackManifest`)

All models are frozen pydantic, `extra="forbid"`; ids reuse the fabric's patterns. `manifest_digest` is the
canonical hash of the whole manifest.

### Identity
`pack_id` (`^[a-z][a-z0-9_.-]{2,79}$`), `version` (fabric version pattern), `provider` (who supplies the runtime:
`provider_id`, `runtime_id`, `runtime_version`), `capability_ids` (derived from the declared descriptors; must
equal the set they declare), `provenance` (`source` = `IN_TREE | OPERATOR_SUPPLIED | THIRD_PARTY`, `license` =
existing `LicenseInfo`, `source_digest` = SHA-256 of the reviewed source/artifact or `None` for in-tree,
`publisher`).

### Inputs / outputs
Per capability: the existing `CapabilityDescriptor.input_contract` / `output_contract` (`name/vN`) are the typed
contracts; `max_classification` is the classification ceiling; outputs are bounded by `ScopedArtifacts`
(`max_outputs`, `max_output_bytes`) and the manifest's declared `max_output_bytes` (must not exceed the fabric
bound).

### Runtime requirements
`runtime` = `runtime_type` (`IN_PROCESS | LOCAL_SUBPROCESS | LOCAL_HTTP | MCP | CONTAINER | EXTERNAL_SERVICE`),
`device` (existing `ResourceRequirement`s), `execution_config_digest` (SHA-256 of the config the capability was
measured/validated under — the Measured Capability rule), `timeout_seconds` (bounded), `network` (existing
`NetworkRequirement`; must match every adapter's declaration).

### Security
`required_grants` (derived `CAPABILITY:<id>`, checked, never granting), `secret_refs` (`secretref://` names only,
validated with the existing grammar — never values), `allowed_roots` (logical root names, not paths),
`project_binding` (`PROJECT_SCOPED` only in V1), `classification_ceiling` (≤ every descriptor's
`max_classification`), `egress` (`NONE | LOOPBACK_ONLY | ALLOWLIST` with host names; must be consistent with
`network`).

### Runtime enforcement (`GovernedPackAdapter`)
The manifest is enforced on every request, not only at validation: a request for a capability the adapter does not
implement is `PACK_CAPABILITY_MISMATCH`; a request classified above the pack's own `classification_ceiling` is
`PACK_CLASSIFICATION_CEILING_EXCEEDED` (the ceiling may be narrower than the descriptors'); an availability probe that
raises (missing runtime binary, unreachable provider) is reported as *unavailable*, so the planner finds the pack
INFEASIBLE instead of crashing — an optional provider being unavailable never blocks the platform.

### Execution
No field. The contract states — and boundary tests enforce — that pack code is reached only through
`CapabilityExecutor` with the existing scheduler/router; a pack cannot call `bind_stage`, `run_bound_stage`,
`guarded`, or any CAS/acceptance API.

### Evidence
`evidence_requirements`: closed set of keys every successful `StageOutput.evidence` of the pack must carry
(`runtime_version`, `device`, `config_digest`, `provider_id`, `artifact_digest`, plus pack-specific keys). The
pack's REFERENCE_CHECK hook (`pack_evidence_check`) refuses outputs missing them or contradicting the manifest
(e.g. a different `runtime_version` or `config_digest`).

### Verification
`verification_hooks`: per output contract, the checker id of a deterministic validator the pack supplies
(`CheckerSpec`); registered by the operator into the existing `ValidatorRegistry` (one checker per contract, no
override). Semantic acceptance remains outside the pack.

### Lifecycle
The pack's state is the **existing** capability-loop vocabulary (`CapabilityState`): `DISCOVERED`, `EXPERIMENTAL`,
`VALIDATION_REQUIRED`, `VALIDATED`, `BLOCKED`, plus the registry's `REVOKED` for **DISABLED**. Transitions reuse
`pados.capability_loop.lifecycle.propose_transition` (VALIDATED needs a `validation_outcome_ref`; proposals are
inert, `auto_apply=False`). A manifest may declare its *initial* state only as `DISCOVERED` or `EXPERIMENTAL`; it can
never declare itself `VALIDATED`.

### Failure
`failure_codes`: closed vocabulary (`^[A-Z][A-Z0-9_]{2,63}$`) with `retryable: bool` each; adapters raise only
`CapabilityAdapterError(code, retryable=…)` with a declared code (`pack_failure_code` normalizes anything else to
`PACK_ADAPTER_FAILED`, never echoing content). Fail closed; retryability is advisory input for the existing
circuit-breaker/fallback policy, never a self-retry inside the pack.

### Rollback / removal
`removal`: `DISABLE` (registry lifecycle → `REVOKED`, adapters unplannable, receipts kept), `ROLLBACK` (the existing
registry is in-memory, has no unregister and treats `REVOKED` as terminal, so rollback is deterministic: disable the
current version, then register the previous manifest into a freshly built registry; `PackIndex` keeps every
version's descriptor digests so old receipts stay attributable),
`ARTIFACT_CLEANUP` (only pack-owned scratch, never content-addressed evidence), `state_compatibility` (the
`output_contract` versions a new pack version still produces).

### Observability
Receipts are the existing `StageReceipt`s; `PackIndex.attribution(receipt)` maps a receipt's
`capability_digest`/`adapter_digest` to `(pack_id, version, manifest_digest)`. Timing and resource usage are the
receipt's existing fields / `StageOutput.metrics`; audit is the registry's `RegistryEvent`s.

### Registration rules
`register_pack` checks everything before the registry is touched: `PACK_DISCOVERED_NOT_REGISTRABLE`,
`PACK_IMPLEMENTATION_MISSING` / `_UNDECLARED` / `_MISMATCH`; an adapter id that already exists in this registry
(registered, superseded or disabled) is `PACK_STALE_REGISTRATION`; a capability already registered is reused only
when its descriptor is identical (compared at the declared lifecycle) and is otherwise `PACK_CAPABILITY_CONFLICT` — a
new version can never silently redefine a capability. A new version therefore carries new adapter ids.

### Hand-off toward acceptance
No bridge from fabric receipts to `work_protocol` exists in Core today, and none is added. A pack may expose a pure
projection of its verified output onto the bounded provider-response fields that the existing
`normalize_capability_result` accepts (contributions, evidence references, goal outcome, metrics). PADOS supplies
every task, authorization, provenance and receipt field, composes with `compose_results`, and the candidate stays
`PENDING_GOVERNED_ACCEPTANCE` until the existing governed verification, approval and single CAS. No pack imports
`pados.work_protocol`, and no pack may name an acceptance or CAS entry point (boundary tests).

## Validation rules (fail closed, `PackManifestError(code)`)

1. schema/ids/versions; 2. every adapter's `capability_id` is a declared capability; 3. no duplicate ids;
4. classification ceiling ≤ descriptors; 5. network/egress consistent; 6. secret refs are refs; 7. evidence keys
bounded and include the mandatory core keys; 8. failure codes closed and bounded; 9. initial lifecycle ∈
{DISCOVERED, EXPERIMENTAL}; 10. third-party provenance needs a `source_digest`; 11. size bounds (≤ 32
capabilities, ≤ 64 adapters, ≤ 64 failure codes); 12. `capability_ids` equals the declared set.

## Reference packs

1. **Synthetic reference pack** (`src/pados/packs/reference/`): one deterministic capability exercising the whole
   contract in tests.
2. **Structured Problem Solving Pack** (`src/pados/packs/problem_solving/`), profiles **8D** (first) and **A3**
   (proves a profile is data, not a code path): evidence-first pipeline (evidence admission → claims with
   citations → grounding verification → generation → critic/deterministic verification → `StageOutput` candidate →
   optional work-protocol projection). The task, never the pack, pins the evidence-pack digest
   (`PS_EVIDENCE_DIGEST_STALE`) and names the authorized sources (`PS_EVIDENCE_SOURCE_UNAUTHORIZED`, whole pack
   refused, never filtered); restated duplicate evidence is refused; insufficient evidence yields `ABSTAINED`;
   the critic is advisory only. Generic contracts and synthetic fixtures only; no company source, data, documents,
   prompts, endpoints or credentials.

## Known limits of V1 (stated, not hidden)

* `timeout_seconds`, `egress`/`egress_hosts`, `allowed_roots` and `secret_refs` are **declarations** checked for
  consistency; V1 has no isolation runner that enforces them at execution time. An `IN_PROCESS` adapter can do
  whatever its Python code does, so `IN_PROCESS` is allowed only for `IN_TREE` (repository-reviewed) packs; code not
  reviewed in-tree must declare an isolated runtime (`PACK_RUNTIME_ISOLATION_REQUIRED`). Allowlisted egress hosts
  must be public names (no IP literals, loopback or link-local names).
* The registry has no capacity query, so a full registry is detected only when it refuses; `register_pack` then
  revokes what it registered (`PACK_REGISTRATION_INCOMPLETE`) and nothing of the pack stays plannable.
* Structured Problem Solving: claim text and critic reason codes are untrusted and recorded only in the claim set
  and report artifacts; contributions are rendered from grounded tokens and admitted evidence statements. A profile
  binds each section to the subjects it may assert; the subject vocabulary is the profile's data.

## Non-goals

No second scheduler/router/CAS/task model; no auto-install, auto-promotion or self-authorization; no network
fetching of packs; no signing trust root (V2); no change to `EnterpriseCapabilityManifest` (it remains the
enterprise integration manifest; expressing it as a Pack V1 is future work); no UI.
