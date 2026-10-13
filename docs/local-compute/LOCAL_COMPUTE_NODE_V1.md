# PADOS Local Compute Node V1

A Local Compute Node is a replaceable execution substrate underneath PADOS. It runs bounded jobs and returns immutable, hashed artifacts. It is **not** any of the following authorities: semantic, project, memory, audit, scheduler or provider router. PADOS remains the control plane.

```text
Human Intent → PADOS Project Context (+ enterprise evidence) → Governed Work Package
→ Provider Router (existing Worker Router) → LocalInferenceProvider → Local Compute Node
→ Execution → artifact (SHA-256) → PADOS independent verification → ExecutionReceipt
→ ResultReceipt candidate → Validation → ScopeVersion → CAS → Audit
```

Node execution never equals semantic acceptance. The maker writes a node artifact into the approved workspace only after it has been verified independently. The Core validator then re-verifies the ExecutionReceipt against the workspace bytes, and only CAS advances state. Vertical-slice step 8 shows that tampering after verification is rejected.

## Reused Enterprise Pack contracts (no duplicates)

| Enterprise contract | Reuse |
|---|---|
| `enterprise.compute.ComputeNode` | `NodeDescriptor.as_compute_node()`. `ComputeNode.eligible_for` is a routing gate |
| `enterprise.compute.ProviderCapabilityProfile` | Per (node, model) profile; `permits(classification)` is a routing gate |
| `enterprise.compute.LocalInferenceProvider` (protocol) | `NodeModelBinding` implements it (tested by structural typing) |
| `enterprise.classification` | Node, model, registration and request ceilings; `lowest()` |
| `enterprise.secrets` | Node credential and backend credentials are SecretRefs; `SecretBoundary`, `redact` |
| `enterprise.observability` | New allowlisted events `node.probe` and `node.selected`, plus attributes `node_id`, `model_id`, `node_state`, `queue_delay_ms`, `job_id` (additive). No content |
| `enterprise.manifest.CapabilityHealth` | `NodeState.health` mapping |
| Enterprise evidence context | `LocalComputeNodeMaker` derives the work classification from the contract's `enterprise_evidence` |
| `enterprise.visibility.EXECUTION_CLASS` | `PADOS_LOCAL_INFERENCE → LOCAL` (additive) |

## Package `pados.compute_node`

| Module | Side | Stage | Contract |
|---|---|---|---|
| `manifest.py` | both | 2, 3, 9, 10 | `NodeDescriptor`, `NodeState` (ONLINE/DEGRADED/BUSY/OFFLINE/UNAUTHORIZED), `ModelManifest` (canonical `backend/model@version`), `NodeCapability`, `NodeLimits`, `NodeLoad` |
| `identity.py` | both | 2, 5 | Persisted identity key (0600) giving a stable `lcn-…` id; HMAC identity proof over a PADOS nonce; record MAC |
| `config.py` | node | 4, 17 | `NodeConfig`; bind only to loopback, or to a private address with `allow_lan`; wildcard and public binds are refused |
| `backends.py` | node | 6–8 | `OpenAICompatibleBackend` (generic), `OllamaBackend`, `DeterministicBackend`, `mlx_openai_compatible` (contract) |
| `jobs.py` | node | 11, 16 | `JobManager`: QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/TIMED_OUT, concurrency, queue depth, timeout, cancellation, input/output/batch budgets |
| `artifacts.py` | node | 15 | Content-addressed, read-only artifact store with an integrity check on read |
| `service.py` | node | 4 | FastAPI node service (`pados node serve`) |
| `registry.py` | PADOS | 5, 3 | `NodeRegistration`, `NodeRegistry`, `NodeClient`, `NodeProber` (state derived from probe evidence) |
| `provider.py` | PADOS | 6, 12, 15 | `LocalInferenceProvider` (router-backed routing, execution, independent verification), `NodeModelBinding` |
| `receipts.py` | PADOS | 14 | `NodeExecutionReceipt`, bound to the Core ExecutionReceipt id; `ReceiptLedger` (execution evidence, not audit) |
| `maker.py` | PADOS | 24, 26 | `LocalComputeNodeMaker` (product-loop Direct Maker); bounded batch spec `pados.compute-node.batch.v1` |
| `status.py` | PADOS | 19 | Doctor section `compute_nodes` |
| `visibility.py` | PADOS | 20 | Mission Control `compute_nodes` block (read-only) |

## Core touch points (additive, optional)

- `Settings.compute_nodes_config` (`PADOS_COMPUTE_NODES_CONFIG`, default empty).
- `pados doctor`: `compute_nodes` section plus one text line; `ok` and the exit code are unchanged.
- `pados node serve` and `pados node probe` commands.
- `mission_control()["compute_nodes"]` and the web `ComputeNodesPanel`.

No Worker Router, schema, CAS, audit, memory or migration change.

## Routing through the existing Provider Router

1. **Registration gates.** Explicit registration, then `authorized`, then project scope (empty means none; `"*"` means all), then allowed capability, then `max_classification`.
2. **Evidence gate.** A fresh probe verifies the identity proof, then the node state must be `ONLINE`. BUSY, DEGRADED, OFFLINE and UNAUTHORIZED are never routed.
3. **Contract gates.** `ComputeNode.eligible_for(classification, capability)` and `lowest(registration, node)` ceiling.
4. **Model gates.** Availability `AVAILABLE` (derived from backend discovery and digest), task match, registration model allowlist, context limit, and `ProviderCapabilityProfile.permits`.
5. **Router.** Eligible (node, model) pairs go to `pados.worker.router.select_worker(..., allowed_worker_types={WorkerType.OLLAMA})`. `OLLAMA` is the router's existing *local-model worker class*. Using it avoids a router schema change; the node's own capability is enforced by gates 1–4.

RESTRICTED work routes only to a registration explicitly set to `max_classification: RESTRICTED`, on a node and model that also declare RESTRICTED.

## Status

- **Local Compute Node V1:** see `runtime/artifacts/reports/local-compute-node-v1/ACCEPTANCE_REPORT.md`.
- **MLX:** `MLX_ADAPTER_CONTRACT_READY`. There is no Apple Silicon environment, so it was not live-validated.
- **Mac mini:** `DEPLOYMENT_READY_HARDWARE_PENDING`.
- **Windows training node:** `CONTRACT_READY`.
