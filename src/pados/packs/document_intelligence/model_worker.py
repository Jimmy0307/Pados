# SPDX-License-Identifier: AGPL-3.0-only
"""DI-1 candidate: run a pinned model engine in a separate, killable worker process (opt-in; not a production
boundary until the owner reviews it).

The parent (``SubprocessModelBackend``) presents the same backend interface the in-process engines use
(``embed(documents, batch_size)`` / ``rerank(query, documents, batch_size)``), so it plugs into
``PinnedTextEmbedder`` / ``PinnedCrossEncoder`` as their ``factory``. Every guarantee of those engines (pin, bounds,
per-batch deadline, output validation, closed codes) still applies on top.

What the worker adds:

* **Fixed identity, no hash/load race (DI-2).** The parent materializes a private verified copy of the pinned files
  (``materialize_verified``); the worker is started with only that directory and re-verifies it against the pin
  before loading. The operator's model root is never visible to it.
* **A real kill.** A request that misses its deadline kills the whole worker process group; no runaway thread is
  left behind, and the next call starts a fresh worker.
* **Bounded resources.** One request at a time; request and response lines are size-limited; a watchdog kills the
  worker tree when its resident memory passes the pin's ``max_ram_mb`` (polled every 100 ms:
  ``MODEL_WORKER_MEMORY_LIMIT``). With ``os_isolation`` (the default whenever the worker is used) the kernel also
  enforces a hard ceiling a little above ``max_ram_mb`` (``worker_isolation``): on Windows a Job Object (committed
  memory of the whole tree, kill-on-close, an active-process limit sealed at READY), entered while the worker is
  still suspended; on Linux ``RLIMIT_AS`` plus ``RLIMIT_CORE = 0`` and ``RLIMIT_NOFILE``, which the worker applies
  to itself before it reads anything. An allocation the kernel refuses in Python is ``MODEL_WORKER_MEMORY_REFUSED``
  and the worker exits; one refused inside native code is ``LOCAL_MODEL_FAILED`` or ``MODEL_WORKER_EXITED``.
* **Network policy.** The worker runs with an empty environment plus ``HF_HUB_OFFLINE`` / ``TRANSFORMERS_OFFLINE``
  and thread-count variables (no proxies, tokens or user variables) and refuses Python-level connect, send-to and
  name resolution in-process before the engine is imported (defense in depth: native code bypasses it). On Linux
  ``os_isolation`` adds a seccomp filter: ``socket()`` fails for every family, also in children (kernel-enforced).
  Windows has no OS network control yet; the sealed job only stops it from starting a helper process. AppContainer
  or a firewall rule is an owner decision (``docs/packs/document_intelligence/MODEL_WORKER_ISOLATION.md``).
* **Fail closed.** Isolation that cannot be set up, or that the parent cannot confirm from outside after READY
  (job membership; ``/proc`` limits and seccomp mode), is ``MODEL_WORKER_ISOLATION_FAILED``.
* **Cleanup.** The worker is killed on close, on garbage collection and at interpreter exit (on Windows also when
  the parent's job handle closes); its private model copy is removed.

The worker is opt-in: nothing in PADOS passes ``subprocess_factory`` as an engine factory.

Protocol: one JSON object per line on stdin and stdout. Requests ``{"op": "embed", "texts": [...]}`` and
``{"op": "rerank", "query": ..., "texts": [...]}`` (the test engine alone also answers ``probe``); replies
``{"ok": true, "result": ...}`` or ``{"ok": false, "code": "<CLOSED_CODE>"}``. The worker never echoes input text.
"""
from __future__ import annotations

import atexit
import errno
import json
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from functools import partial
from pathlib import Path
from typing import IO, Any, Final

from pados.packs.document_intelligence.local_models import LocalModelError
from pados.packs.document_intelligence.materialize import MaterializeError, discard, materialize_verified
from pados.packs.document_intelligence.model_pin import ModelPin, ModelRole
from pados.packs.document_intelligence.worker_isolation import (
    CONSOLE_HOST_IMAGE,
    CREATE_SUSPENDED,
    ISOLATION_FAILED,
    MEMORY_REFUSED,
    MALLOC_ARENAS,
    IsolationError,
    WindowsJob,
    apply_worker_limits,
    effective_address_space,
    linux_isolation_observed,
    memory_cap_bytes,
    resume,
    starting_processes,
    supported,
)

MAX_LINE_BYTES: Final = 32 * 1024 * 1024
WATCHDOG_INTERVAL: Final = 0.1
ENGINES: Final = ("fastembed", "hashing-test-engine")
_LIVE: weakref.WeakSet[SubprocessModelBackend] = weakref.WeakSet()
_WORKER_EXITS: Final = (MEMORY_REFUSED, ISOLATION_FAILED, "MODEL_WORKER_IDENTITY_REFUSED")


# --------------------------------------------------------------------------------------------- worker side
NETWORK_REFUSAL: Final = "network disabled in the PADOS model worker"
_REFUSED_METHODS: Final = ("connect", "connect_ex", "sendto", "sendmsg")
_REFUSED_FUNCTIONS: Final = ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                             "gethostbyaddr")


def _deny_network() -> None:
    """In-process refusal (defense in depth): Python-level connect, send-to and name resolution. Native code and
    the ``_socket`` type are NOT covered; on Linux the seccomp filter refuses ``socket()`` itself."""
    import socket

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(NETWORK_REFUSAL)

    for name in _REFUSED_METHODS:
        if hasattr(socket.socket, name):
            setattr(socket.socket, name, refuse)
    for name in _REFUSED_FUNCTIONS:
        setattr(socket, name, refuse)


class _HashingEngine:
    """Deterministic test engine (no model, no dependency). Lets CI exercise the worker boundary itself."""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def _directive(self, text: str) -> None:
        if text.startswith("__sleep__:"):
            time.sleep(float(text.split(":", 1)[1]))
        elif text.startswith("__alloc_mb__:"):
            _hold.append(bytes([1]) * (int(text.split(":", 1)[1]) * 1024 * 1024))  # filled, so the pages are resident
            time.sleep(5)
        elif text == "__crash__":
            os._exit(3)

    def embed(self, documents: list[str], batch_size: int) -> list[list[float]]:
        vectors = []
        for text in documents:
            self._directive(text)
            vector = [0.0] * self.dimension
            for index, char in enumerate(text):
                vector[(ord(char) + index) % self.dimension] += 1.0
            vector[0] += 1.0
            vectors.append(vector)
        return vectors

    def rerank(self, query: str, documents: list[str], batch_size: int) -> list[float]:
        terms = set(query.lower().split())
        for text in documents:
            self._directive(text)
        return [float(len(terms & set(text.lower().split()))) for text in documents]


_hold: list[bytes] = []


def _attempt(action: Any) -> str:
    try:
        action()
    except OSError as exc:
        if NETWORK_REFUSAL in str(exc):
            return "REFUSED_IN_PROCESS"
        winerror = getattr(exc, "winerror", None)
        if winerror:
            return f"REFUSED_BY_OS:WINERROR_{winerror}"
        return f"REFUSED_BY_OS:{errno.errorcode.get(exc.errno or 0, 'UNKNOWN')}"
    return "ALLOWED"


def _probe(port: int) -> dict[str, Any]:
    """Test engine only: what the worker can see and reach. Never reachable with a production engine."""
    import socket

    import _socket

    def raw_connect() -> None:
        handle = _socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # the C type: no in-process refusal
        try:
            handle.settimeout(2)
            handle.connect(("127.0.0.1", port))
        finally:
            handle.close()

    def spawn() -> str:
        """Start a child (no in-process refusal there) that opens a socket: exit 0 opened, 10 refused."""
        code = "import _socket, sys\ntry:\n    _socket.socket(2, 1).close()\nexcept OSError:\n    sys.exit(10)"
        try:
            completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=30,
                                       check=False)
        except OSError as exc:
            return _attempt(partial(_raise, exc))
        return {0: "CHILD_SOCKET_ALLOWED", 10: "CHILD_SOCKET_REFUSED"}.get(completed.returncode,
                                                                          f"CHILD_FAILED:{completed.returncode}")

    return {"environment": sorted(os.environ),
            "offline": {name: os.environ.get(name) for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")},
            "connect": _attempt(lambda: socket.create_connection(("127.0.0.1", port), timeout=2).close()),
            "resolve": _attempt(lambda: socket.getaddrinfo("localhost", port)),
            "raw_connect": _attempt(raw_connect), "spawn": spawn()}


def _raise(exc: BaseException) -> None:
    raise exc


def _load(engine: str, role: str, model_name: str, directory: Path, threads: int, dimension: int) -> Any:
    if engine == "hashing-test-engine":
        return _HashingEngine(dimension or 8)
    import importlib

    arguments = {"model_name": model_name, "specific_model_path": str(directory), "local_files_only": True,
                 "threads": threads, "providers": ["CPUExecutionProvider"], "cuda": False, "lazy_load": False}
    if role == ModelRole.EMBEDDING.value:
        return importlib.import_module("fastembed").TextEmbedding(**arguments)
    return importlib.import_module("fastembed.rerank.cross_encoder").TextCrossEncoder(**arguments)


def _reply(stream: IO[str], payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stream.flush()


def _enter_isolation(setting: str) -> bool:
    """POSIX: the worker limits itself before it reads anything (no ``preexec_fn`` in the parent). ``setting`` is
    ``off`` or the address-space limit in bytes (which also installs the socket filter)."""
    if setting != "off" and not (setting.isdigit() and len(setting) <= 15 and int(setting) > 0):
        return False
    if sys.platform == "win32":
        return setting == "off"  # the parent placed this process in its Job Object before it ran
    try:
        apply_worker_limits(None if setting == "off" else int(setting))
    except IsolationError:
        return False
    return True


def worker_main(argv: list[str]) -> int:
    """``python -I -m pados.packs.document_intelligence.model_worker <pin.json> <model dir> <engine> <isolation>``."""
    if len(argv) != 4 or not _enter_isolation(argv[3]):
        _reply(sys.stdout, {"ok": False, "code": ISOLATION_FAILED})
        return 4
    _deny_network()
    pin = ModelPin.model_validate_json(Path(argv[0]).read_text(encoding="utf-8"))
    directory, engine = Path(argv[1]), argv[2]
    from pados.packs.document_intelligence.model_pin import verify_model_dir

    if engine not in ENGINES or verify_model_dir(pin, directory) is not None:
        _reply(sys.stdout, {"ok": False, "code": "MODEL_WORKER_IDENTITY_REFUSED"})
        return 2
    try:
        backend = _load(engine, pin.role.value, pin.model_name, directory, pin.threads, pin.dimension or 0)
    except MemoryError:
        _reply(sys.stdout, {"ok": False, "code": MEMORY_REFUSED})
        return 5
    except Exception:  # noqa: BLE001 - closed code, no engine text
        _reply(sys.stdout, {"ok": False, "code": "LOCAL_MODEL_FAILED"})
        return 3
    _reply(sys.stdout, {"ok": True, "result": "READY"})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            texts = [str(text) for text in request["texts"]]
            if request["op"] == "embed":
                result: Any = [[float(v) for v in vector] for vector in backend.embed(texts, batch_size=len(texts))]
            elif request["op"] == "rerank":
                result = [float(s) for s in backend.rerank(str(request["query"]), texts, batch_size=len(texts))]
            elif request["op"] == "probe" and engine == "hashing-test-engine":
                result = _probe(int(request["port"]))
            else:
                raise ValueError
        except MemoryError:  # the kernel cap refused an allocation: report it, then exit (state is suspect)
            _hold.clear()
            _reply(sys.stdout, {"ok": False, "code": MEMORY_REFUSED})
            return 5
        except Exception:  # noqa: BLE001 - closed code, no input or engine text
            _reply(sys.stdout, {"ok": False, "code": "LOCAL_MODEL_FAILED"})
            continue
        _reply(sys.stdout, {"ok": True, "result": result})
    return 0


# --------------------------------------------------------------------------------------------- parent side
def _worker_environment(threads: int) -> dict[str, str]:
    environment = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "OMP_NUM_THREADS": str(threads),
                   "RAYON_NUM_THREADS": str(threads), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if sys.platform != "win32":  # glibc reserves 64 MiB of address space per thread arena; bounded under RLIMIT_AS
        environment["MALLOC_ARENA_MAX"] = str(MALLOC_ARENAS)
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"):  # what the interpreter itself needs to start
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


class SubprocessModelBackend:
    """Parent handle of one model worker. Not thread-safe: the pinned engines already serialize calls."""

    def __init__(self, pin: ModelPin, source: Path, *, engine: str = "fastembed", deadline: float | None = None,
                 scratch: Path | None = None, os_isolation: bool = True) -> None:
        if engine not in ENGINES:
            raise LocalModelError("MODEL_WORKER_ENGINE_UNKNOWN")
        if os_isolation and not supported():
            raise LocalModelError(ISOLATION_FAILED)
        self.pin, self.source, self.engine = pin, source, engine
        self.os_isolation = os_isolation  # False only for diagnosis: the RSS watchdog alone remains
        self._job: WindowsJob | None = None
        self.deadline = deadline if deadline is not None else max(0.5, pin.timeout_seconds * 0.9)
        self._scratch = scratch
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._copy: Path | None = None
        self._killed_for: str | None = None
        _LIVE.add(self)
        weakref.finalize(self, SubprocessModelBackend._finalize, self._state())
        self._start()  # eagerly: the engine's factory call runs under its (longer) load deadline

    def _state(self) -> dict[str, Any]:
        self._holder: dict[str, Any] = {"process": None, "copy": None, "job": None}
        return self._holder

    @staticmethod
    def _finalize(holder: dict[str, Any]) -> None:
        process = holder.get("process")
        if process is not None and process.poll() is None:
            _kill(process)
        if holder.get("job") is not None:
            holder["job"].close()
        if holder.get("copy") is not None:
            discard(holder["copy"])

    @property
    def worker_pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def model_dir(self) -> Path | None:
        return self._copy

    @property
    def job(self) -> WindowsJob | None:
        """The worker's Job Object (Windows with OS isolation), for inspection."""
        return self._job

    @property
    def address_space(self) -> int | None:
        """The ``RLIMIT_AS`` the worker runs under (Linux with OS isolation)."""
        if self.os_isolation and sys.platform.startswith("linux"):
            return effective_address_space(self.pin.max_ram_mb)
        return None

    def _start(self) -> None:
        try:
            self._copy = materialize_verified(self.pin, self.source, parent=self._scratch)
        except MaterializeError as exc:
            raise LocalModelError(exc.code) from None
        self._holder["copy"] = self._copy
        pin_file = self._copy.parent / f"{self._copy.name}.pin.json"
        pin_file.write_text(self.pin.model_dump_json(by_alias=True), encoding="utf-8")
        address_space = self.address_space
        argv = [sys.executable, "-I", "-m", "pados.packs.document_intelligence.model_worker", str(pin_file),
                str(self._copy), self.engine, "off" if address_space is None else str(address_space)]
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            if self.os_isolation:
                options["creationflags"] |= CREATE_SUSPENDED  # nothing runs before it is inside the job
                self._job = self._isolated(lambda: WindowsJob(memory_cap_bytes(self.pin.max_ram_mb),
                                                              starting_processes()))
                self._holder["job"] = self._job
        else:
            options["start_new_session"] = True  # no preexec_fn: the worker applies its limits itself
        self._process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=_worker_environment(self.pin.threads),
                                         cwd=str(self._copy), text=True, encoding="utf-8", bufsize=1, **options)
        self._holder["process"] = self._process
        if self._job is not None:
            self._isolated(partial(_enter_job, self._job, self._process.pid))
        self._lines = queue.Queue()
        threading.Thread(target=self._read, args=(self._process.stdout, self._lines), daemon=True,
                         name=f"pados-model-worker-reader-{self.pin.pin_id}").start()
        threading.Thread(target=self._watch, args=(self._process,), daemon=True,
                         name=f"pados-model-worker-watchdog-{self.pin.pin_id}").start()
        try:
            ready = self._receive(max(self.deadline, self.pin.timeout_seconds * 5))
        except LocalModelError:
            self.close()
            raise
        if ready != "READY":
            self.close()
            raise LocalModelError("MODEL_WORKER_START_FAILED")
        self._isolated(self._confirm_isolation)

    def _isolated(self, step: Any) -> Any:
        """Run an isolation step; any failure closes the worker and is ``MODEL_WORKER_ISOLATION_FAILED``."""
        try:
            return step()
        except (IsolationError, OSError):
            self.close()
            raise LocalModelError(ISOLATION_FAILED) from None

    def _confirm_isolation(self) -> None:
        """After READY, re-read from outside what the kernel enforces on the worker; refuse any shortfall."""
        process = self._process
        if not self.os_isolation or process is None:
            return
        if self._job is not None:
            import psutil  # type: ignore[import-untyped]  # declared runtime dependency; no stubs are pinned

            root = psutil.Process(process.pid)
            tree = [root, *root.children(recursive=True)]
            if not all(self._job.contains(member.pid) for member in tree):
                raise IsolationError("PROCESS_OUTSIDE_JOB")
            self._job.seal(sum(member.name().lower() != CONSOLE_HOST_IMAGE for member in tree))
        elif linux_isolation_observed(process.pid) != (self.address_space, "2"):
            raise IsolationError("LINUX_LIMITS_NOT_OBSERVED")

    @staticmethod
    def _read(stream: IO[str] | None, sink: queue.Queue[str | None]) -> None:
        if stream is None:
            sink.put(None)
            return
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                sink.put(None)
                return
            sink.put(line)

    def _watch(self, process: subprocess.Popen[str]) -> None:
        import psutil

        limit = self.pin.max_ram_mb * 1024 * 1024
        try:
            handle = psutil.Process(process.pid)
            while process.poll() is None:
                # The whole tree: on Windows a venv python.exe is a launcher whose child is the real interpreter.
                tree = [handle, *handle.children(recursive=True)]
                if sum(member.memory_info().rss for member in tree) > limit:
                    self._killed_for = "MODEL_WORKER_MEMORY_LIMIT"
                    _kill(process)
                    return
                time.sleep(WATCHDOG_INTERVAL)
        except (psutil.Error, OSError):
            return

    def _receive(self, deadline: float) -> Any:
        try:
            line = self._lines.get(timeout=deadline)
        except queue.Empty:
            self._killed_for = "LOCAL_MODEL_TIMEOUT"
            self.close()
            raise LocalModelError("LOCAL_MODEL_TIMEOUT", retryable=True) from None
        if line is None:
            code = self._killed_for or "MODEL_WORKER_EXITED"
            self.close()
            raise LocalModelError(code)
        if len(line) > MAX_LINE_BYTES:
            self.close()
            raise LocalModelError("MODEL_WORKER_OUTPUT_TOO_LARGE")
        try:
            reply = json.loads(line)
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or reply.get("ok") not in (True, False):
            self.close()
            raise LocalModelError("MODEL_WORKER_PROTOCOL_INVALID")
        if reply["ok"] is not True:
            code = str(reply.get("code", "LOCAL_MODEL_FAILED"))
            if code in _WORKER_EXITS:
                self.close()  # the worker exits after these: reap it now, the next call starts a fresh one
            raise LocalModelError(code if code.isupper() and len(code) <= 64 else "LOCAL_MODEL_FAILED")
        return reply.get("result")

    def _call(self, request: dict[str, Any]) -> Any:
        if self._process is None or self._process.poll() is not None:
            self.close()
            self._killed_for = None
            self._start()
        line = json.dumps(request, separators=(",", ":"))
        if len(line) > MAX_LINE_BYTES:
            raise LocalModelError("LOCAL_MODEL_INPUT_TOO_LARGE")
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            process.stdin.write(line + "\n")
            process.stdin.flush()
        except OSError:
            code = self._killed_for or "MODEL_WORKER_EXITED"
            self.close()
            raise LocalModelError(code) from None
        return self._receive(self.deadline)

    def embed(self, documents: list[str], batch_size: int) -> list[list[float]]:
        result = self._call({"op": "embed", "texts": list(documents)})
        if not isinstance(result, list):
            raise LocalModelError("MODEL_WORKER_PROTOCOL_INVALID")
        return [list(vector) for vector in result]

    def rerank(self, query: str, documents: list[str], batch_size: int) -> list[float]:
        result = self._call({"op": "rerank", "query": query, "texts": list(documents)})
        if not isinstance(result, list):
            raise LocalModelError("MODEL_WORKER_PROTOCOL_INVALID")
        return [float(score) for score in result]

    def close(self) -> None:
        process, self._process = self._process, None
        self._holder["process"] = None
        if process is not None:
            _kill(process)
        job, self._job = self._job, None
        self._holder["job"] = None
        if job is not None:
            job.close()  # kill-on-close: nothing of the worker outlives its job
        if self._copy is not None:
            discard(self._copy)
            pin_file = self._copy.parent / f"{self._copy.name}.pin.json"
            pin_file.unlink(missing_ok=True)
            self._copy = None
            self._holder["copy"] = None


def _enter_job(job: WindowsJob, pid: int) -> None:
    """Assign a worker created suspended to its job, then let it run (Windows)."""
    job.assign(pid)
    resume(pid)


def _kill(process: subprocess.Popen[str]) -> None:
    import psutil

    try:
        parent = psutil.Process(process.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except psutil.Error:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


@atexit.register
def _close_all() -> None:
    for backend in list(_LIVE):
        backend.close()


def subprocess_factory(engine: str = "fastembed", *, scratch: Path | None = None, os_isolation: bool = True) -> Any:
    """A ``factory`` for ``PinnedTextEmbedder`` / ``PinnedCrossEncoder`` that runs the model in a worker process.
    Nothing in PADOS calls this by default: an operator opts in by passing it as the engine's ``factory``."""
    def factory(pin: ModelPin, directory: Path) -> SubprocessModelBackend:
        return SubprocessModelBackend(pin, directory, engine=engine, scratch=scratch, os_isolation=os_isolation)
    return factory


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1:]))
