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
  worker when its resident memory passes the pin's ``max_ram_mb`` (polled; Linux additionally drops core dumps and
  caps open files). This is a monitored bound, not a kernel-enforced cap: a Windows Job Object or a cgroup is the
  production follow-up.
* **Network policy.** The worker runs with an empty environment plus ``HF_HUB_OFFLINE`` / ``TRANSFORMERS_OFFLINE``
  (no proxies, tokens or user variables) and refuses any socket connection in-process before the engine is
  imported. That is defense in depth, not OS isolation (a network namespace or firewall rule is an owner decision).
* **Cleanup.** The worker is killed on close, on garbage collection and at interpreter exit; its private model copy
  is removed.

Protocol: one JSON object per line on stdin and stdout. Requests ``{"op": "embed", "texts": [...]}`` and
``{"op": "rerank", "query": ..., "texts": [...]}``; replies ``{"ok": true, "result": ...}`` or
``{"ok": false, "code": "<CLOSED_CODE>"}``. The worker never echoes input text.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import IO, Any, Final

from pados.packs.document_intelligence.local_models import LocalModelError
from pados.packs.document_intelligence.materialize import MaterializeError, discard, materialize_verified
from pados.packs.document_intelligence.model_pin import ModelPin, ModelRole

MAX_LINE_BYTES: Final = 32 * 1024 * 1024
WATCHDOG_INTERVAL: Final = 0.1
ENGINES: Final = ("fastembed", "hashing-test-engine")
_LIVE: weakref.WeakSet[SubprocessModelBackend] = weakref.WeakSet()


# --------------------------------------------------------------------------------------------- worker side
def _deny_network() -> None:
    import socket

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("network disabled in the PADOS model worker")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign,assignment]
    socket.create_connection = refuse  # type: ignore[assignment]


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


def worker_main(argv: list[str]) -> int:
    """``python -I -m pados.packs.document_intelligence.model_worker <pin.json> <model dir> <engine>``."""
    _deny_network()
    pin = ModelPin.model_validate_json(Path(argv[0]).read_text(encoding="utf-8"))
    directory, engine = Path(argv[1]), argv[2]
    from pados.packs.document_intelligence.model_pin import verify_model_dir

    if engine not in ENGINES or verify_model_dir(pin, directory) is not None:
        _reply(sys.stdout, {"ok": False, "code": "MODEL_WORKER_IDENTITY_REFUSED"})
        return 2
    try:
        backend = _load(engine, pin.role.value, pin.model_name, directory, pin.threads, pin.dimension or 0)
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
            else:
                raise ValueError
        except Exception:  # noqa: BLE001 - closed code, no input or engine text
            _reply(sys.stdout, {"ok": False, "code": "LOCAL_MODEL_FAILED"})
            continue
        _reply(sys.stdout, {"ok": True, "result": result})
    return 0


# --------------------------------------------------------------------------------------------- parent side
def _worker_environment(threads: int) -> dict[str, str]:
    environment = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "OMP_NUM_THREADS": str(threads),
                   "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"):  # what the interpreter itself needs to start
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _limits(max_open_files: int) -> None:  # POSIX preexec: no core dumps, bounded descriptors
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (max_open_files, max_open_files))


class SubprocessModelBackend:
    """Parent handle of one model worker. Not thread-safe: the pinned engines already serialize calls."""

    def __init__(self, pin: ModelPin, source: Path, *, engine: str = "fastembed", deadline: float | None = None,
                 scratch: Path | None = None) -> None:
        if engine not in ENGINES:
            raise LocalModelError("MODEL_WORKER_ENGINE_UNKNOWN")
        self.pin, self.source, self.engine = pin, source, engine
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
        self._holder: dict[str, Any] = {"process": None, "copy": None}
        return self._holder

    @staticmethod
    def _finalize(holder: dict[str, Any]) -> None:
        process = holder.get("process")
        if process is not None and process.poll() is None:
            _kill(process)
        if holder.get("copy") is not None:
            discard(holder["copy"])

    @property
    def worker_pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def model_dir(self) -> Path | None:
        return self._copy

    def _start(self) -> None:
        try:
            self._copy = materialize_verified(self.pin, self.source, parent=self._scratch)
        except MaterializeError as exc:
            raise LocalModelError(exc.code) from None
        self._holder["copy"] = self._copy
        pin_file = self._copy.parent / f"{self._copy.name}.pin.json"
        pin_file.write_text(self.pin.model_dump_json(by_alias=True), encoding="utf-8")
        argv = [sys.executable, "-I", "-m", "pados.packs.document_intelligence.model_worker", str(pin_file),
                str(self._copy), self.engine]
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True
            options["preexec_fn"] = lambda: _limits(256)
        self._process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, env=_worker_environment(self.pin.threads),
                                         cwd=str(self._copy), text=True, encoding="utf-8", bufsize=1, **options)
        self._holder["process"] = self._process
        self._lines = queue.Queue()
        threading.Thread(target=self._read, args=(self._process.stdout, self._lines), daemon=True,
                         name=f"pados-model-worker-reader-{self.pin.pin_id}").start()
        threading.Thread(target=self._watch, args=(self._process,), daemon=True,
                         name=f"pados-model-worker-watchdog-{self.pin.pin_id}").start()
        ready = self._receive(max(self.deadline, self.pin.timeout_seconds * 5))
        if ready != "READY":
            self.close()
            raise LocalModelError("MODEL_WORKER_START_FAILED")

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
        import psutil  # type: ignore[import-untyped]  # declared runtime dependency; no stubs are pinned

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
        if self._copy is not None:
            discard(self._copy)
            pin_file = self._copy.parent / f"{self._copy.name}.pin.json"
            pin_file.unlink(missing_ok=True)
            self._copy = None
            self._holder["copy"] = None


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


def subprocess_factory(engine: str = "fastembed", *, scratch: Path | None = None) -> Any:
    """A ``factory`` for ``PinnedTextEmbedder`` / ``PinnedCrossEncoder`` that runs the model in a worker process."""
    def factory(pin: ModelPin, directory: Path) -> SubprocessModelBackend:
        return SubprocessModelBackend(pin, directory, engine=engine, scratch=scratch)
    return factory


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1:]))
