# Model worker isolation: OS-enforced memory and network limits (candidate; worker stays opt-in)

Status: implemented as hardening of the **opt-in** model worker (`model_worker.py`, `worker_isolation.py`). The
default stays in-process. Nothing in PADOS selects the worker: an operator opts in by passing
`subprocess_factory(...)` as the engine `factory`, and a test pins that (`test_di_worker_default_off.py`). This
document is the evidence the owner asked for **before** any default-on proposal. It does not make that proposal.

When the worker is used, OS isolation is on (`os_isolation=True`). Turning it off is only for diagnosis; the RSS
watchdog alone remains. Any isolation step that cannot be completed fails closed with
`MODEL_WORKER_ISOLATION_FAILED`, and the worker never runs with less isolation than requested. A platform
without an implementation (anything other than Windows or Linux on x86_64 or aarch64) also fails closed.

## What is enforced, by whom

| Guarantee | Windows 11 | Linux (Ubuntu 24.04, kernel 6.8) | Enforced by |
| --- | --- | --- | --- |
| Memory ceiling | Job Object `JOB_OBJECT_LIMIT_JOB_MEMORY` = `max_ram_mb` + 512 MiB of committed memory, for every process in the job together | `RLIMIT_AS` = `max_ram_mb` + 1024 MiB of address space | kernel: the allocation is **refused** |
| Memory bound (`max_ram_mb`) | RSS watchdog, polled every 100 ms, kills the tree | same | PADOS parent (unchanged) |
| No new process | `JOB_OBJECT_LIMIT_ACTIVE_PROCESS`: 1, or 2 behind a venv launcher, while starting; sealed at READY to the processes then alive | not limited (Linux counts `RLIMIT_NPROC` per user, so it cannot bound one worker that needs threads). A child inherits the seccomp filter and `RLIMIT_AS` | kernel |
| No socket | **not enforced** (residual, see below) | seccomp: `socket()` fails with `EACCES` for **every** family (IP, Unix, netlink, packet); `io_uring_setup` fails with `ENOSYS`; a foreign-arch or x32 system call kills the process. Inherited by threads and children | kernel |
| In-process network refusal | `connect`, `connect_ex`, `sendto`, `sendmsg`, `create_connection`, `getaddrinfo` and `gethostby*` raise | same | the worker (defense in depth; native code and the `_socket` type bypass it) |
| Environment | empty, plus `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, `OMP_NUM_THREADS`, `RAYON_NUM_THREADS`, `PYTHONIOENCODING`, `PYTHONUTF8` and the variables the interpreter needs to start (`SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`, `PATH`); Linux adds `MALLOC_ARENA_MAX=2` | same | the parent |
| Worker dies with the parent | `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: the parent holds the only job handle | not enforced (the existing kill on close, garbage collection and exit remains) | kernel (Windows) |
| No crash dialog, no core | `DIE_ON_UNHANDLED_EXCEPTION` | `RLIMIT_CORE = 0`, `RLIMIT_NOFILE = 256` | kernel |

Closed failure codes, declared in the model manifests: `MODEL_WORKER_MEMORY_REFUSED` (the kernel refused an
allocation, so the worker replies and exits), `MODEL_WORKER_MEMORY_LIMIT` (the watchdog killed the tree) and
`MODEL_WORKER_ISOLATION_FAILED`.

## How it starts

### Windows: no instruction runs outside the job

1. The parent creates the Job Object and sets its limits. If that fails, nothing is started.
2. `Popen` with `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`. The process exists, but none of
   its code (not even the loader) has run.
3. `AssignProcessToJobObject`, then resume (`NtResumeProcess` through psutil). If either fails, the suspended
   process is terminated and the start is `MODEL_WORKER_ISOLATION_FAILED`.
4. Anything the worker starts from then on is created inside the job. A venv `python.exe` is a launcher whose
   child is the real interpreter, and that child is in the job too (PR #25 found the launcher; the sealed-job
   test checks every member of the tree).
5. After READY, the parent confirms from outside that every process of the tree is in the job
   (`IsProcessInJob`). It then seals the active-process limit to the processes alive. Windows attaches a
   `conhost.exe` to a console process. That process is counted in `ActiveProcesses` but is never held against
   the limit, so the seal leaves it out. Without that, one extra process could have started; the probe test
   found this.

Race: none between `Popen` and assignment, because the process is suspended until it is in the job.

What remains:

- The process count is decided by `starting_processes()`: 2 when `sys.executable` is a venv launcher, otherwise 1.
  An undercount fails closed, because the interpreter cannot start. An overcount (a symlinked venv) would allow
  one helper process before READY.
- The job memory limit counts **commit**, not RSS. An allocation inside native code that the limit refuses
  surfaces as `LOCAL_MODEL_FAILED` (ONNX Runtime raises) or `MODEL_WORKER_EXITED` (an abort). Both are closed
  codes, but neither names the cap.

### Linux: the worker limits itself before it reads anything

The parent uses no `preexec_fn`, so no Python runs between fork and exec in a possibly multithreaded parent;
this is the same rule as `ocr/_rlimit_exec`. The address-space limit travels as one strictly parsed argv value.
The worker's first action, before it reads the pin, imports the engine or reads stdin:

1. `RLIMIT_CORE = 0`, `RLIMIT_NOFILE = 256` and `RLIMIT_AS`, each read back.
2. It checks that `/proc/self/status` shows `Threads: 1`, because a `prctl` filter binds only the calling
   thread and later threads inherit it.
3. `PR_SET_NO_NEW_PRIVS`, then `PR_SET_SECCOMP` with a fixed BPF program built with ctypes. This needs no
   libseccomp and no privilege.
4. It checks that `/proc/self/status` shows `Seccomp: 2` and `NoNewPrivs: 1`.

If any step fails, the worker replies `MODEL_WORKER_ISOLATION_FAILED` and exits. After READY, the parent reads
`/proc/<pid>/limits` and `/proc/<pid>/status` and refuses unless both show the expected address-space limit and
`Seccomp: 2`.

The filter program has this order: check the architecture (`AUDIT_ARCH_X86_64` or `AUDIT_ARCH_AARCH64`,
otherwise kill), kill x32 system calls (x86_64), make `socket` return `EACCES`, make `io_uring_setup` return
`ENOSYS`, and allow everything else. `socketcall` does not exist on these two architectures.

What remains:

- Interpreter start-up and PADOS's own module imports run before the limits. That is trusted code; the model
  file, the pin and the input are read only afterwards.
- `RLIMIT_AS` bounds address space, not resident memory, so the watchdog stays the `max_ram_mb` bound. As on
  Windows, an allocation refused inside native code is a closed code that does not name the cap.

## Headroom (measured, both platforms)

Measurements used the real pinned engines (`bge-small-en-v1.5-onnx-q`, `ms-marco-minilm-l6-v2-onnx`, FastEmbed
0.9.1, ONNX Runtime 1.32.0, 2 threads). Each round was a full `max_batch` (32 / 16) of identical texts, three
rounds, read after the run. The Windows 11 host and the 4-vCPU Ubuntu 24.04 VM gave these figures, in MiB:

| Platform, workload | Peak RSS | Peak commit (Windows) / peak address space (Linux) | Gap |
| --- | --- | --- | --- |
| Windows, embed, 200 chars | 295 | 294 | -1 |
| Windows, cross-encoder, 200 chars | 242 | 240 | -2 |
| Windows, embed, 2000 chars | 1219 | 1312 | 93 |
| Windows, cross-encoder, 2000 chars | 795 | 1076 | **281** |
| Linux, embed, 200 chars, glibc default arenas | 319 | 839 | 520 |
| Linux, cross-encoder, 2000 chars, glibc default arenas | 812 | 1920 | **1108** |
| Linux, embed, 200 chars, `MALLOC_ARENA_MAX=2` | 318 | 548 | 230 |
| Linux, cross-encoder, 200 chars, `MALLOC_ARENA_MAX=2` | 251 | 486 | 235 |
| Linux, embed, 2000 chars, `MALLOC_ARENA_MAX=2` | 1240 | 1569 | 329 |
| Linux, cross-encoder, 2000 chars, `MALLOC_ARENA_MAX=2` | 814 | 1338 | **524** |

On Windows, commit is the job's own `PeakJobMemoryUsed`, which equals the sum of the processes' peak pagefile
usage.

Decisions:

- **Linux: `RLIMIT_AS` is workable for the real engine**, but only once glibc's per-thread arenas (64 MiB of
  reserved address space each) are bounded. The worker therefore gets `MALLOC_ARENA_MAX=2` and
  `RAYON_NUM_THREADS=<pin threads>` (the tokenizer's pool otherwise grows with the core count). The largest
  measured gap is then 524 MiB, so `LINUX_ADDRESS_SPACE_HEADROOM_MB = 1024`, about twice that. For the built-in
  pins (`max_ram_mb` 1024) the cap is 2048 MiB, and both engines ran under it on the VM. `RLIMIT_DATA` would
  allow a tighter cap (the data-segment gap was 48 to 343 MiB). It was not chosen, because it does not count
  file or shared mappings, and `RLIMIT_AS` is what the OCR pack already uses. A cgroup v2 `MemoryMax`
  (`systemd-run --user`) also works without privilege on the VM (the user manager delegates `cpu memory pids`).
  It bounds real memory instead of address space, but it needs a systemd user session (`Linger=no` there), so
  it is an option, not the default.
- **Windows: `WINDOWS_COMMIT_HEADROOM_MB = 512`.** The largest measured gap between commit and RSS was
  281 MiB. The built-in pins get a 1536 MiB job.
- The cap is a hard ceiling **above** `max_ram_mb`. The watchdog still kills at `max_ram_mb` of RSS. The cap is
  what a fast spike between two 100 ms polls, or a dead watchdog thread, cannot pass.

Finding for the owner, which this PR does not change: at the pins' own maximum bounds (32 texts of 2000
characters), the embedder's peak RSS is about 1220 to 1240 MiB. That is above its `max_ram_mb` of 1024, so the
worker's watchdog kills that workload (`MODEL_WORKER_MEMORY_LIMIT`). The in-process path has no memory bound,
so it does not notice. Lowering `max_batch` or raising `max_ram_mb` is a pin change, and pin changes are owner
decisions.

## Network isolation: options and recommendation

The table records what was checked on this host and VM. "Unprivileged" means no administrator, no root and no
capability.

| Option | Platform | Privilege | Status here | Residual risk |
| --- | --- | --- | --- | --- |
| In-process socket refusal (existing, extended) | both | none | implemented | Native code (ONNX Runtime, tokenizers) and the `_socket` type bypass it. Pinned by the probe test: on Windows a raw `_socket` connection to a local listener still **succeeds** |
| seccomp filter on `socket()` (raw BPF with ctypes) | Linux | none (`no_new_privs`) | **implemented**, tested on the VM | No new socket of any family, so no Unix-socket broker either (D-Bus, resolved). Remaining: filesystem writes and `ptrace` of the user's own processes when Yama `ptrace_scope` is 0 (Ubuntu's default is 1: descendants only, which inherit the filter). Those are not network |
| libseccomp / `python3-seccomp` | Linux | none | `libseccomp.so.2` is present but the Python binding is not, so it would be a new dependency | same as above |
| Network namespace through a user namespace (`unshare(CLONE_NEWUSER\|CLONE_NEWNET)`) | Linux | none, if policy allows | `unshare -Un` succeeded on the VM. `unshare -rn` failed (`write /proc/self/uid_map: Operation not permitted`) under `apparmor_restrict_unprivileged_userns=1` | Depends on AppArmor and sysctl policy and on container seccomp profiles. Unix sockets remain reachable, so a D-Bus broker can still reach the network. Could be layered on the filter; adds little |
| bubblewrap | Linux | none (user namespaces) or setuid | not installed on the VM | new system dependency; same namespace policy |
| `systemd-run --user -p IPAddressDeny=any` | Linux | none | **not enforced**: a connection to 127.0.0.1:22 succeeded inside the scope (the user manager cannot attach the cgroup BPF program) | needs the system manager, which means root or polkit |
| Landlock (ABI 4: TCP bind and connect) | Linux, kernel 6.7 or later | none | not implemented | TCP only (no UDP, no Unix). A good next step for **filesystem** confinement |
| Windows Firewall outbound rule per program | Windows | administrator | not implemented | Rules match an executable path. The worker is `python.exe`, shared with every other Python use, so this needs a dedicated interpreter copy. Brokered traffic (BITS, WinHTTP through other services) is not covered |
| Windows Filtering Platform filter | Windows | administrator (BFE) | not implemented | strongest host filter, but it needs an installer component and admin |
| AppContainer (LowBox token, no `internetClient` capabilities) | Windows | **none** | not implemented | Needs (1) a CreateProcess path with `STARTUPINFOEX` and `SECURITY_CAPABILITIES` (`subprocess` cannot pass it; ctypes `CreateProcessW` with our own pipes) and (2) a one-time read and execute ACL grant for the AppContainer SID on the Python installation, the venv and the scratch directory (the per-user Python install here; a Program Files install needs admin once). It also confines file system and object access |

**Recommendation:**

- **Linux:** keep the implemented seccomp filter as the worker's OS network control (unprivileged, every
  socket family, inherited by children). Landlock for filesystem confinement is the next hardening step. A
  network namespace is optional defense in depth where the distribution allows it.
- **Windows:** **owner decision required.** Options:
  - (A) Recommended: launch the worker in an AppContainer without network capabilities. It is unprivileged
    at run time and needs a one-time ACL grant per installation.
  - (B) An administrator-installed firewall or WFP rule for a dedicated interpreter copy.
  - (C) Accept the residual and keep the Windows worker at local-supervised dogfood only.

  The job's process seal already stops the worker from starting a helper process. What remains is a socket
  opened by native code inside the worker, or a request to an out-of-process broker over COM or RPC.

**Before any default-on proposal:** Windows network isolation (A or B) is in place, the pin finding above is
resolved, and the owner accepts the residual table.

## Evidence

- `tests/packs/document_intelligence/test_di_worker_isolation.py`:
  - memory: an allocation of cap + 512 MiB is refused by the kernel (`MODEL_WORKER_MEMORY_REFUSED`). On Windows
    the job's peak stays under the limit. The next call starts a fresh capped worker. An allocation under the cap
    but over `max_ram_mb` is still the watchdog's (`MODEL_WORKER_MEMORY_LIMIT`).
  - containment, Windows: the whole tree is in a sealed job. Closing the job handle alone ends the tree. A job
    that cannot be created fails closed and removes the private copy. A worker that cannot be assigned is still
    suspended when it is terminated.
  - containment, Linux: the worker's `/proc` shows the limit, `Seccomp: 2` and `NoNewPrivs: 1`. A shortfall
    seen from outside fails closed.
  - fail-closed paths: an unsupported platform fails closed. A malformed isolation setting is refused before
    anything applies.
  - the filter program's shape on x86_64 and aarch64.
  - network probe, with OS isolation on and off. The environment is exact (a proxy, a token and a user variable
    set in the parent do not reach the worker), and the offline variables are set. Python-level connect and
    resolve are refused in-process. On Linux a raw socket and a child's socket fail with `EACCES`. On Windows the
    raw connection **succeeds** (the residual) and starting a process fails with `ERROR_NOT_ENOUGH_QUOTA` (1816).
    With isolation off both are allowed, which shows the OS layer is what blocks them.
- `tests/packs/document_intelligence/test_di_worker_default_off.py`:
  - the engines default to the in-process factories.
  - no module under `src/` or `scripts/` references the worker.
  - no Document Intelligence module or model script imports a model-conversion toolchain (Optimum, ONNX, torch,
    transformers, the ONNX Runtime quantization and transformers tools, tf2onnx, OpenVINO, CTranslate2).
  - the fetch script imports the hub client only inside its operator action, which requires `--approve`.
  - the governed Document Intelligence workflow runs end to end with `subprocess.Popen` forbidden.
- Results:
  - Windows 11 host: all Document Intelligence worker tests pass. The four Linux-only tests are skipped, each
    with its reason.
  - Ubuntu 24.04 VM, offline venv with this branch: 135 passed and 4 skipped (the Windows-only tests) in
    `tests/packs/document_intelligence`. This includes live parity of the isolated worker with the in-process
    engines over the real pinned models.
