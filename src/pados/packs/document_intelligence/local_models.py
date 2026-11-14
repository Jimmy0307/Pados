# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned local model engines: a dense text embedder and a cross-encoder, behind closed-code boundaries.

The shipped backend is FastEmbed (Apache-2.0) over ONNX Runtime (MIT) on the CPU execution provider. It is an
OPTIONAL dependency (``pip install "pados[document-intelligence]"``); without it, or without an operator
directory matching the pin, the engine reports a refusal from ``status()`` and is never loaded.

Guarantees:

* **No download.** The engine is constructed with ``specific_model_path`` (FastEmbed returns that directory
  before any hub code runs) and ``local_files_only=True``, and only after ``verify_model_dir`` passes.
* **Bounded.** At most ``MAX_TEXTS`` inputs per call; each input is cut to the pin's ``max_input_chars`` (the
  count is reported); batches are the pin's ``max_batch``; the engine runs with the pin's thread count.
* **Deadline.** Every call (and the load) runs on the engine's single worker thread and must finish within the
  pin's ``timeout_seconds``. CPython cannot stop that thread, so a late call POISONS the engine: every later call
  is refused (``ENGINE_POISONED``) and at most one runaway computation exists per engine.
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
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from pados.enterprise.compute import ResourceClass
from pados.enterprise.manifest import CapabilityHealth
from pados.enterprise.rerank import RerankCandidate, RerankError
from pados.packs.document_intelligence.model_pin import ModelPin, ModelRole, verify_model_dir

MAX_TEXTS: Final = 4096
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


def _fastembed_kwargs(pin: ModelPin, directory: Path) -> dict[str, Any]:
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
                 identity: EngineIdentity | None | object = _AUTO) -> None:
        self.pin = pin
        self.directory = directory
        self._factory = factory
        self.identity: EngineIdentity | None = (fastembed_identity(pin.threads) if identity is _AUTO
                                                else identity)  # type: ignore[assignment]
        self._backend: Any = None
        self._poisoned = False
        self._verified = False
        self._lock = threading.Lock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"pados-model-{pin.pin_id}")

    def status(self) -> str | None:
        """``None`` when the engine may run; otherwise the closed reason it may not (no side effects)."""
        if self._poisoned:
            return "ENGINE_POISONED"
        if self.identity is None:
            return "ENGINE_NOT_INSTALLED"
        if self.pin.role is not self.role:
            return "MODEL_PIN_ROLE_MISMATCH"
        return None if self._verified else verify_model_dir(self.pin, self.directory)

    def _run(self, call: Callable[[], Any]) -> Any:
        future: Future[Any] = self._worker.submit(call)
        try:
            return future.result(timeout=self.pin.timeout_seconds)
        except FutureTimeout:
            self._poisoned = True  # the thread cannot be stopped: refuse everything after this
            failure = "LOCAL_MODEL_TIMEOUT"
        except LocalModelError:
            raise
        except Exception:  # noqa: BLE001 - closed boundary: engine text never escapes
            failure = "LOCAL_MODEL_FAILED"
        raise LocalModelError(failure) from None

    def _backend_ready(self) -> Any:
        reason = self.status()
        if reason is not None:
            raise LocalModelError(reason)
        self._verified = True
        if self._backend is None:
            self._backend = self._run(lambda: self._factory(self.pin, self.directory))
            if self._backend is None:
                raise LocalModelError("LOCAL_MODEL_FAILED")
        return self._backend

    def _bounded(self, texts: Sequence[str]) -> tuple[list[str], int]:
        if (not isinstance(texts, Sequence) or isinstance(texts, str) or not 1 <= len(texts) <= MAX_TEXTS
                or any(not isinstance(text, str) for text in texts)):
            raise LocalModelError("LOCAL_MODEL_INPUT_INVALID")
        limit = self.pin.max_input_chars
        return [text[:limit] for text in texts], sum(1 for text in texts if len(text) > limit)


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
                 identity: EngineIdentity | None | object = _AUTO) -> None:
        super().__init__(pin, directory, factory=factory, identity=identity)

    def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        bounded, truncated = self._bounded(texts)
        dimension = self.pin.dimension
        assert dimension is not None  # an EMBEDDING pin always has one (validated)
        with self._lock:
            backend = self._backend_ready()
            raw = self._run(lambda: [list(vector) for vector in backend.embed(bounded,
                                                                              batch_size=self.pin.max_batch)])
        if len(raw) != len(bounded):
            raise LocalModelError("LOCAL_MODEL_OUTPUT_INVALID")
        return EmbeddingResult(tuple(_unit(vector, dimension) for vector in raw), truncated)


class PinnedCrossEncoder(_PinnedEngine):
    role = ModelRole.CROSS_ENCODER

    def __init__(self, pin: ModelPin, directory: Path, *, factory: Factory = fastembed_cross_encoder,
                 identity: EngineIdentity | None | object = _AUTO) -> None:
        super().__init__(pin, directory, factory=factory, identity=identity)

    def score(self, query: str, texts: Sequence[str]) -> tuple[float, ...]:
        (bounded_query,), _ = self._bounded([query])
        bounded, _ = self._bounded(texts)
        with self._lock:
            backend = self._backend_ready()
            raw = self._run(lambda: [float(score) for score in backend.rerank(bounded_query, bounded,
                                                                              batch_size=self.pin.max_batch)])
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
