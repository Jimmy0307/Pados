# PADOS node protocol (`pados.compute-node.protocol.v1`)

**Transport.** HTTP/JSON on a private, trusted network: loopback, home/office LAN, or a direct host-to-host link. The physical medium (Ethernet, Wi-Fi, USB4, Thunderbolt) is irrelevant. The protocol knows only endpoint, identity, authorization, health, latency and capability.

**Not for the public Internet.** Nodes refuse wildcard and public binds, and PADOS refuses non-private node endpoints and endpoints with embedded credentials.

## Authentication and identity

- **Credential.** Every request carries `Authorization: Bearer <node credential>`. Both sides hold the credential only as a SecretRef (`secretref://…`), resolved inside the call. Unauthenticated requests get `401 NODE_UNAUTHENTICATED`.
- **Identity.** `node_id = "lcn-" + sha256("pados-lcn-identity\n" + identity_key)[:20]`. The 32-byte identity key is generated once and persisted with mode 0600 in the node's state dir, so the id is stable across restarts and independent of hostname or IP.
- **Proof.** `/health` and `/capabilities` accept `?nonce=`. The node returns `identity_proof = HMAC-SHA256(credential, "pados-node-identity\n{node_id}\n{nonce}")`. PADOS requires `node_id == registration.node_id` **and** a valid proof; a reachable host with the right name is not enough.
- **Job records.** Each record carries `record_mac = HMAC-SHA256(credential, "pados-node-record\n" + canonical(record without record_mac))`.
- **Future.** Asymmetric node keys (Ed25519) are planned; the `cryptography` package is not a current PADOS dependency.

## Endpoints

| Method and path | Response |
|---|---|
| `GET /health?nonce=` | `node_id`, `protocol`, `runtime_version`, `state`, `reasons`, `load`, `last_seen`, `manifest_hash`, `identity_proof` |
| `GET /capabilities?nonce=` | `descriptor` (NodeDescriptor), `manifest_hash`, `identity_proof` |
| `GET /models` | Declared model manifests with availability from live backend discovery |
| `POST /execute` | `202` job record. Rejections: `403 CAPABILITY_NOT_ENABLED`, `403 CLASSIFICATION_ABOVE_NODE_CEILING` / `403 CLASSIFICATION_ABOVE_MODEL_CEILING`, `404 MODEL_NOT_ADVERTISED`, `409 MODEL_<availability>`, `400 MODEL_CAPABILITY_MISMATCH`, `400 TIMEOUT_ABOVE_NODE_LIMIT`, `413 INPUT_BUDGET_EXCEEDED` / `413 OUTPUT_BUDGET_EXCEEDED` / `413 BATCH_TOO_LARGE`, `409 REQUEST_ID_CONFLICT` (a different request reusing an admitted `request_id`; an identical request replays the admitted job without a new reservation), `429 QUEUE_FULL` |
| `GET /jobs/{id}` | Job record |
| `POST /jobs/{id}/cancel` | Job record, state `CANCELLED` unless already terminal |
| `GET /artifacts/{sha256:…}` | Bytes plus `X-PADOS-Artifact-SHA256`. `404` if missing; `409 ARTIFACT_INTEGRITY_FAILED` if the stored bytes no longer match |

**Execute request.** Fields:

- `request_id` (idempotent), `work_package_id`, `run_id`
- `capability`, `model_id` (canonical), `classification`
- `inputs[]`: each item is `{messages}` or `{text}`, plus `labels` for classification
- `output_path`, `max_output_tokens`, `timeout_seconds`

**Job record (content-free).** Fields:

- identity: `job_id`, `node_id`, `request_id`, `work_package_id`, `run_id`
- request: `capability`, `model_id`, `classification`, `input_sha256`, `input_items`, `output_path`
- state and timing: `state`, `created_at`/`started_at`/`finished_at`, `queue_delay_ms`, `inference_ms`, `error_code`
- results: `artifacts[{artifact_id, sha256, size_bytes, media_type, path}]`, `runtime{backend, backend_model, usage…}`
- binding: `manifest_hash`, `node_runtime_version`, `record_mac`

## Job lifecycle

`QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLED | TIMED_OUT`

- A timeout or cancel that arrives while a backend call is in flight discards the late result.
- This is local execution state only; there is no project or task model on the node.

## PADOS verification (all fail closed)

| Failure | Code |
|---|---|
| Record MAC invalid, or request binding (ids, model, capability, classification, path, input hash) mismatch | `NODE_RECEIPT_INVALID` |
| Manifest changed between routing and execution | `NODE_MANIFEST_STALE` |
| No artifact / more than one artifact | `ARTIFACT_MISSING` / `ARTIFACT_UNEXPECTED` |
| Artifact path not the declared, allow-listed path | `ARTIFACT_PATH_UNAUTHORIZED` |
| Fetched bytes do not match the recorded SHA-256 or size | `ARTIFACT_HASH_MISMATCH` |
| Node store integrity failure | `ARTIFACT_TAMPERED` |
| Job did not succeed | `JOB_<STATE>:<error_code>` |
| Deadline exceeded (PADOS then cancels the job) | `JOB_TIMED_OUT` |
| Node unreachable | `NODE_OFFLINE` / `NODE_TIMEOUT` |

## Node state (derived by PADOS from probe evidence)

| State | Evidence |
|---|---|
| OFFLINE | Connect error or timeout |
| UNAUTHORIZED | 401, identity mismatch, invalid proof, unresolvable SecretRef, or registration not authorized |
| DEGRADED | Invalid manifest, backend unavailable, model missing or digest mismatch, or a capability without an available model |
| BUSY | Running and queue both at capacity (slot occupancy: a job holds its slot from admission until its worker call returns, including a cancelled or timed-out job whose backend call is still running) |
| ONLINE | Otherwise |

Probe evidence is persisted in `<registry.state_dir>/probes/<node_id>.json`. Health is never set by hand.
