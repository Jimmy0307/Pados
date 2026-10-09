# PADOS Roadmap

## Product Completion V1

Close cross-platform validation for task protocols, residual completion, Work Graph projection, existing-scheduler integration, authenticated mobile intents, recommendation governance, and the deterministic end-to-end loop. Keep the accepted Core, policy, CAS, audit, memory, and Capability Fabric foundations frozen.

## Controlled Dogfood

Begin only after critical and high findings are closed, the task-to-acceptance path is validated on Windows and VMware Linux, mobile remote intent behavior is verified, and repository/publication boundaries are reviewed.

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
- Capability Packs and deterministic algorithm integrations
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
- Public redistribution rights and license decision
