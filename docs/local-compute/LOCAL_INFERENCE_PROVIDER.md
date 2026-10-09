# LocalInferenceProvider

`pados.compute_node.provider.LocalInferenceProvider` routes to and executes on registered local compute nodes, **through the existing PADOS Worker Router**. It is not a second router: see "Routing through the existing Provider Router" in LOCAL_COMPUTE_NODE_V1.md.

## Backends (node side)

| Kind | Interface | Endpoints | Notes |
|---|---|---|---|
| `openai_compatible` | **Generic V1 transport** | `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/embeddings` | Works with llama.cpp server, vLLM, MLX OpenAI-compatible servers, and Ollama's `/v1` |
| `ollama` | Optional native adapter | `/api/version`, `/api/tags`, `/api/chat`, `/api/embed` | Digest, family and quantization come from discovery; `404` / "model … not found" maps to `MODEL_NOT_FOUND` |
| `mlx_openai_compatible` | Contract (`MLX_ADAPTER_CONTRACT_READY`) | Same as OpenAI-compatible | A future Apple Silicon node serves MLX models via an OpenAI-compatible server; not live-validated |
| `deterministic` | Development and test | none | No weights or network; LOCAL_INFERENCE / EMBEDDING (PADOS deterministic embedding) / CLASSIFICATION (keyword labels) |

**Request defaults.** Temperature is 0 and streaming is off.

**Fail-closed backend errors.**

- `MODEL_NOT_FOUND`, `BACKEND_TIMEOUT`
- `ENDPOINT_FAILURE:<ExceptionClass>`, `BACKEND_HTTP_<n>`, `BACKEND_SCHEMA_INVALID`
- `EMPTY_COMPLETION`: for example, a reasoning model that spent its whole budget before answering (observed during live UAT)

Backend response text is never propagated.

## Model identity

- Canonical: `model_id = <backend_id>/<backend_model>@<digest|version>`.
- Aliases are labels only and cannot be identity.
- Availability is derived from live discovery: `AVAILABLE`, `MISSING`, `DIGEST_MISMATCH` (declared digest ≠ discovered), `BACKEND_UNAVAILABLE`, or `UNVERIFIED`.
- No vendor or model name is hard-coded. The live UAT picks the smallest installed model by discovery.
- A model id longer than a provider id allows (for example one pinned to a full manifest digest) is referenced in
  the router candidate as `lcn:<node>:m-<sha256 of the model id, 32 hex>`; the route keeps the full manifest.

## Model authorization

- A registration authorizes only the model ids it names in `allowed_models`. An empty list authorizes **no**
  model; it is never a wildcard (Stage B M4.1 owner decision: strict deny-by-default).

## Measured execution gate (Stage B M4.1, optional)

With a `MeasuredExecutionGate`, the provider routes only to models that carry a REGISTERED measured
`ModelCapabilityProfile` in the existing `ModelCatalog` (matched by manifest digest), whose immutable model
reference the current registration names, and whose live runtime binding (runtime, device, freshness, resource
headroom) and measured evidence satisfy the operator's `CapabilityRequirement` for the node capability.

- Before the job is submitted, `execute` re-derives the admission from current authority (catalog, registrations,
  node probe, live binding, requirement). Any difference refuses with `EXECUTION_GATE_REFUSED:<code>`, and nothing
  reaches the node.
- After the job finished, the same check runs again. Any difference withholds the output with
  `REVOKED_DURING_EXECUTION`. A job already running is **not** cancelled by a revocation; only its output is
  withheld.
- The gate reads authority and never writes it. Selection stays with the Worker Router.

## Execution result

`NodeExecution(route, record, artifact_bytes, receipt)`. The node receipt (`pados.compute-node.execution-receipt.v1`) carries:

- node_id, provider_id (`PADOS_LOCAL_INFERENCE`), model_id, capability
- work_package_id, run_id, job_id
- started_at, finished_at, status, classification
- input_sha256
- artifact hashes
- runtime (backend, token usage, queue_delay_ms, inference_ms, manifest_hash, node runtime version)
- `credential_ref` (SecretRef string only)
- `execution_receipt_id` (bound to the Core ExecutionReceipt by the maker)
- verification results, `semantic_authority: false`, `receipt_hash`

It carries no prompt or output text.

## Product-loop integration

`LocalComputeNodeMaker(approved_root, provider)` is a Direct Maker for `ProductLoopService`. It:

1. Requires exactly one declared artifact.
2. Takes the work classification as the maximum of the floor (INTERNAL) and the classifications of the contract's enterprise evidence.
3. Routes, executes and verifies.
4. Writes the artifact through SafeWorkspaceFS and re-reads its hash.
5. Returns a Core `ExecutionReceipt` (`provider_id=PADOS_LOCAL_INFERENCE`; `evidence_kind` is `DETERMINISTIC_HOST` for the deterministic backend and `LIVE_PROVIDER` otherwise).

Routing and authorization failures are `AUTHORITY_BLOCK` before any node job. Artifact and receipt failures are `EVIDENCE_GAP`.

**Batch work (Stage 24).** A declared artifact whose draft content is `{"schema": "pados.compute-node.batch.v1", "capability": …, "items": [...]}` (at most 64 items; the node limit also applies) runs as **one** bounded node job inside the same governed Work Contract. The output is JSONL. Examples include embedding refresh, classification, rerank evaluation, corpus enrichment, nightly synthesis and evaluation. No cron and no scheduler: batches run only when a governed Work Contract is dispatched.
