# PADOS Enterprise Capability Pack V1

The pack turns the enterprise-AI capabilities proven in earlier private projects into reusable, product-neutral PADOS extensions. PADOS Core stays frozen.

The pack is a capability provider: it produces evidence and candidate artifacts. It never owns or advances PADOS semantic state.

```text
Human Intent → PADOS Context (+ enterprise evidence as data) → Governed Work Package
→ Enterprise Capability / Provider → ExecutionReceipt → Artifact verification
→ ResultReceipt candidate → Validation → ScopeVersion check → CAS → Semantic State → Immutable Audit
```

Baseline: WSL `0d4ea1a`, Windows `b6c36d6`, shared tree `bcbdea3e`.
Branch: `codex/pados-enterprise-capability-pack-v1`.

## Stage 0: inventory of existing PADOS contracts (no duplicates)

| Concern | Existing PADOS implementation | Pack usage |
|---|---|---|
| Provider Router | `pados.worker.router.select_worker`, `pados.product_loop.provider_selection` | Unchanged. `compute.profile_for_worker` only *describes* workers; there is no routing |
| Skills/MCP | `pados.execution.authorization.authorize_execution_capabilities` (skills, MCP, SecretRef grants) | Enterprise authorization calls it directly. MCP tools map to `mcp_capabilities` |
| SecretRef | `secretref://` grammar in `pados.execution.authorization` | `enterprise.secrets` reuses the same validator and adds only the resolution boundary and redaction |
| Authorization | Project-bound grant (`PADOS_PROJECT_GRANT`), product-loop control auth | Remains the only authority. Enterprise ACL facts are request inputs, not a store |
| ExecutionReceipt | `pados.execution.receipts` | Unchanged. Enterprise evidence is bound through the Work Contract context fingerprint |
| ResultReceipt / CAS / audit | `pados.state_proof` (proof lane), `P6_19BStateGateway` | Unchanged. The enterprise package imports none of it (tested) |
| Context | `pados.execution.context.ProjectContextLoader` | Additive optional `ProductLoopService(context_extensions=...)`. It is empty by default, and the Core context is byte-identical (tested) |
| Memory | `pados.memory` (operational memory authority, `20260930_0014`) | Not touched. Enterprise evidence is never promoted to memory |
| Project identity | `projects` / `context_project_registry` | Enterprise sources bind to the PADOS project id |
| Source identity | `pados.knowledge.identity` (`SourceIdentity`, content-hash id) | Reused as the physical identity of every enterprise source |
| Doctor/readiness | `pados doctor`, `pados.execution.readiness`, `operations.system_health` | Extended with a separate `enterprise` section; `ok`/exit code unchanged |
| Observability | `pados.logging_config.get_logger` (structlog JSON) | `enterprise.observability` emits allowlisted, content-free events through it |
| Artifacts | `artifact_handoff`, `ObjectStore` (content-addressed) | Unchanged. Mission Control reads hash-verified run-context objects |
| Chunking / parsing / embedding | `pados.corpus.ingestion.chunk_text_pages`, `deterministic_embedding`, `pados.corpus.pdf.parse_pdf` | Reused as-is by the pipeline |
| Classification vocabulary | Free-text `classification`, default `'internal'` (migration 0003) | New ordered `DataClassification`. It parses the existing lowercase value |

The donor inventory used only an authorized read-only mirror; the internal extraction matrix is excluded from public export. Only generic concepts were re-implemented (see THIRD_PARTY_PROVENANCE.md).

## Package `pados.enterprise`

| Module | Stage | Contract |
|---|---|---|
| `manifest.py` | 2 | `EnterpriseCapabilityManifest`, `CapabilityType` (9 types), `ExecutionMode`, `CapabilityHealth`, `ArtifactContract(semantic_authority="NONE")` |
| `registry.py` | 2, 6 | `CapabilityRegistry`: installed ≠ authorized; health comes from the implementation probe |
| `classification.py` | 5 | `PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED`; unknown values fail closed |
| `authorization.py` | 6 | `authorize_capability` (adapter over the PADOS grant), `RetrievalScope` (coarse), `presentation_filter` (fine), `assert_no_withheld_content` |
| `lineage.py` | 4, 13 | `EnterpriseSource`, `LineageGraph`, dry-run `plan_revocation`, projection-mask `apply_revocation` |
| `extraction.py` | 9 | `DocumentExtractor` protocol; text, PDF text-layer (existing parser), and unconfigured OCR (fails closed) |
| `retrieval.py` | 3, 7 | `RetrievalRequest`/`RetrievalResponse`/`EvidenceItem`; exact, vector and graph backend protocols; in-process implementations |
| `rerank.py` | 8 | `Reranker` protocol; no-op, deterministic lexical, and generic HTTP rerankers; modes DISABLED/SHADOW/ACTIVE |
| `budget.py` | 14 | `EvidenceBudget` (sources, chunks, tokens, restricted items); `assert_within_budget` runs before invocation |
| `observability.py` | 15 | Allowlisted scalar events, `pados.*` attribute names (OTel-mappable), no content |
| `pipeline.py` | 3 | `KnowledgePipeline` (ingest → … → bounded evidence package), `local_pipeline()` |
| `context.py` | 3, 17 | `EnterpriseEvidenceContext` product-loop extension |
| `compute.py` | 10, 11 | `ProviderCapabilityProfile`, `ComputeNode`, `LocalInferenceProvider` protocol (no implementation) |
| `secrets.py` | 12 | `SecretBoundary`, `SecretValue` (never renders), `EnvironmentSecretResolver`, `redact` |
| `status.py` | 16 | `enterprise_status` for the existing doctor (`PADOS_ENTERPRISE_CONFIG`) |
| `visibility.py` | 18 | Read-only Mission Control projection |

## Core touch points (all additive and optional)

The brief allows "extension-aware optional handling". The Core touch points are:

1. `Settings.enterprise_config` (`PADOS_ENTERPRISE_CONFIG`, default empty).
2. `ProductLoopService(context_extensions=())`. It is resolved before a run id exists, so a denial is `CONTEXT_EXTENSION_UNAUTHORIZED` with no durable effect.
3. `pados doctor`: an `enterprise` section plus one text line. `ok` and the exit code are unchanged.
4. `mission_control()`: an `enterprise` key; `system_health` is unchanged.
5. Web: an optional `enterprise` type and `EnterprisePanel` in Mission Control.

No migration, CAS, audit, memory or provider-router code changed.

## Configuration (optional)

```json
{"schema_version": "pados.enterprise.config.v1",
 "authorized_secret_refs": ["secretref://env/RERANK_TOKEN"],
 "allow_remote_egress": false,
 "capabilities": [{"capability_id": "knowledge.local", "capability_type": "KNOWLEDGE_RETRIEVAL", "...": "...",
                   "implementation": "pados.enterprise.pipeline:local_pipeline"}]}
```

Implementations can only be referenced inside `pados.enterprise.*`; configuration cannot import arbitrary code.

## Evidence

See `runtime/artifacts/reports/enterprise-capability-pack-v1/`: `ACCEPTANCE_REPORT.md`, `GATES.json`, `VERTICAL_SLICE.json`, `CORE_REGRESSION.json` and `FINAL_STATUS.json`.
