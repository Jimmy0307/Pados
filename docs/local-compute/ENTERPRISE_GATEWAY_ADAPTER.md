# Enterprise HTTP AI Gateway Adapter

Status: **ADAPTER_READY** · **SIMULATED_E2E_PASS** · **ENTERPRISE_LIVE_NOT_VALIDATED**.

| Status | Meaning | Evidence |
|---|---|---|
| `ADAPTER_READY` | The adapter, its contract schema, failure taxonomy, retry, circuit, receipts and data policy are implemented and covered by contract tests against a simulated gateway over real loopback HTTP and HTTPS. | `tests/compute_node/test_gateway_contract.py`, `tests/compute_node/test_gateway_adapter.py` |
| `SIMULATED_E2E_PASS` | The E2E ran as real processes (simulated gateway, harness, `pados serve`, browser) and every step passed. | `scripts/dogfood/gateway_simulated_e2e.py`, `web/e2e/providers-live.spec.ts` |
| `ENTERPRISE_LIVE_NOT_VALIDATED` | No real enterprise gateway has been called. The owner runs [ENTERPRISE_GATEWAY_LIVE_ACCEPTANCE.md](ENTERPRISE_GATEWAY_LIVE_ACCEPTANCE.md) after VPN access exists. | none yet |

Code: `src/pados/compute_node/gateway_contract.py` (contract), `src/pados/compute_node/gateway.py` (backend),
`src/pados/compute_node/circuit.py` (circuit breaker), `src/pados/providers/setup.py`,
`src/pados/providers/diagnostics.py` and `src/pados/providers/routes.py` (Control Center setup and diagnostics).

## Place in the architecture

The gateway is one more compute-node backend kind, `enterprise_gateway`, next to `ollama`, `llama_cpp_native` and
`openai_compatible`. It implements the existing `InferenceBackend` protocol (`health`, `discover`, `run`). It adds no
model catalog, router, scheduler or capability authority:

```
CAPABILITY = MODEL ARTIFACT x RUNTIME x DEVICE x EXECUTION CONFIGURATION x EVIDENCE
```

A gateway model is **unmeasured** until a measured profile exists for it in the operator `ModelCatalog`. Its output is
labelled `NON_AUTHORITATIVE_OBSERVATION` (`runtime.authority`, `runtime.measured = false`), it never reaches the CAS
and it never counts as accepted (R19-6). Authorization stays with the compute-node registration
(`PADOS_COMPUTE_NODES_CONFIG`). "Reachable" and "authorized, measured and executable" are separate states and the
Providers page shows them separately.

A measured execution configuration is **refused** by this adapter (`EXECUTION_CONFIG_UNVERIFIABLE:context_window`):
a gateway's application of context window, reasoning mode or sampling cannot be confirmed from its answer, so a
measured route can never be satisfied by a gateway call.

## The contract (`pados.compute-node.gateway-contract.v1`)

The contract is a **private deployment file** (JSON, `extra=forbid`). It is never committed. A node references it by
absolute path (`BackendConfig.contract_path`, kind `enterprise_gateway`, no `endpoint`, no `credential_ref`), and the
Control Center references it from the owner's provider setup.

| Field | Rule |
|---|---|
| `base_url` | `https://` required; `http://` only on loopback. No user info, query or fragment. |
| `tls_ca_file` | Optional absolute path to a private CA bundle. TLS verification is never disabled. |
| `auth` | `scheme`: `bearer`, `header` (with `header_name`) or `none`. `auth_ref` is a reference only, `secretref://env/<NAME>`. The value is resolved inside one call and never stored, logged or returned. |
| `health` | `GET` route and expected status. |
| `discovery` | `GET` route; `models_path` (JSON path to the model list), `id_field`, optional `digest_field`, optional `state_field` with `loaded_values`. |
| `chat` | `POST` route; `request_template` whose string values `{{model}}`, `{{messages}}`, `{{prompt}}`, `{{max_output_tokens}}`, `{{temperature}}` are filled type-preserving (closed set; anything else is refused); `text_path`, optional `model_path` (answer identity), `finish_reason_path`, usage paths, `error_code_path` with `error_codes` mapped to four closed failures. |
| `timeouts` | `connect_seconds` (≤ 30), `read_seconds` (≤ 600), `probe_seconds` (≤ 30). |
| `retry` | `max_attempts` (≤ 5), base and maximum delay, `max_retry_after_seconds`. |
| `circuit` | `failure_threshold`, `open_seconds`. |
| `rate_limit_headers` | `retry_after`, optional `remaining` and `reset` header names. |
| `idempotency` | `header_name` and `honoured`. `honoured: true` is the gateway owner's declaration that a repeated key has no second side effect. |
| `max_context_tokens` | Upper bound checked before a call. |
| `policy` | `max_classification` (default `PUBLIC`; `RESTRICTED` is refused) and `execution_enabled` (default `false`: health and discovery only). |

JSON paths are a small closed language: `a.b`, `a[0]`, `a[*].id`. Any credential-looking value anywhere in the file
refuses the whole contract (`GATEWAY_CONTRACT_CONTAINS_SECRET`).

## Failure taxonomy (closed codes)

| Code | Cause | Write outcome | Counts against the circuit | Retried (reads / keyed chat) |
|---|---|---|---|---|
| `GATEWAY_NETWORK_UNREACHABLE` | connect refused or connect timeout (VPN or private network down) | NO | yes | yes |
| `GATEWAY_DNS_FAILED` | host name not resolvable | NO | yes | yes |
| `GATEWAY_TLS_FAILED` | handshake or certificate error | NO | yes | no |
| `GATEWAY_AUTH_FAILED` | 401 / 403 or a mapped code | NO | no | no |
| `GATEWAY_AUTH_NOT_CONFIGURED` | the referenced environment variable is not set; nothing is sent | NO | no | no |
| `GATEWAY_MODEL_NOT_LOADED` | mapped error code (for example a cold model) | NO | no | yes |
| `GATEWAY_MODEL_NOT_FOUND` | 404 on chat or a mapped code | NO | no | no |
| `GATEWAY_MODEL_IDENTITY_MISMATCH` | the answer names another model; the answer is discarded | YES | no | no |
| `GATEWAY_RATE_LIMITED` | 429; `Retry-After` is recorded and honoured up to `max_retry_after_seconds` | NO | no | yes |
| `GATEWAY_TIMEOUT` | read timeout, 408 or 504 | UNKNOWN on chat | yes | yes |
| `GATEWAY_CONNECTION_LOST` | the connection dropped mid-answer | UNKNOWN on chat | yes | yes |
| `GATEWAY_UNAVAILABLE` | 503 (refused) or other 5xx (UNKNOWN on chat) | NO / UNKNOWN | yes | yes |
| `GATEWAY_RESPONSE_INVALID` | the answer does not match the contract | — | yes | no |
| `GATEWAY_CIRCUIT_OPEN` | refused before any traffic | NO | — | no |
| `GATEWAY_NOT_AUTHORIZED_FOR_EXECUTION` | execution disabled, classification missing or above the ceiling, or RESTRICTED | NO | — | no |
| `GATEWAY_WRITE_OUTCOME_UNKNOWN` | a chat call that may have reached the gateway got no usable answer, and no honoured key allows a re-send; the detail carries `cause` | UNKNOWN | — | never |

Provider text never leaves the adapter; only codes, HTTP status, counters and hashes do. Each code has a
plain-language explanation (English in `EXPLANATIONS`, zh-TW in the web catalogue).

## Retry and circuit policy

- **Reads** (`health`, discovery) retry with exponential backoff and jitter through the existing
  `pados.operability.resilience.retry_call`, bounded by `max_attempts` and by any caller deadline.
- **Chat** is retried only when `idempotency.honoured` is true. The same key is sent on every attempt (a node job
  passes `<request_id>:<item index>`). Otherwise a chat call is sent once. If its outcome is unknown, the caller gets
  `GATEWAY_WRITE_OUTCOME_UNKNOWN` and must reconcile before trying again. The simulated E2E proves both cases by
  counting the gateway's side effects.
- **Circuit** (per provider, in process memory, visible in diagnostics): `CLOSED`, then `OPEN` after
  `failure_threshold` consecutive dependency failures. While open, calls are refused without traffic. After
  `open_seconds` the circuit is `HALF_OPEN` and exactly one trial call decides: success closes it (for example after a
  VPN reconnect), failure opens it again. An operator's **Test connection** is a read-only probe and may serve as the
  trial while open, so recovery is noticed at once. When the circuit opens during a call's own retries, the call
  reports the real cause, not the pause.
- **Receipts**: every attempt, including refusals before the network, is a content-free record
  (`pados.compute-node.gateway-attempt.v1`): operation, attempt number, outcome code, HTTP status, write outcome,
  circuit state before and after, `Retry-After`, rate-limit headers, SHA-256 of the request body and of the
  idempotency key, `semantic_authority: false`. A node writes them under its state directory (`gateway-attempts/`), and
  the Control Center writes them next to the provider setup file.

## Data classification and project scope

- The contract's `policy` is the gateway owner's ceiling. The provider setup can narrow it further. The effective
  ceiling is the stricter of the two, and execution needs both to allow it.
- Every chat call must carry a classification (a node passes the job's classification). A missing classification,
  one above the ceiling, or `RESTRICTED` is refused before any traffic with `GATEWAY_NOT_AUTHORIZED_FOR_EXECUTION`.
- A node refuses to start when a model on a gateway backend is declared above the contract's ceiling
  (`GATEWAY_MODEL_CLASSIFICATION_ABOVE_CONTRACT`). The node's job admission also refuses data above the model's
  classification.
- Project scope stays with the existing authority, the compute-node registration (`project_ids`). The provider setup
  can only narrow it for the consumer path (`PROVIDER_PROJECT_NOT_IN_SCOPE`).
- Being reachable never releases data: classification and project policy decide, never reachability.

## Provider setup and diagnostics (`/control/providers`)

| Route | Purpose | OP-004 | Proposed session scope |
|---|---|---|---|
| `GET /control/providers` | providers, catalog profiles, registrations, governed-use rule | read | sessions allowed |
| `GET /control/providers/{provider_id}` | one provider | read | sessions allowed |
| `POST /control/providers/{provider_id}/diagnostics` | health, discovery, model identity, governance, circuit; optional fixed PUBLIC probe prompt (`probe_chat`) | `NO_DURABLE_WRITE` (attempt receipts only) | sessions allowed |
| `PUT /control/providers/{provider_id}` | replace non-sensitive settings (revision-guarded) | `STATE_GUARDED` | token or session |
| `POST /control/providers/{provider_id}/enable` and `/disable` | toggle (revision-guarded) | `STATE_GUARDED` | token or session |

The setup file (`PADOS_PROVIDERS_SETUP`, default runtime/local/providers.json, schema `pados.providers.setup.v1`) is
written atomically with owner-only permissions under the store lock. It holds no key value: only the NAME of an
environment variable (`auth_env`), and any credential-looking value refuses the write
(`PROVIDER_SETTINGS_CONTAIN_KEY_VALUE`). It can only narrow. `resolve_bound_execution` refuses `PROVIDER_DISABLED`,
`PROVIDER_PROJECT_NOT_IN_SCOPE` and `PROVIDERS_SETUP_INVALID`. A provider without an entry is unchanged.

Diagnostics use only the existing paths: `describe_runtime` (local runtimes, loopback only), `GatewayBackend`,
`ModelCatalogStore`, `bind_live`, the compute-node registry and, for the configured chat model,
`resolve_bound_execution` itself. Per discovered model the result is one of:

| Governance | Meaning |
|---|---|
| `AUTHORIZED_AND_MEASURED` | A measured profile matches the exact digest, binds live (runtime version and device), and a registration names its `model_ref`. |
| `MEASURED_NOT_AUTHORIZED` | Measured and bindable, but no registration authorizes it. |
| `MEASURED_NOT_APPLICABLE` | A profile exists, but its evidence does not apply here (for example `RUNTIME_VERSION_NOT_MEASURED`). |
| `NOT_MEASURED` | No profile for this digest: non-authoritative observations only. |

## Simulated gateway and E2E

`tests/compute_node/gateway_sim.py` is a stdlib server with a deliberately non-OpenAI API. It can list models, answer,
require a header key, return 401, 403, 404, 429 and 503 (including a "model cold" code), delay past the timeout, drop
the connection after processing, answer as the wrong model, dedupe idempotency keys and count side effects. TLS tests
use a throwaway certificate made with the `openssl` binary at test time (skipped without it). A TLS failure is also
proven without any certificate, by speaking HTTPS to a plain HTTP endpoint.

`scripts/dogfood/gateway_simulated_e2e.py --out <dir> [--playwright] [--real-ollama-catalog <catalog>]` runs the steps
listed in its docstring and writes GATEWAY_SIMULATED_E2E_RESULT.json with label `SIMULATED_E2E_PASS`.

## Known limits

- No real enterprise gateway was called (`ENTERPRISE_LIVE_NOT_VALIDATED`).
- Streaming answers are not supported (one JSON answer per call).
- System proxy variables are ignored (`trust_env=False`). A gateway reachable only through an HTTP proxy needs a
  contract extension.
- Circuit state is per process and is lost on restart, by design (observation, not authority).
- A gateway cannot run a measured route. Measuring a gateway model would need a verification design for its
  execution configuration.
