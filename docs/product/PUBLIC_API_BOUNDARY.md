# Public API boundary (0.1.0a1)

Every externally visible name is classified as one of:

- **PUBLIC_STABLE**: covered by the compatibility policy.
- **PUBLIC_EXPERIMENTAL**: may change in 0.x with a changelog entry.
- **INTERNAL**: not a product contract.
- **LEGACY_COMPATIBILITY**: kept for existing deployments; not for new use.

Before 1.0 nothing is frozen by semver, but PUBLIC_STABLE items change only with a migration note (see VERSIONING_POLICY.md and COMPATIBILITY_POLICY.md).

## Product principles (stable)

- `MODEL OUTPUT != TRUTH`
- `EXECUTION COMPLETE != SEMANTIC ACCEPTANCE`
- Project state changes only through: ExecutionReceipt → verification → ResultReceipt → validation → expected ScopeVersion → CAS → immutable audit.

## CLI (`pados`)

| Command | Class |
|---|---|
| `pados version`, `pados init`, `pados doctor`, `pados serve` | PUBLIC_STABLE (first-run path) |
| `pados node serve`, `pados node probe` | PUBLIC_EXPERIMENTAL |
| `pados project create` | PUBLIC_STABLE |
| `pados memory …` (`project-register`, `fact-add`, `promote`, `decision-add`, `artifact-register`, `event-add`, `search`, `stale`) | PUBLIC_EXPERIMENTAL |
| `pados context assemble / inspect` | PUBLIC_EXPERIMENTAL |
| `pados worker list / health / capabilities / diagnose` | PUBLIC_EXPERIMENTAL (diagnostics) |
| `pados loop …` (P6.10 task loop) | LEGACY_COMPATIBILITY |
| `Start-PADOS*.ps1` launchers | LEGACY_COMPATIBILITY (Windows convenience) |

## HTTP API (loopback only; the control token is required for control and product-loop surfaces)

| Surface | Class |
|---|---|
| `GET /health`, `GET /ready` | PUBLIC_STABLE |
| `POST /product-loop/runs`, `GET /product-loop/runs/{id}` | PUBLIC_EXPERIMENTAL (governed execution entry point) |
| `GET /api/mission-control`, `GET /api/decision-inbox`, `GET /api/projects` | PUBLIC_EXPERIMENTAL (projections; `projection_only: true`) |
| `mission-control.enterprise`, `mission-control.compute_nodes` blocks | PUBLIC_EXPERIMENTAL (optional; absent when not configured) |
| `/control/*` (approvals, jobs, loops, …) | PUBLIC_EXPERIMENTAL |
| `/api/graph/*`, `/api/corpus/*`, `/memory/*`, `/context/assemble`, other `/api/*` | INTERNAL until reviewed individually (used by the bundled UI) |
| `/docs`, `/redoc`, `/openapi.json` | INTERNAL (developer aids) |

## Contracts and schemas

| Contract | Class |
|---|---|
| ExecutionReceipt (`schema_version "1"`), ResultReceipt, ScopeVersion/CAS semantics, semantic audit chain | PUBLIC_STABLE (semantics); storage layout INTERNAL |
| `pados.enterprise.capability-manifest.v1` | PUBLIC_EXPERIMENTAL |
| `pados.enterprise.retrieval.v1` request/response | PUBLIC_EXPERIMENTAL |
| `pados.enterprise.config.v1` | PUBLIC_EXPERIMENTAL |
| `pados.compute-node.protocol.v1` (`/health`, `/capabilities`, `/models`, `/execute`, `/jobs`, `/artifacts`) | PUBLIC_EXPERIMENTAL |
| `pados.compute-node.config.v1`, `pados.compute-nodes.registry.v1` | PUBLIC_EXPERIMENTAL |
| `pados.compute-node.execution-receipt.v1` | PUBLIC_EXPERIMENTAL |
| `pados.compute-node.batch.v1` | PUBLIC_EXPERIMENTAL |
| Database table layouts, Alembic revision ids | INTERNAL (the migration *procedure* is public) |

## Python packages

| Package | Class |
|---|---|
| `pados.enterprise` (manifest, classification, authorization adapter, retrieval, rerank, lineage, budget, secrets, observability) | PUBLIC_EXPERIMENTAL SDK |
| `pados.compute_node` (manifest, backends protocol, provider, registry, public) | PUBLIC_EXPERIMENTAL SDK |
| `pados.compute_node.identity_providers` (`NodeIdentityProvider`, `SecurityProfile`, `IdentityProfile`) | PUBLIC_EXPERIMENTAL |
| `pados.versions` | PUBLIC_STABLE |
| `pados.worker`, `pados.product_loop`, `pados.state_proof`, `pados.execution`, `pados.memory`, `pados.operations`, all others | INTERNAL |

## Internal compatibility mapping: `WorkerType.OLLAMA`

The frozen Worker Router has no compute-node worker type. `LocalInferenceProvider` presents eligible (node, model) pairs to it as its existing *local-model worker class* `WorkerType.OLLAMA`. This is **INTERNAL / LEGACY_COMPATIBILITY**. The router schema is not changed for naming.

The public identity of a node-backed executor uses capability and provider terms only. `pados.compute_node.public` is the single translation boundary:

| Public term | Meaning |
|---|---|
| `COMPUTE_NODE` | Executor kind for node-backed work (`public_worker_view()`) |
| `LOCAL_INFERENCE` | Capability |
| `PADOS_LOCAL_INFERENCE` | Provider id in receipts |
| `OLLAMA_BACKEND`, `OPENAI_COMPATIBLE_BACKEND`, `LLAMA_CPP_BACKEND`, `MLX_BACKEND`, `DETERMINISTIC_BACKEND` | Backend labels (node config kinds `ollama`, `openai_compatible`, `llama_cpp`, `mlx_openai_compatible`, `deterministic`) |

**Guard.** A test asserts that `WorkerType` does not appear in the node doctor, Mission Control projection, node receipts or UI panel (`tests/productization/test_productization.py`).
