# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned local model engines: a dense text embedder and a cross-encoder, behind closed-code boundaries.

The shipped backend is FastEmbed (Apache-2.0) over ONNX Runtime (MIT) on the CPU execution provider. It is an
OPTIONAL dependency (``pip install "pados[document-intelligence]"``); without it, or without an operator
directory matching the pin, the engine reports a refusal from ``status()`` and is never loaded.

Guarantees:

* **No download.** The engine is constructed with ``specific_model_path`` (FastEmbed returns that directory
  before any hub code runs), ``local_files_only=True`` and ``HF_HUB_OFFLINE=1``, and only after
  ``verify_model_dir`` passes. Verification is cached on a stat fingerprint of the directory; any change
  re-verifies and unloads. (The files are hashed, then reopened by the engine: keep the model root writable only
  by the operator. Residual DI-2.)
* **Bounded.** At most ``MAX_TEXTS`` inputs and ``MAX_TOTAL_CHARS`` characters per call; each input is cut to the
  pin's ``max_input_chars`` (the count is reported); the engine runs with the pin's thread count.
* **Deadline.** Each batch of ``max_batch`` inputs runs on a daemon thread and must finish within the pin's
  ``timeout_seconds``; a load gets ``LOAD_DEADLINE_FACTOR`` times that. CPython cannot stop the thread, so a late
  batch makes the engine refuse (``ENGINE_POISONED``) until that computation has returned; it then reloads.
  At most one runaway computation exists per engine, and it never blocks interpreter exit.
* **Validated output.** Count, dimension and finiteness are checked; embeddings are L2-normalized; every number is
  quantized to 6 decimals so the result schema is stable. Any engine exception becomes ``LOCAL_MODEL_FAILED``
  with an empty exception chain (input text is never echoed).

``CrossEncoderReranker`` presents a cross-encoder as the existing ``pados.enterprise.rerank.Reranker`` so the
existing DISABLED / SHADOW / ACTIVE ``rerank`` function decides how its scores are used.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import math
import os
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Final, Protocol

from pados.enterprise.compute import ResourceClass
from pados.enterprise.manifest import CapabilityHealth
from pados.enterprise.rerank import RerankCandidate, RerankError
from pados.packs.document_intelligence.model_pin import ModelPin, ModelRole, verify_model_dir

MAX_TEXTS: Final = 4096
MAX_TOTAL_CHARS: Final = 1_000_000  # per call, after per-text truncation
LOAD_DEADLINE_FACTOR: Final = 5
QUANTUM_DIGITS: Final = 6
PROVIDERS: Final = ("CPUExecutionProvider",)
MODEL_ROOT_ENV: Final = "PADOS_MODEL_ROOT"


class LocalModelError(RuntimeError):
    """Closed-code engine failure (never carries input or engine text)."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class EngineIdentity:
    engine: str
    engine_version: str
    runtime: str
    runtime_version: str
    providers: tuple[str, ...]
    device: str
    threads: int

    def label(self) -> str:
        return f"{self.engine}@{self.engine_version}+{self.runtime}@{self.runtime_version}:{self.device}"


class EmbeddingBackend(Protocol):
    def embed(self, documents: list[str], batch_size: int) -> Iterable[Sequence[float]]: ...


class CrossEncoderBackend(Protocol):
    def rerank(self, query: str, documents: list[str], batch_size: int) -> Iterable[float]: ...


Factory = Callable[[ModelPin, Path], Any]


def default_model_root() -> Path:
    """The operator's model directory: ``PADOS_MODEL_ROOT``, else ``%LOCALAPPDATA%/pados/models`` or
    ``~/.local/share/pados/models``. Only ever read; PADOS never writes or downloads into it."""
    configured = os.environ.get(MODEL_ROOT_ENV)
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "pados" / "models" if local else Path.home() / ".local" / "share" / "pados" / "models"


def fastembed_identity(threads: int) -> EngineIdentity | None:
    """The installed engine's identity, or ``None`` when FastEmbed or ONNX Runtime is not installed."""
    if importlib.util.find_spec("fastembed") is None or importlib.util.find_spec("onnxruntime") is None:
        return None
    try:
        return EngineIdentity(engine="fastembed", engine_version=importlib.metadata.version("fastembed"),
                              runtime="onnxruntime", runtime_version=importlib.metadata.version("onnxruntime"),
                              providers=PROVIDERS, device="cpu", threads=threads)
    except importlib.metadata.PackageNotFoundError:
        return None


def _offline() -> None:
    """Belt and braces beside ``specific_model_path``: the hub client in this process stays offline."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _fastembed_kwargs(pin: ModelPin, directory: Path) -> dict[str, Any]:
    _offline()
    return {"model_name": pin.model_name, "specific_model_path": str(directory), "local_files_only": True,
            "threads": pin.threads, "providers": list(PROVIDERS), "cuda": False, "lazy_load": False}


def fastembed_embedding(pin: ModelPin, directory: Path) -> Any:
    """An ``EmbeddingBackend`` (imported dynamically: the engine is an optional dependency)."""
    return importlib.import_module("fastembed").TextEmbedding(**_fastembed_kwargs(pin, directory))


def fastembed_cross_encoder(pin: ModelPin, directory: Path) -> Any:
    """A ``CrossEncoderBackend`` (imported dynamically: the engine is an optional dependency)."""
    module = importlib.import_module("fastembed.rerank.cross_encoder")
    return module.TextCrossEncoder(**_fastembed_kwargs(pin, directory))


_AUTO: Final = object()


class _PinnedEngine:
    role: ModelRole

    def __init__(self, pin: ModelPin, directory: Path, *, factory: Factory,
                 identity: EngineIdentity | None | object = _AUTO, private_copy: bool = False) -> None:
        self.private_copy = private_copy  # DI-2: load from a verified private copy, never the operator directory
        self._copy: Path | None = None
        self.pin = pin
        self.directory = directory
        self._factory = factory
        self.identity: EngineIdentity | None = (fastembed_identity(pin.threads) if identity is _AUTO
                                                else identity)  # type: ignore[assignment]
        self._backend: Any = None
        self._runaway: threading.Thread | None = None
        self._checked: tuple[tuple[Any, ...], str | None] | None = None
        self._lock = threading.Lock()

    def _fingerprint(self) -> tuple[Any, ...]:
        """Cheap identity of the directory's current state: every file's path, size, mtime and file id."""
        entries: list[tuple[str, int, int, int]] = []
        try:
            for path in sorted(self.directory.rglob("*")):
                info = path.lstat()
                entries.append((path.as_posix(), info.st_size, info.st_mtime_ns, info.st_ino))
                if len(entries) > 1024:
                    break
        except OSError:
            return ("UNREADABLE",)
        return tuple(entries)

    def status(self) -> str | None:
        """``None`` when the engine may run; otherwise the closed reason it may not.

        Verification is cached on a stat fingerprint of the model directory (sizes, mtimes, file ids): unchanged
        files are not re-hashed, and any change re-verifies before the next use and unloads a loaded engine whose
        files no longer match."""
        if self._runaway is not None:
            if self._runaway.is_alive():
                return "ENGINE_POISONED"
            self._runaway = None  # the late call has returned: nothing is computing any more; reload cleanly
            self._backend = None
        if self.identity is None:
            return "ENGINE_NOT_INSTALLED"
        if self.pin.role is not self.role:
            return "MODEL_PIN_ROLE_MISMATCH"
        fingerprint = self._fingerprint()
        if self._checked is None or self._checked[0] != fingerprint:
            self._checked = (fingerprint, verify_model_dir(self.pin, self.directory))
        if self._checked[1] is not None:
            self._backend = None
        return self._checked[1]

    def _run(self, call: Callable[[], Any], *, deadline: float) -> Any:
        """Run ``call`` on a fresh DAEMON thread (it never blocks interpreter exit) within ``deadline`` seconds."""
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["value"] = call()
            except BaseException as exc:  # noqa: BLE001 - recorded, mapped to a closed code below
                box["error"] = exc

        worker = threading.Thread(target=target, name=f"pados-model-{self.pin.pin_id}", daemon=True)
        worker.start()
        worker.join(deadline)
        if worker.is_alive():
            self._runaway = worker  # cannot be stopped: refuse until it returns (status() then recovers)
            failure = "LOCAL_MODEL_TIMEOUT"
        elif isinstance(box.get("error"), LocalModelError):
            raise box["error"]
        elif "error" in box:
            failure = "LOCAL_MODEL_FAILED"
        else:
            return box.get("value")
        raise LocalModelError(failure) from None

    def _backend_ready(self) -> Any:
        reason = self.status()
        if reason is not None:
            raise LocalModelError(reason)
        if self._backend is None:
            # A load reads ~100 MB and builds the session: it gets its own, longer deadline.
            source = self._materialize() if self.private_copy else self.directory
            loaded = self._run(lambda: self._factory(self.pin, source),
                               deadline=self.pin.timeout_seconds * LOAD_DEADLINE_FACTOR)
            if loaded is None:
                raise LocalModelError("LOCAL_MODEL_FAILED")
            self._backend = loaded
        return self._backend

    def _materialize(self) -> Path:
        from pados.packs.document_intelligence.materialize import MaterializeError, discard, materialize_verified

        if self._copy is not None:
            discard(self._copy)
            self._copy = None
        try:
            self._copy = materialize_verified(self.pin, self.directory)
        except MaterializeError as exc:
            raise LocalModelError(exc.code) from None
        return self._copy

    def close(self) -> None:
        """Release the engine: a worker backend is stopped and a private model copy removed (idempotent)."""
        backend, self._backend = self._backend, None
        if backend is not None and callable(getattr(backend, "close", None)):
            backend.close()
        if self._copy is not None:
            from pados.packs.document_intelligence.materialize import discard

            discard(self._copy)
            self._copy = None

    def _batches(self, texts: list[str]) -> list[list[str]]:
        size = self.pin.max_batch
        return [texts[index:index + size] for index in range(0, len(texts), size)]

    def _bounded(self, texts: Sequence[str]) -> tuple[list[str], int]:
        if (not isinstance(texts, Sequence) or isinstance(texts, str) or not 1 <= len(texts) <= MAX_TEXTS
                or any(not isinstance(text, str) for text in texts)):
            raise LocalModelError("LOCAL_MODEL_INPUT_INVALID")
        limit = self.pin.max_input_chars
        bounded = [text[:limit] for text in texts]
        if sum(len(text) for text in bounded) > MAX_TOTAL_CHARS:
            raise LocalModelError("LOCAL_MODEL_INPUT_TOO_LARGE")
        return bounded, sum(1 for text in texts if len(text) > limit)


def _embed_batch(backend: Any, batch: list[str]) -> list[list[float]]:
    return [list(vector) for vector in backend.embed(batch, batch_size=len(batch))]


def _rerank_batch(backend: Any, query: str, batch: list[str]) -> list[float]:
    return [float(score) for score in backend.rerank(query, batch, batch_size=len(batch))]


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    truncated: int


def _unit(values: Sequence[float], dimension: int) -> tuple[float, ...]:
    numbers = [float(value) for value in values]
    if len(numbers) != dimension:
        raise LocalModelError("LOCAL_MODEL_DIMENSION_MISMATCH")
    if not all(math.isfinite(value) for value in numbers):
        raise LocalModelError("LOCAL_MODEL_OUTPUT_INVALID")
    norm = math.sqrt(sum(value * value for value in numbers))
    if not norm:
        raise LocalModelError("LOCAL_MODEL_OUTPUT_INVALID")
    return tuple(round(value / norm, QUANTUM_DIGITS) for value in numbers)


class PinnedTextEmbedder(_PinnedEngine):
    role = ModelRole.EMBEDDING

    def __init__(self, pin: ModelPin, directory: Path, *, factory: Factory = fastembed_embedding,
                 identity: EngineIdentity | None | object = _AUTO, private_copy: bool = False) -> None:
        super().__init__(pin, directory, factory=factory, identity=identity, private_copy=private_copy)

    def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        bounded, truncated = self._bounded(texts)
        dimension = self.pin.dimension
        assert dimension is not None  # an EMBEDDING pin always has one (validated)
        with self._lock:
            backend = self._backend_ready()
            raw: list[list[float]] = []
            for batch in self._batches(bounded):  # the deadline applies per batch, never to the whole call
                raw += self._run(partial(_embed_batch, backend, batch), deadline=self.pin.timeout_seconds)
        if len(raw) != len(bounded):
            raise LocalModelError("LOCAL_MODEL_OUTPUT_INVALID")
        return EmbeddingResult(tuple(_unit(vector, dimension) for vector in raw), truncated)


class PinnedCrossEncoder(_PinnedEngine):
    role = ModelRole.CROSS_ENCODER

    def __init__(self, pin: ModelPin, directory: Path, *, factory: Factory = fastembed_cross_encoder,
                 identity: EngineIdentity | None | object = _AUTO, private_copy: bool = False) -> None:
        super().__init__(pin, directory, factory=factory, identity=identity, private_copy=private_copy)

    def score(self, query: str, texts: Sequence[str]) -> tuple[float, ...]:
        (bounded_query,), _ = self._bounded([query])
        bounded, _ = self._bounded(texts)
        with self._lock:
            backend = self._backend_ready()
            raw: list[float] = []
            for batch in self._batches(bounded):
                raw += self._run(partial(_rerank_batch, backend, bounded_query, batch),
                                 deadline=self.pin.timeout_seconds)
        if len(raw) != len(bounded) or not all(math.isfinite(score) for score in raw):
            raise LocalModelError("LOCAL_MODEL_OUTPUT_INVALID")
        return tuple(round(score, QUANTUM_DIGITS) for score in raw)


class CrossEncoderReranker:
    """A pinned cross-encoder presented as the existing enterprise ``Reranker`` protocol."""

    reranker_id = "pados.rerank.cross-encoder"
    resource_class = ResourceClass.CPU_HEAVY

    def __init__(self, encoder: PinnedCrossEncoder) -> None:
        self._encoder = encoder
        self.model = encoder.pin.model_name
        self.version = encoder.pin.revision

    def health(self) -> CapabilityHealth:
        return CapabilityHealth.READY if self._encoder.status() is None else CapabilityHealth.UNAVAILABLE

    def score(self, query: str, candidates: Sequence[RerankCandidate]) -> list[float]:
        try:
            return list(self._encoder.score(query, [candidate.text for candidate in candidates]))
        except LocalModelError as exc:
            code = exc.code
        raise RerankError(code) from None
