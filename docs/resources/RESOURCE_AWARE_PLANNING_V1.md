# Resource-Aware Planning V1

Package: `src/pados/resources/` (Capability Expansion Phase E). Tests: `tests/resources/`.

## Purpose and authority boundary

PADOS decomposes work into small capabilities. Some of them (model inference, embeddings, OCR models,
heavy algorithms) need specific hardware. This package answers two descriptive questions, deterministically
and with explanations:

1. *Does this resource requirement fit this machine, and why (not)?*
2. *For this task's capability, which (runtime, model, resource) tuples are feasible?*

It does **not** decide anything:

    described != registered != authorized != selected != accepted

* No hardware or network probing. Hardware profiles are operator-declared or translated read-only from an
  existing compute-node descriptor; tests run with outbound sockets disabled.
* No model or runtime is installed, downloaded, chosen or hard-coded. Example profiles in the tests are
  synthetic (`example-text-7b`, `example-embed-small`, ...).
* No ranking authority. Feasible tuples are listed in a fixed order: `resource_id`, then `model_id`, then
  runtime declaration order. Measured preference belongs to Outcome Learning (`ScoreAdvisor`, bounded by
  `apply_advice`); model-provider selection stays with the Worker Router
  (`pados.worker.router.select_worker`); authorization stays with `pados.capability.policy.evaluate`;
  acceptance stays with the receipt -> validation -> CAS path. Every plan carries
  `selection_authority = "NONE"`.
* The planner reaches hardware only through the fabric seam `pados.capability.seams.ResourceInventory`
  (`match(requirement)`), so any inventory implementation can back it.

## Modules

| Module | Contents |
| --- | --- |
| `hardware.py` | `HardwareProfile` (resource id, node role, OS, architecture, CPU cores, RAM, GPU vendor + bounded label, VRAM, unified memory, accelerator runtimes, optional `mach-...` machine ref, `observed_at`, `stale`), `hardware_from_node`, `hardware_from_document` |
| `models.py` | `ModelRuntimeProfile`, `ModelCatalog`, `MemoryRequirement`, `profile_from_manifest`, `profiles_from_node` |
| `inventory.py` | `StaticResourceInventory` (implements `ResourceInventory`), `assess`, `requirement_for`, `Feasibility`, `resource_fit` |
| `planning.py` | `PlanningTask`, `ResourcePlanner`, `ResourcePlan` (`plan_hash`, `evidence()`, `resource_fit`) |

Closed vocabularies: node roles mirror `pados.machines.profile.MachineRole`; accelerator runtimes are
`CPU, CUDA, ROCM, METAL, DIRECTML, VULKAN, ONEAPI, NPU`; model roles are
`TEXT, EMBEDDING, RERANKER, VISION, OCR`.

## Fit semantics (fail closed)

Every evaluation reports one reason per dimension, e.g.
`RAM_OK:required_mb=8192:available_mb=65536`, `VRAM_INSUFFICIENT:required_mb=40960:available_mb=16384`,
`ACCELERATOR_AVAILABLE:runtime=CUDA`, `TELEMETRY_STALE:age_s=7500:max_s=900`.

* **Telemetry**: a profile flagged `stale`, older than `max_telemetry_age` (default 15 min) or more than
  `max_future_skew` (default 2 min) in the future never fits, whatever the requirement. The inventory is
  evaluated at a fixed `as_of` instant passed by the caller; it never reads the clock.
* **Dedicated GPU machines**: RAM and VRAM are checked separately. A requirement sized only in unified
  memory cannot be converted into VRAM and does not fit (`VRAM_REQUIREMENT_UNDECLARED`).
* **Unified-memory machines (Apple silicon, unified APUs)**: RAM and GPU memory are one pool, reported once
  (`ram_mb == unified_memory_mb`). Pool demand is the declared `unified_memory_mb`, or the conservative sum
  `ram_mb + vram_mb` when undeclared; the RAM figure must also fit.
* **Accelerators**: `ResourceRequirement.accelerators` is an any-of set; unknown names fail.
* **Models**: each runtime path must declare its own memory figure (`ram_mb` for CPU/NPU, `vram_mb` for
  dedicated GPU runtimes, `unified_memory_mb` for METAL). PADOS never estimates memory from parameter counts.
* **Model filters** in planning (role, modality overlap, context length, tool use, structured output,
  classification ceiling, network ceiling of both task and capability) only exclude, each with a reason.

## Connecting to policy

`resource_fit(inventory.match(requirement), resource_id)` or `plan.resource_fit(resource_id, model_id=...,
runtime=...)` produces the boolean that `pados.capability.policy.evaluate(..., resource_fit=...)` consumes,
with `PolicyContext.resource_id` naming the same resource. Unknown resources never fit. The decision is
still made by policy and re-validated before execution.

`ResourcePlan.evidence()` returns a bounded, content-free record (ids, codes, counts, `plan_hash`) that
passes `assert_minimized` with an explicit schema; it can be referenced from stage receipts. Nothing in
this package persists anything.

## How the user registers their chosen local model later

PADOS ships no model choice. When you have installed a model runtime yourself (outside PADOS), describe it:

1. **Describe the machine** in a hardware document (schema `pados.resources.hardware-inventory.v1`), or
   derive it from a registered compute node with `hardware_from_node(descriptor, HardwareCapacity(...))`:

   ```json
   {"schema_version": "pados.resources.hardware-inventory.v1",
    "resources": [{"resource_id": "home-workstation", "node_role": "HOME_BASE", "os": "WINDOWS",
                   "architecture": "X86_64", "cpu_cores": 16, "ram_mb": 65536,
                   "gpu": {"vendor": "NVIDIA", "label": "RTX-class 16 GB"}, "vram_mb": 16384,
                   "runtimes": ["CPU", "CUDA"], "observed_at": "2026-10-01T12:00:00+00:00"}]}
   ```

   For a Mac, set `os` to `MACOS`, `architecture` to `ARM64`, `unified_memory_mb` equal to `ram_mb`,
   `gpu.vendor` to `APPLE` and include `METAL`. Refresh `observed_at` whenever you re-measure; old figures
   stop fitting automatically.

2. **Describe the model** in a catalog document (schema `pados.resources.model-catalog.v1`) using the
   canonical compute-node id `<backend>/<backend model>@<version or digest>`, the memory figure for every
   runtime path you will use, and its license:

   ```json
   {"schema_version": "pados.resources.model-catalog.v1",
    "models": [{"model_id": "<backend>/<your-model>@<version>", "backend": "<backend>",
                "family": "<family>", "roles": ["TEXT"], "modalities": ["TEXT"], "context_length": 8192,
                "quantization": "Q4_K_M", "runtimes": ["CPU", "CUDA"], "ram_mb": 9000, "vram_mb": 7000,
                "max_classification": "CONFIDENTIAL", "network_requirement": "LOCAL_ONLY",
                "license": {"spdx": "<SPDX id>", "engine_version": "<version>",
                            "distribution": "RUNTIME_OPTIONAL"},
                "tool_use": false, "structured_output": true}]}
   ```

   If the model is served by a registered compute node, `profiles_from_node(descriptor, {model_id:
   MemoryRequirement(...)})` translates its advertised manifests instead; models that are not `AVAILABLE`
   or have no declared memory figures are skipped with an explicit reason.

3. **Load and plan** (Python API; no CLI wiring in V1):

   ```python
   inventory = StaticResourceInventory(hardware_from_document(hw_doc), as_of=now)
   planner = ResourcePlanner(inventory, ModelCatalog.from_document(model_doc))
   plan = planner.plan(PlanningTask(task_id=..., capability=..., classification=...,
                                    network_policy=..., role=ModelRole.TEXT))
   ```

4. **Authorize separately.** A feasible tuple is not permission. The project grant must still name
   `CAPABILITY:<capability_id>`, policy must evaluate ALLOWED (with `resource_fit` from the plan), and model
   inference must still be selected by the Worker Router.

Documents are bounded (at most 256 entries, 1 MiB) and pass `assert_minimized` before parsing; profiles
reject unknown fields, so endpoints, paths, download locations and commands cannot be smuggled in.

## Not in V1

* No CLI command or settings key (would require `src/pados/cli/main.py` / `src/pados/config.py`).
* No automatic hardware discovery or runtime health probing.
* No persistence of profiles or plans.
