# PADOS Roadmap

## P7 Authority Baseline: CLOSED

Canonical baseline: `main@e550141` (PR #16, post-merge authorization and runtime hardening; PR #8 delivered the P7
capability discovery and learning loop). The authority layer is frozen: `TaskEnvelope`, scheduler,
`CapabilityRegistry`, `CapabilityExecutor`, router, policy, authorization and approval, execution receipts,
verification, `ResultCandidate`, CAS semantic acceptance, audit, state and memory, and the P7 session-binding
authority. Core is not expanded further.

## Capability Expansion: CURRENT PHASE

Capabilities become governed, replaceable, measurable extensions around the frozen authority layer.

- Capability Pack V1 (schema `pados.capability.pack.v1`): a versioned packaging and registration convention over the
  existing Capability Fabric, not a new authority
  (`docs/superpowers/specs/2026-10-31-capability-pack-v1-design.md`)
- Structured Problem Solving reference pack, with 8D as the first profile
- Next: AI Scientist (draft PR #17) as a Pack V1 consumer for `RESEARCH_CAMPAIGN`; real runtime not yet validated

Invariants: MODEL OUTPUT != TRUTH; EXECUTION COMPLETE != SEMANTIC ACCEPTANCE; PROVIDER IS NOT AUTHORITY;
DISCOVERED != VALIDATED; PACK INSTALLED != AUTHORIZED; PACK EXECUTION SUCCESS != SEMANTIC ACCEPTANCE. A capability
pack is not an authority. An optional provider being unavailable (for example the REA or OpenMAIC external runtime)
degrades only the capability that depends on it, fail-closed; it does not block the platform.

## Product Completion V1

Close cross-platform validation for task protocols, residual completion, Work Graph projection, existing-scheduler integration, authenticated mobile intents, recommendation governance, and the deterministic end-to-end loop. Keep the accepted Core, policy, CAS, audit, memory, and Capability Fabric foundations frozen.

## Controlled Dogfood

Begin only after critical and high findings are closed, the task-to-acceptance path is validated on Windows and VMware Linux, mobile remote intent behavior is verified, and repository/publication boundaries are reviewed.

Separately, local dogfood is a CONDITIONAL GO at the immutable tag `PADOS_RC2_LOCAL_DOGFOOD_V1`, for local,
supervised, non-sensitive use only (`docs/releases/PADOS_RC2_LOCAL_DOGFOOD_V1/DOGFOOD_BOUNDARY.md`).

## Capability and Runtime Expansion

- Local intelligence stack and measured model benchmarks (Stage B Local Intelligence V1: ACCEPTED; see
  `docs/architecture/MEASURED_CAPABILITY_EXECUTION_CONTRACT.md`)
- Storage Flexibility V1: small authoritative state locally, large assets by reference
  (`docs/architecture/STORAGE_FLEXIBILITY_V1.md`)
- Enterprise interop: the Enterprise Local Runtime Adapter pattern
  (`docs/architecture/ENTERPRISE_LOCAL_RUNTIME_INTEROP.md`, `pados.local_intelligence.runtime_evidence`) and
  the Governed Outcome Learning Adapter pattern (`docs/architecture/GOVERNED_OUTCOME_LEARNING_INTEROP.md`,
  `pados.learning.external_outcomes`). Generic reference adapters are in place. Deployment-specific connectors
  stay with their owners.
- Live OCR and Vision engines
- Embedding and reranker capabilities
- Capability Packs (Pack V1 is the current phase; see above) and deterministic algorithm integrations
- Governed Skills and MCP registration and authorization
- Office and engineering adapters, including CAD capability evaluation

Large models and optional OCR/Vision engines are runtime expansion work; they are not prerequisites for deterministic product-contract validation.

## Usage-Driven Product Work

- Refine Work Map navigation, topology, and mobile task controls from dogfood evidence.
- Add Mac and additional Home Base environments after platform-specific validation.
- Expand live network, hotspot, and intermittent-connectivity UAT.

## Pending Evidence

- Live OCR backend
- Herdr live UAT
- Physical hotspot/cellular UAT
- Local model benchmark: done on the Windows reference node (Stage B); other devices pending
- Real REA and OpenMAIC runtime validation: `NOT_VALIDATED`; the adapters are contract- and failure-tested against
  fakes only
- AI Scientist real runtime validation
- Public redistribution rights and license decision
