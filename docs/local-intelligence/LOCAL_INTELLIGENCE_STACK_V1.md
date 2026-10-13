# Local Intelligence Stack V1

Stage B adds **measurement** to PADOS: what this machine is, which local inference runtimes it runs, and (in
later milestones) how well a specific model performs on it. It adds no authority. The existing chain stays:

```
Human intent → Task contract → Capability requirement → Capability Registry / Fabric planner
  → ResourceInventory (HardwareProfile)   ← Device Profiler projects into this
  → Worker Router (select_worker) / Compute Node provider
  → execution → receipts → verification → CAS → Outcome Learning (advisory)
```

```
discovered != audited != downloaded != benchmarked != registered != authorized != routable
```

## Package `pados.local_intelligence`

| Module | Owns | Never does |
|---|---|---|
| `probes.py` | Structured probe execution: constant argv, `shell=False`, stdin closed, stderr discarded, 256 KiB output cap, timeouts. `ProbeRecord` (outcome, code, duration, SHA-256 of the raw output). `Basis` (`UPSTREAM_FACT`, `MODEL_CARD_CLAIM`, `COMMUNITY_CLAIM`, `PADOS_MEASURED`, `UNKNOWN`). | Trust tool output without validation |
| `device.py` | `DeviceProfile` contract and `DeviceProfiler`; `to_hardware_profile` projection into `pados.resources.hardware.HardwareProfile` | Register, route, authorize |
| `runtimes.py` | `RuntimeDescriptor`; probes keyed by Compute Node backend kinds (`ollama`, `openai_compatible`, `llama_cpp`, `mlx_openai_compatible`) | Probe beyond loopback; create a runtime kind outside `BACKEND_KINDS` |
| `capture.py` | `python -m pados.local_intelligence.capture --out DIR`: writes the evidence documents plus `raw/` probe outputs | Download or install anything |
| `runtime_evidence.py` | Enterprise Local Runtime Adapter: validates an external runtime plane's evidence document (size, schema, credentials, optional HMAC through a SecretRef) and projects it into `RuntimeObservation` / `DeviceObservation` for `bind_live` (`docs/architecture/ENTERPRISE_LOCAL_RUNTIME_INTEROP.md`) | Network I/O; treat a lease as authorization; raise into the planner |

`pados.resources` is unchanged and still never probes. Its boundary test pins its module list.

## Device Profiler

| Fact | Source (Windows / Linux) |
|---|---|
| OS, release, version, name | `platform`; registry `CurrentBuildNumber`/`DisplayVersion`/`EditionID` (build ≥ 22000 is Windows 11, because `ProductName` still says 10) / `/etc/os-release` |
| Architecture | `platform.machine()` → `Architecture` |
| Machine identity | SHA-256 of the case-folded hostname (the name itself is never recorded); optional `mach-…` machine ref |
| Virtualization / form factor | BIOS registry keys / `/sys/class/dmi/id`; battery via `psutil` → `VIRTUAL_MACHINE`, `LAPTOP`, `NO_BATTERY_REPORTED` |
| CPU model, physical/logical cores | registry `ProcessorNameString` / `/proc/cpuinfo`; `psutil.cpu_count` |
| RAM total/available, disk total/free | `psutil.virtual_memory`; `shutil.disk_usage` (recorded under a label, never a path) |
| Display adapters | registry display device class (`DriverDesc`, `MatchingDeviceId` → PCI vendor) / `/sys/bus/pci/devices/*` class `0x03` |
| NVIDIA GPU, VRAM total/used/free, driver, compute capability, utilization | `nvidia-smi --query-gpu=… --format=csv,noheader,nounits` (retried with a basic field set for older drivers) |
| CUDA driver API version, NVML version | `nvidia-smi --version` (fallback: the `nvidia-smi` summary header) |
| CUDA toolkit | `nvcc --version` (only probed when a GPU is present; absence is normal) |
| Network mode | the outbound route's address class (a UDP `connect` to TEST-NET-1 selects a route and sends nothing): `NO_ROUTE`, `LOOPBACK_ONLY`, `PRIVATE_NETWORK`, `PUBLIC_ADDRESS` |
| Python runtime | `platform` |

Every field of `nvidia-smi` output is validated (index, display-safe name, MiB ranges, version, PCI bus id and
compute capability patterns). If any row fails, the whole probe is `INVALID_OUTPUT` and no GPU is claimed.

### GPU status

| Status | Meaning | Projection |
|---|---|---|
| `GPU_PRESENT` | Enumerated by the vendor runtime with validated memory facts | GPU + VRAM; `CUDA` only when the driver reports a CUDA version |
| `GPU_ABSENT` | Platform enumeration succeeded; only non-compute adapters (e.g. a hypervisor's emulated display) | CPU only |
| `GPU_UNAVAILABLE` | A GPU is there, but its runtime is missing or failing (`NVIDIA_SMI_NOT_FOUND`, `NVIDIA_DRIVER_UNAVAILABLE`), or no probe exists for its vendor yet | CPU only |
| `GPU_DISCOVERY_FAILED` | The probes themselves failed (enumeration unavailable, timeout, invalid output) | CPU only |

A missing GPU never stops PADOS, and a GPU that was not measured can never satisfy a GPU requirement.

## Runtime discovery

A runtime is described by `runtime_id`, `runtime_type` (a Compute Node backend kind), `endpoint` (scheme, host
and port only), `network_requirement`/`local`, `health` (`HEALTHY`, `UNREACHABLE`, `PROBE_FAILED`,
`NOT_PROBED`), `failure_code`, `version`, `protocols`, `models` (each `DISCOVERED`, with the runtime-reported
digest, size, family, parameter size, quantization and format), `loaded_models` (resident size and VRAM size),
`streaming`/`structured_output`/`tool_use` (each a `FeatureSupport` with its basis), `last_probe` and `probes`.

* Ollama: `/api/version`, `/api/tags`, `/api/ps`. The runtime feature facts are Ollama's published API
  (`UPSTREAM_FACT`, version-gated). Whether a given model honours them is measured per model later.
* OpenAI-compatible servers: `/v1/models`.
* Only loopback `http` endpoints without credentials, paths or queries are probed (bounded by
  `pados.providers.discovery`: 1 MB, 5 s). Anything else is `NOT_PROBED` / `DISCOVERY_LOOPBACK_ONLY`. Remote
  runtimes join through Compute Node registration.
* PADOS startup does not need Ollama. An absent runtime is `UNREACHABLE`, not an error.

## Compute Node admission (Stage B R4 fix)

`JobManager` reserves a slot at admission and releases it when the worker call returns. The old design
inferred load from job states, so a job that was not yet scheduled (`QUEUED`) or that had been cancelled or
timed out while its backend call was still running did not count as occupying a worker. Bounds are now
enforced as `occupied < max_concurrent_jobs + max_queue_depth`, independent of thread scheduling.
`NodeLoad.running`/`queued` report slot occupancy. The wire schema is unchanged.

## Measured execution contract (Stage B M4.2, accepted)

Every `ModelCapabilityProfile` records, per measured capability, the execution configuration it was measured under (`execution_conditions`: context window, reasoning mode, temperature, seed, output constraint, output budget and the largest observed completion), derived from raw benchmark records. The `MeasuredExecutionGate` binds one condition per requirement and the runtime adapter reproduces it or refuses. The first live dogfood failed because it did not (reasoning spent the whole output budget: `EMPTY_COMPLETION`); with the contract the live run passed and was independently verified. Full rule: `docs/architecture/MEASURED_CAPABILITY_EXECUTION_CONTRACT.md`.
