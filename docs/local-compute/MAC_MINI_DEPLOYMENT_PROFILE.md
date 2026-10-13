# Mac mini deployment profile: `PADOS-LCN-01` (DEPLOYMENT_READY_HARDWARE_PENDING)

A future Apple Silicon node. Nothing here assumes exact RAM, chip generation or model; those are fixed only after the hardware exists and is measured.

## Identity and registration

1. Install PADOS (the same package) on the Mac mini. Run `pados node serve --config node.json`.
2. The node creates its persisted identity (`lcn-…`).
3. PADOS registers it explicitly in `PADOS_COMPUTE_NODES_CONFIG` with:
   - `node_id`
   - a private LAN endpoint
   - `auth_ref: secretref://…`
   - `authorized: true` (a human decision)
   - `allowed_capabilities`
   - `max_classification`
   - `project_ids`
4. `pados node probe` must show `ONLINE` before any routing.

## Candidate capabilities

| Capability | Mechanism |
|---|---|
| LOCAL_INFERENCE | MLX / Ollama / llama.cpp behind the OpenAI-compatible backend (or the native Ollama adapter) |
| EMBEDDING | `/v1/embeddings` or `/api/embed` |
| RERANK | Future node capability (the Enterprise reranker protocol would call a node job) |
| BATCH_INFERENCE | `pados.compute-node.batch.v1` batch Work Contracts |
| OCR_POSTPROCESS | Future `DOCUMENT_EXTRACTION` / `OCR` node adapters |
| EVALUATION | Batch evaluation jobs whose outputs are artifacts, never truth |
| NIGHTLY_SYNTHESIS | A governed batch Work Contract dispatched by a human or existing PADOS flow (no node-side scheduler) |

V1 implements LOCAL_INFERENCE, EMBEDDING and CLASSIFICATION on nodes; the others are profile entries only.

## Candidate runtimes

| Runtime | How the node sees it |
|---|---|
| MLX (`mlx_openai_compatible`) | An OpenAI-compatible server exposing MLX models (`MLX_ADAPTER_CONTRACT_READY`) |
| Ollama | Native adapter, or `/v1` through the generic backend |
| llama.cpp server | `openai_compatible` |

## Network and security

- Bind to the Mac's private LAN address with `allow_lan: true`, or to loopback behind a host-to-host link.
- The physical link (Wi-Fi, Ethernet, Thunderbolt/USB4 bridge) does not matter to PADOS.
- Never expose the node publicly.
- Credentials exist only as SecretRefs (for example `secretref://env/PADOS_NODE_CREDENTIAL` in the launch environment or a keychain-backed resolver in future).

## Acceptance when hardware arrives

1. `pados node probe` reports ONLINE with an MLX or Ollama model `AVAILABLE`.
2. `runtime/artifacts/reports/local-compute-node-v1/scripts/node_protocol_uat.py` passes on the Mac (using its own discovery).
3. Run the vertical slice with the Mac registration and record measured latency and memory.
4. Declare `accelerators: ["APPLE_SILICON"]` and a `resource_class` only from measured evidence.
