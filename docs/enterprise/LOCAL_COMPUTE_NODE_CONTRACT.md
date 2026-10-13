# Local compute node contract (V1: contract ready, implementation deferred)

The PADOS Worker Router remains the only provider router. This contract adds no scheduler, dispatcher, Kubernetes or Kafka.

## `ProviderCapabilityProfile`

| Field | Values |
|---|---|
| `provider_id` | Worker id |
| `locality` | LOCAL / REMOTE |
| `accelerators` | CPU / CUDA / APPLE_SILICON |
| `privacy_class` | Maximum `DataClassification` the provider may receive |
| `context_limit_tokens` | Context limit |
| `model_family` | Model family |
| `cost_class` | FREE_LOCAL / LOW / MEDIUM / HIGH / UNKNOWN |
| `latency_class` | INTERACTIVE / BATCH / UNKNOWN |
| `supported_tasks` | Supported tasks |

`profile_for_worker(WorkerSpec)` describes existing workers:

- Codex CLI and Claude Code are REMOTE, capped at INTERNAL. PADOS has no evidence that higher classes may leave the host.
- Deterministic and Ollama workers are LOCAL.

Unknown facts stay `UNKNOWN` rather than being invented.

## `ComputeNode`

| Field | Constraint |
|---|---|
| `node_id` | — |
| `endpoint` | Endpoints with embedded credentials are rejected |
| `platform` | LINUX / WINDOWS / MACOS |
| `capabilities` | — |
| `health` | Uses `CapabilityHealth` |
| `resource_class` | NONE / CPU_LIGHT / CPU_HEAVY / GPU |
| `privacy_class` | — |
| `models` | — |

`eligible_for(classification, capability)` is descriptive only.

## `LocalInferenceProvider` (protocol)

`profile`, `health()`, `generate(prompt, *, max_tokens)`.

Future adapters follow the same contract: Ollama, MLX, llama.cpp, local OpenAI-compatible endpoints, and Linux/vLLM workers. Each would register as a Worker Router worker and describe itself with a profile. Mac-mini-specific integration is explicitly not part of V1.
