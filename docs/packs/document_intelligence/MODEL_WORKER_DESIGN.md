# Model worker design: DI-1 and DI-2 (candidate; not a production boundary)

Status: implemented as an **opt-in** candidate in PR #25. The default stays in-process. Promoting the worker to the
default, or calling it a security boundary, is an owner decision.

## Threats addressed

| ID | Threat | In-process today | Worker candidate |
| --- | --- | --- | --- |
| DI-1 | ONNX Runtime or the tokenizer (native code) misbehaves on a model or an input: memory corruption, a hang or a memory blow-up inside the PADOS process | Every bound applies (inputs, deadline, poisoning), but a hung batch cannot be killed and `max_ram_mb` is not enforced | Separate process. A missed deadline **kills** the process group, and the next call starts fresh. A watchdog kills the tree when RSS passes `max_ram_mb` |
| DI-2 | A model file swapped between hashing and loading | Mitigated only by operator file permissions | `materialize_verified`: copy to a fresh private directory while hashing the written bytes, refuse on any mismatch, mark read-only. The engine loads only the copy. In-process engines can opt in with `private_copy=True` |

## Properties

| Property | How |
| --- | --- |
| Fixed model identity | Pin = repository + 40-hex revision + per-file SHA-256 and size. The worker receives the pin and **re-verifies** its private copy before loading. Unpinned files are refused. |
| Content-addressed verified inputs | The copy's bytes are exactly the hashed bytes; the operator directory is never given to the worker. |
| Bounded resources | One request at a time; request and response lines ≤ 32 MiB; per-batch deadline (the pin's timeout × 0.9 in the worker, under the engine's own deadline); RSS watchdog over the process **tree** (on Windows a venv's `python.exe` is a launcher whose child is the interpreter, found by a test); POSIX `RLIMIT_CORE = 0`, `RLIMIT_NOFILE = 256`. Kernel-enforced memory cap above `max_ram_mb` (Windows Job Object, Linux `RLIMIT_AS`): see `MODEL_WORKER_ISOLATION.md`. |
| Filesystem | The working directory is the private copy; the worker gets nothing but that directory and the texts it is sent. |
| Network | Empty environment plus `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` / `OMP_NUM_THREADS`, with no proxies, tokens or user variables. Socket `connect` is refused in-process before the engine is imported. The live test loads the in-process engines with sockets blocked. Linux adds a seccomp filter (no `socket()` at all); Windows has no OS network control yet: see `MODEL_WORKER_ISOLATION.md`. |
| Timeouts | A load gets 5× the pin timeout; each batch gets the pin timeout × 0.9. A miss means kill and `LOCAL_MODEL_TIMEOUT` (retryable); there is no poisoning, because no thread is left running. |
| Process cleanup | Kill of the whole tree on close, on garbage collection (`weakref.finalize`) and at exit (`atexit`); the private copy is removed. POSIX uses `start_new_session`; Windows uses `CREATE_NEW_PROCESS_GROUP`. |
| Closed failures | `MODEL_WORKER_EXITED`, `_MEMORY_LIMIT`, `_START_FAILED`, `_PROTOCOL_INVALID`, `_OUTPUT_TOO_LARGE`, `_IDENTITY_REFUSED`, `_ENGINE_UNKNOWN`, `_ISOLATION_FAILED` and `_MEMORY_REFUSED`, all declared in the model manifests. No input or engine text is echoed. |

Evidence:

- `tests/packs/document_intelligence/test_di_model_worker.py`: 12 tests. The dependency-free
  `hashing-test-engine` exercises the boundary in CI, and live parity runs where the pinned models exist.
- Windows: 12 of 12 passed.
- VMware, fresh offline venv at `f699344`: passed within 1216 of 1216.

## Not done (owner decisions)

- **Kernel-enforced memory cap: done** in `MODEL_WORKER_ISOLATION.md` (Windows Job Object with kill-on-close and a
  sealed process count; Linux `RLIMIT_AS` with measured headroom). The watchdog stays.
- **OS network isolation: Linux done** (seccomp socket filter). **Windows is an owner decision**: an AppContainer
  launch (recommended), an administrator-installed firewall or WFP rule, or accepting the residual. See
  `MODEL_WORKER_ISOLATION.md`.
- **Default mode.** Each worker costs one model load (about 1 s) and one copy of the model (about 160 MB) per
  engine.
- **A signed model provenance chain** beyond HTTPS plus the pinned digests.
