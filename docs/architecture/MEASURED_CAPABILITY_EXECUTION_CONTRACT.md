# Measured Capability Execution Contract

Status: **ACCEPTED** (Stage B M4.2, live on the Windows reference node, independently verified on Linux).
Code: `src/pados/resources/capability_profile.py` (contract), `src/pados/compute_node/provider.py` (gate),
`src/pados/compute_node/backends.py` (runtime adapters), `src/pados/local_intelligence/profile_builder.py`
(derivation from evidence).

## Rule

A capability is not a property of a model. It is a measurement of:

```
CAPABILITY = MODEL ARTIFACT x RUNTIME x DEVICE x EXECUTION CONFIGURATION x EVIDENCE
```

So a **measured capability is not an unconditioned model property**. PADOS may claim a measured capability
only while it executes under the capability-critical settings the measurement used. The execution gate must
never run a "measured" route under materially different settings. If the settings are **missing**,
**inconsistent**, **incompatible** or **unverifiable**, it fails closed.

```mermaid
flowchart LR
    E[Raw benchmark evidence<br/>per-call options] --> B[Profile builder]
    B --> P[ModelCapabilityProfile<br/>execution_conditions]
    P --> R[required_execution<br/>requirement -> ONE condition]
    R --> G{Measured gate<br/>check_execution}
    G -- COMPATIBLE --> N[Node request<br/>generic ExecutionConfig]
    G -- MISSING / INCONSISTENT /<br/>INCOMPATIBLE --> X[Refuse before the node<br/>MEASURED_EXECUTION_CONFIG_MISMATCH]
    N --> A[Runtime adapter<br/>translates or refuses]
    A --> O[Observed behaviour checks<br/>reasoning / truncation / empty]
    O --> S[Signed node record echoes<br/>the bound configuration]
```

## Generic vocabulary (planner-facing)

`ExecutionConfig` is runtime-independent. The planner, Worker Router bridge and stage adapter never see
runtime parameter names. A static test enforces this.

| Field | Meaning | Compatibility rule |
|---|---|---|
| `context_window` | tokens of context the runtime allocates | must equal the measured value |
| `reasoning_mode` | `DISABLED` / `ENABLED`: a separate reasoning phase before the visible answer | must equal |
| `temperature` | sampling temperature | must equal |
| `seed` | sampling seed (or none) | must equal |
| `output_constraint` | `UNCONSTRAINED` / `JSON_SCHEMA` (schema-constrained decoding) | must equal |
| `max_output_tokens` | output budget | must cover the largest completion the measured runs needed (or the measured budget when that is unknown) |

`MeasuredExecutionCondition` binds one `ExecutionConfig` to the capabilities measured under it. It also
records the largest observed completion, the sample size and evidence references.

### Requirement resolution

`required_execution(profile, requirement)` collects every capability the requirement claims: its floors,
plus long-context, latency and throughput bounds when they are set. It returns the **one** condition under
which all of them were measured. Several matching conditions are resolved only when they differ in the
context window alone, and then the smallest sufficient window wins. Otherwise:

- `MEASURED_EXECUTION_CONFIG_MISSING:<capability>`: a claimed capability has no recorded condition.
- `MEASURED_EXECUTION_CONFIG_INCONSISTENT`: the claimed capabilities were measured under different
  capability-critical settings.

`assess()` adds this refusal, so a profile whose conditions are unknown is never routable. A profile built
before conditions were recorded keeps its identity, because an empty list is omitted from the hash, but it
supports no requirement.

## Enforcement points

1. **Admission** (`LocalInferenceProvider.measured_check`). The route carries the bound
   `MeasuredExecutionCondition`. Re-validation before and after execution compares it.
2. **Before submission** (`effective_execution`). The effective configuration is the bound condition plus
   the caller's output budget and output constraint. Nothing else can be changed: `execute()` has no
   configuration parameter. Anything not `COMPATIBLE` raises
   `EXECUTION_GATE_REFUSED:MEASURED_EXECUTION_CONFIG_MISMATCH:<field>`, and the node is never contacted.
3. **Runtime adapter** (`backends.py`). The adapter translates the generic fields or refuses with
   `EXECUTION_CONFIG_UNVERIFIABLE`. The generic OpenAI-compatible API has no portable per-request context
   window or reasoning switch, so it refuses. Observable violations fail closed:
   - `EXECUTION_CONFIG_NOT_APPLIED:reasoning_mode`: reasoning text appeared although it was disabled.
   - `OUTPUT_TRUNCATED`: `done_reason=length`.
   - `EMPTY_COMPLETION`.
4. **Node record**. The MAC-signed record and the node receipt carry the configuration. The provider requires
   the record and the adapter's `execution_requested` echo to equal what it sent; otherwise it raises
   `MEASURED_EXECUTION_CONFIG_NOT_APPLIED`. The echo proves what was *requested*. Behaviour is covered by
   point 3.

### Ollama translation (adapter-only)

| Generic | Ollama `/api/chat` |
|---|---|
| `reasoning_mode` | top-level `think` (bool) |
| `output_constraint=JSON_SCHEMA` | top-level `format` (the caller's schema) |
| `context_window` | `options.num_ctx` |
| `max_output_tokens` | `options.num_predict` |
| `temperature`, `seed` | `options.temperature`, `options.seed` |

The placement is the one the benchmark used against the installed runtime (0.34.3). The benchmark produced
the evidence, so its request shape is authoritative for this runtime.

## Failure evidence

A node job that does not succeed raises `NodeExecutionError` with `failure` (schema
`pados.compute-node.failure-evidence.v1`). It records:

- the request and job ids and the terminal state;
- the complete, bounded normalized code (for example `JOB_FAILED:EMPTY_COMPLETION`);
- the backend counters, re-filtered to integers and short labels (`done_reason`, `eval_count`,
  `thinking_chars`, …);
- whether the record MAC verified, and the record itself, only when verified and bounded;
- `node_receipt = NOT_ISSUED_FOR_FAILED_EXECUTION` and `semantic_authority = NONE`.

Prompts, content and credentials are never included.

## Evidence for the rule (Stage B)

| Observation | Source |
|---|---|
| The extraction capability was measured with `think=false`, `num_ctx=8192`, `temperature=0`, `seed=42`, a JSON schema, and a largest completion of 68 tokens | M3.1 profile `mcp-bd7794d4…`, re-derived identically on Windows and Linux |
| The only reasoning-enabled measurement needed 1327 output tokens | same profile, `REASONING_EXTENDED` condition |
| The pre-fix live request (`num_predict=256`, `temperature=0`, nothing else) returned **EMPTY_COMPLETION**: `done_reason=length`, 256 tokens all spent on 972 characters of reasoning, 0 visible characters, despite a `/no_think` prompt hint | `POST_HOC_REPRODUCTION.json` (real RTX 5070 / Ollama 0.34.3) |
| With the bound configuration, the same task produced a correct extraction (65 tokens, `done_reason=stop`) | same file; live dogfood archive `f81036e8…` |

## Known limits

- `output_constraint` binds the constraint *kind*. The caller's schema is recorded by hash. The measured
  quality numbers come from the benchmark's own schemas.
- Runtime server-side settings (environment variables of the runtime process) are captured as integrity
  telemetry but are not part of the condition.
- Concurrency, timeout recovery and reload stability carry no condition, so no requirement can claim them as
  measured.
