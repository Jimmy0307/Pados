# PADOS Architecture

PADOS is an Intelligent Mindmap-driven Portable Cognitive & Execution Operating System. Its control plane owns intent, policy, scheduling, receipts, audit, and semantic acceptance. Models, tools, algorithms, people, and compute nodes are resources.

## Authority Boundaries

| Concern | Authority |
| --- | --- |
| Original user intent | Versioned `TaskEnvelope`, captured at authenticated intake |
| Goal relationships and lifecycle | PADOS goal protocol, projected from accepted task state |
| Capability allocation | Existing policy and capability router |
| Queue order and dispatch | Existing PADOS scheduler |
| Execution evidence | Existing execution and result receipts |
| Human acceptance | Existing governed decision path |
| Semantic state | Existing CAS commit path |
| Work Map | Rebuildable projection only |
| Static Web/PWA | Intent sender and projection reader only |
| CI and Pages | Build/distribution machinery only; never PADOS authority |
| Capability packs | Versioned extensions registered through the existing registry; never an authority |
| Optional external runtimes (REA, OpenMAIC) | Evidence providers only; never an authority |

No provider response, graph database, browser, GitHub runner, model, or capability pack is a new authority.

## Task Completion Flow

```mermaid
flowchart LR
  Intent[Authenticated original intent] --> Envelope[TaskEnvelope]
  Envelope --> Goals[Goal graph]
  Goals --> Plan[Capability and scheduling candidates]
  Plan --> Scheduler[Existing scheduler]
  Scheduler --> Resource[Selected worker and resource]
  Resource --> Result[CapabilityResult]
  Result --> Coverage[Discrete coverage map]
  Coverage --> Residual[Residual goals only]
  Residual --> Plan
  Coverage --> Compose[Provenance-preserving composition]
  Compose --> Verify[Verification]
  Verify --> Candidate[ResultCandidate]
  Candidate --> Approval[Human or governed acceptance]
  Approval --> CAS[Existing CAS]
```

`TaskEnvelope` preserves exact original intent and project, classification, priority, deadline, ScopeVersion, creator, and timestamps. Provider requests are projections of that envelope. Provider-specific response data is normalized into `CapabilityResult` using PADOS-supplied bindings, authorization metadata, provenance, and receipt references.

Coverage is discrete (`COMPLETE`, `PARTIAL`, `UNRESOLVED`, `BLOCKED`, or `REJECTED`). Residual generation excludes completed goals and avoids re-running aggregate parent work when an unresolved child is sufficient. A policy refusal or human decision requirement cannot be routed around by choosing a more permissive model.

Composition preserves source goal, worker, capability, resource, artifact, evidence, and receipt references. Conflicts are represented; one contribution does not silently replace another. A composed candidate remains unverified and semantically unaccepted until the existing gates succeed.

## Protocol Layer

Protocol documents declare `protocol_version` and `schema_version` and do not inherit provider API schemas. V1 accepts major version 1 through the supported minor level. Unknown additive fields round-trip. Required fields and exact schema identifiers are validated. Missing versions, unsupported major/minor versions, schema mismatches, invalid authorization bindings, or oversized documents fail closed.

Versioning rules:

- Additive optional fields may be introduced as a compatible minor revision.
- Removing or changing the meaning of a required field, changing authorization semantics, or changing a schema identifier incompatibly requires a new major version.
- Unknown fields may be preserved as inert protocol data; they do not grant authority.
- Authority-sensitive conversions must retain classification, project, ScopeVersion, authorization metadata, provenance, and receipts. Unsupported input is rejected rather than silently downgraded.

Deterministic algorithms declare their resource kind and do not imitate a model provider. Recommendations report integration and governance state separately and never auto-install or authorize a capability.

## Work Graph and Scheduler

The Work Graph is rebuilt from task envelopes, goals, assignments, results, artifacts, residuals, and decisions. It is a read projection and does not accept writes as semantic state. The current Remote Work Map can show only records present in its authorized Home Base projection; it does not invent assignment or result lineage when those bindings are absent.

The planner may explain scheduling candidates, dependencies, priority, deadline, required capabilities, resource needs, and human approval requirements. It does not own a queue. The existing scheduler remains the only source of queue order. Outcome Learning can inform candidate selection but cannot dispatch work.

## Web and Remote Operations

The PWA is static and can be hosted separately from Home Base. It sends versioned intent and reads authenticated projections. It does not store project payloads, provider API keys, or authoritative context. The service worker caches only the app shell and static assets and bypasses Remote Operations and API paths.

Home Base authenticates the actor, checks project access and current scope, enforces idempotency, and routes consequential operations through existing policy and authority. Cross-origin static hosting requires an explicit HTTPS origin allowlist. CORS is not authentication; remote access remains separately authenticated and bound to the private Home Base deployment.

## CI and Static Deployment

GitHub Actions use ephemeral hosted runners, have no persistent Home Base access, and do not receive production authority data. The Pages artifact contains static UI files only. A build or deployment job cannot approve tasks, choose a provider, access the authority database, or commit semantic state.

## Measured Capability Rule

A measured capability is `MODEL x RUNTIME x DEVICE x EXECUTION CONFIG x EVIDENCE`, never an unconditioned model property. The measured execution gate binds the execution configuration a capability was measured under and fails closed on any missing, inconsistent, incompatible or unverifiable setting (`docs/architecture/MEASURED_CAPABILITY_EXECUTION_CONTRACT.md`). Physical hardware acceptance is a separate, independently verified evidence artifact; CI never mocks it (`docs/supply_chain/HARDWARE_ACCEPTANCE_EVIDENCE.md`).

## Enterprise Interop

PADOS has been informed by and validated against real-world enterprise local-AI and structured problem-solving deployments. Interop follows two generic patterns. Neither one grants authority to an external system:

- **Enterprise Local Runtime Adapter** (`docs/architecture/ENTERPRISE_LOCAL_RUNTIME_INTEROP.md`). An external runtime plane submits evidence: availability, health, leases, device state, artifact digests and benchmark references. The evidence becomes the existing live observations and `RuntimeBinding`, and selection stays with the Worker Router. Leases are evidence, not authorization, and model authorization stays explicit.
- **Governed Outcome Learning Adapter** (`docs/architecture/GOVERNED_OUTCOME_LEARNING_INTEROP.md`). External problem-solving evidence becomes authenticated outcome observations, then advisory confidence, then inert candidates, which need evaluation and governed promotion. Training is offline and gated, and its result re-enters only as a new measured artifact.

## Capability Packs and Optional Providers

Capability Pack V1 (schema `pados.capability.pack.v1`) is a versioned packaging and registration convention over the existing Capability Fabric (`docs/capability/CAPABILITY_FABRIC_V1.md`). A pack manifest declares existing `CapabilityDescriptor`s and `AdapterDescriptor`s with identity, provenance, runtime and evidence requirements. An operator registers them through the existing `CapabilityRegistry`; execution runs only through the existing `CapabilityExecutor`, verification and receipts; acceptance stays with `ResultCandidate`, governed acceptance and the existing CAS. A pack adds no registry, scheduler, router, executor, receipt type, store, or CAS. Design: `docs/superpowers/specs/2026-10-31-capability-pack-v1-design.md`.

Invariants:

- **MODEL OUTPUT != TRUTH**
- **EXECUTION COMPLETE != SEMANTIC ACCEPTANCE**
- **PROVIDER IS NOT AUTHORITY**
- **DISCOVERED != VALIDATED**
- **PACK INSTALLED != AUTHORIZED**
- **PACK EXECUTION SUCCESS != SEMANTIC ACCEPTANCE**

An optional provider being unavailable does not block the platform. REA (capability discovery) and OpenMAIC (adaptive learning) are optional external runtimes behind `pados.capability_loop`. When one is absent, unhealthy, incompatible, or returns malformed data, the dependent capability reports unavailable or degraded, fails closed, and leaves PADOS state unchanged.

## Implementation State

The P7 Authority Baseline is closed. The canonical baseline is `main@e550141` (PR #16, post-merge authorization and runtime hardening; PR #8 delivered the P7 capability discovery and learning loop). The authority layer is frozen: `TaskEnvelope`, the scheduler, `CapabilityRegistry`, `CapabilityExecutor`, the router, policy, authorization and approval, execution receipts, verification, `ResultCandidate`, CAS semantic acceptance, audit, state and memory, and the P7 session-binding authority. Core is not expanded further.

The current phase is Capability Expansion: capabilities become governed, replaceable, measurable extensions around the frozen authority layer. The first deliverables are Capability Pack V1 and the Structured Problem Solving reference pack (8D as the first profile). AI Scientist (draft PR #17) follows as a Pack V1 consumer for `RESEARCH_CAMPAIGN`; its real runtime is not yet validated. The REA and OpenMAIC adapters are contract- and failure-tested against fakes in CI; neither has been validated against a live runtime (real-runtime gates: `NOT_VALIDATED`).

The frozen common-code foundation and Capability Fabric V1 have accepted cross-platform evidence. Product-completion protocol contracts and deterministic tests are being integrated with Remote Operations and the PWA. Real multi-resource assignment, residual dispatch, complete receipt lineage, and final Windows/VMware evidence must be confirmed before declaring controlled product dogfood ready.
