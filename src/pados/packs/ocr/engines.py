# SPDX-License-Identifier: AGPL-3.0-only
"""Third-party OCR engine adapters: CONTRACTS ONLY until an operator installs and configures the engine.

Nothing here is bundled or installed by PADOS. Each adapter records its license (SPDX), the operator
-declared engine version and its distribution, and reports ``available() == False`` unless the
operator-configured engine is present, so the shared policy refuses it with ``ADAPTER_UNAVAILABLE``.

* ``TesseractCliAdapter`` (Apache-2.0): fixed argv, no shell, minimal environment
  (``OMP_THREAD_LIMIT=1``), input on stdin, run through the pack's bounded runner
  (``_isolated.run_bounded``): the configured binary must pass ``executable_refusal`` (absolute, no
  symlinked component, regular, executable, not setuid/setgid, no file capabilities), and it is
  started by the ``_rlimit_exec`` launcher, which re-validates it and applies rlimits for
  memory/CPU/file writes/open files/core and, always, ``RLIMIT_NPROC = 0`` (no child process or
  thread) before it execs it; a wall-clock deadline that covers startup, incremental stdout/stderr
  caps and a SIGKILL of the original process group. A build that needs a thread fails closed
  (``ENGINE_FAILED``). Unavailable on Windows and for a privileged process
  (``OCR_ISOLATION_UNAVAILABLE``). TSV output is parsed into ``ocr.engine-result/v1``. The image
  decoders are the engine's own and run inside that bounded child.
* ``PaddleOcrAdapter`` (Apache-2.0) and ``MinerUStyleAdapter`` (AGPL-3.0-only; license review required,
  model weights are model-specific): the engine is reached only through an operator-configured
  ``EngineBinding``: a ``module:function`` reference that is NEVER called in the PADOS process. It
  runs in the bounded binding worker (``_binding_worker``, same limits, ``RLIMIT_NPROC = 0``), so the
  engine and every decoder it uses are inside a bounded child; it returns ``ocr.engine-result/v1``
  (their Python APIs change across versions, so the pack does not hard-code one).
* ``VisionModelOcrAdapter``: a MODEL_RUNTIME delegate. Model selection stays with the PADOS Worker
  Router; the stage must carry the router's selection hash, and the cloud variant is a WAN
  EXTERNAL_SERVICE refused by the shared policy above INTERNAL data or under a local-only task. Its
  binding is a bridge to the worker the router selected; containment of the model runtime (and of
  any decoder it runs) is the router-side worker's, not this pack's.

Engine output is a candidate: it is validated, normalized and bounded, never trusted. Engine and
binding failures surface only as closed codes raised without exception chaining (no ``__cause__`` or
``__context__`` that could carry document text or credentials).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.model import (
    AdapterDescriptor,
    Determinism,
    Distribution,
    LicenseInfo,
    LifecycleState,
    ResourceRequirement,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.ocr._binding_worker import BINDING_CODES, BINDING_IMPORT_FAILED, BindingRefusal
from pados.packs.ocr._isolated import (
    BINDING_TARGET,
    MAX_SOURCE_BYTES,
    OCR_ISOLATION_UNAVAILABLE,
    OCR_SOURCE_TOO_LARGE,
    BoundedRun,
    BoundedRunError,
    ProcessBudget,
    RunFailure,
    isolated_binding,
    run_bounded,
)
from pados.packs.ocr._rlimit_exec import executable_refusal
from pados.packs.ocr._stage import OcrStageAdapter, read_manifest, recognition_output, source_input
from pados.packs.ocr.contracts import (
    IMAGE_MEDIA_TYPES,
    MAX_BLOCKS_PER_PAGE,
    PDF_MEDIA_TYPE,
    OcrContractError,
    StageParameters,
)
from pados.packs.ocr.descriptors import DOCUMENT_OCR, VISION_MODEL_OCR, VISION_MODEL_OCR_CLOUD
from pados.packs.ocr.normalize import EngineLine, EnginePage, EngineResult

TESSERACT_ADAPTER_ID = "ocr.tesseract-cli"
PADDLE_ADAPTER_ID = "ocr.paddleocr"
MINERU_ADAPTER_ID = "ocr.mineru-style"
VISION_LOCAL_ADAPTER_ID = "ocr.vision-model.local"
VISION_CLOUD_ADAPTER_ID = "ocr.vision-model.cloud"

# Adapters whose engine license needs a human license review before any distribution or service use.
LICENSE_REVIEW_REQUIRED: Mapping[str, str] = {
    MINERU_ADAPTER_ID: "AGPL-3.0-only engine (network copyleft); model weights carry model-specific licenses",
    VISION_LOCAL_ADAPTER_ID: "model weights carry model-specific licenses",
    VISION_CLOUD_ADAPTER_ID: "provider terms of service; data leaves the host",
}

# BCP-47 subset -> Tesseract traineddata names (bounded; an unmapped hint fails closed).
TESSERACT_LANGUAGES: Mapping[str, str] = {
    "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por", "nl": "nld", "sv": "swe",
    "pl": "pol", "ru": "rus", "ar": "ara", "hi": "hin", "ja": "jpn", "ko": "kor", "zh": "chi_sim",
    "zh-Hans": "chi_sim", "zh-Hant": "chi_tra", "zh-TW": "chi_tra", "zh-HK": "chi_tra", "vi": "vie", "th": "tha",
}
_ENGINE_CODES = {RunFailure.START_FAILED: "ENGINE_START_FAILED", RunFailure.TIMEOUT: "ENGINE_TIMEOUT",
                 RunFailure.STDOUT_LIMIT: "ENGINE_OUTPUT_TOO_LARGE", RunFailure.STDERR_LIMIT: "ENGINE_OUTPUT_TOO_LARGE",
                 RunFailure.RESOURCE_LIMIT: "ENGINE_BUDGET_EXCEEDED",
                 RunFailure.ISOLATION_UNAVAILABLE: OCR_ISOLATION_UNAVAILABLE,
                 RunFailure.EXECUTABLE_INVALID: "ENGINE_EXECUTABLE_INVALID"}
MIB = 1024 * 1024
_TSV_HEADER = ("level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top", "width",
               "height", "conf", "text")


def _unavailable() -> CapabilityAdapterError:
    return CapabilityAdapterError("ADAPTER_UNAVAILABLE")


# --- Tesseract CLI ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TesseractConfig:
    """Operator configuration (validated where loaded); never derived from task or document input.

    The engine always runs with ``RLIMIT_NPROC = 0``: it can start no process and no thread (Linux
    counts threads), which a single-threaded build with ``OMP_THREAD_LIMIT=1`` does not need. A build
    that does need threads fails closed (``ENGINE_FAILED``); there is no switch to relax it.
    """

    executable: Path
    engine_version: str
    tessdata_dir: Path | None = None
    timeout_seconds: int = 120
    max_output_bytes: int = 8 * 1024 * 1024
    max_stderr_bytes: int = 64 * 1024
    memory_mb: int = 2048

    def __post_init__(self) -> None:
        if not self.executable.is_absolute() or (self.tessdata_dir is not None and not self.tessdata_dir.is_absolute()):
            raise ValueError("TESSERACT_PATH_NOT_ABSOLUTE")
        if (not 1 <= self.timeout_seconds <= 600 or not 1024 <= self.max_output_bytes <= 64 * 1024 * 1024
                or not 1024 <= self.max_stderr_bytes <= 1024 * 1024 or not 256 <= self.memory_mb <= 32 * 1024):
            raise ValueError("TESSERACT_LIMITS_INVALID")
        LicenseInfo(spdx="Apache-2.0", engine_version=self.engine_version)  # validates the version token

    @property
    def budget(self) -> ProcessBudget:
        """Wall clock and CPU = ``timeout_seconds``; memory, output caps and a small descriptor table."""
        return ProcessBudget(wall_seconds=float(self.timeout_seconds), cpu_seconds=self.timeout_seconds,
                             memory_bytes=self.memory_mb * 1024 * 1024, max_input_bytes=MAX_SOURCE_BYTES,
                             max_stdout_bytes=self.max_output_bytes, max_stderr_bytes=self.max_stderr_bytes,
                             max_open_files=64)


def tesseract_descriptor(engine_version: str = "n/a") -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=TESSERACT_ADAPTER_ID, capability_id=DOCUMENT_OCR, version="1", backend="tesseract-cli",
        determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, resources=ResourceRequirement(cpu_cores=1, ram_mb=512),
        license=LicenseInfo(spdx="Apache-2.0", engine_version=engine_version,
                            distribution=Distribution.RUNTIME_OPTIONAL))


def tesseract_languages(hints: Sequence[str]) -> tuple[str, ...]:
    codes: list[str] = []
    for hint in hints or ("en",):
        parts = hint.split("-")
        candidates = (hint, "-".join(parts[:2]), parts[0])
        code = next((TESSERACT_LANGUAGES[c] for c in candidates if c in TESSERACT_LANGUAGES), None)
        if code is None:
            raise CapabilityAdapterError("LANGUAGE_UNSUPPORTED")
        if code not in codes:
            codes.append(code)
    return tuple(codes)


def tesseract_argv(config: TesseractConfig, languages: Sequence[str]) -> list[str]:
    """Fixed argv: image on stdin, TSV on stdout, page segmentation 3; nothing comes from the document."""
    if not languages or any(not re.fullmatch(r"[a-z]{3}(_[a-z]{3,4})?", code) for code in languages):
        raise CapabilityAdapterError("LANGUAGE_UNSUPPORTED")
    argv = [str(config.executable), "stdin", "stdout", "-l", "+".join(languages), "--psm", "3"]
    if config.tessdata_dir is not None:
        argv += ["--tessdata-dir", str(config.tessdata_dir)]
    return [*argv, "tsv"]


def tesseract_environment() -> dict[str, str]:
    """Minimal environment: no inherited variables (and so no credentials), single-threaded for replay."""
    return {"OMP_THREAD_LIMIT": "1", "LC_ALL": "C"}


def _tsv_numbers(fields: Sequence[str]) -> tuple[tuple[int, ...], tuple[float, ...]] | None:
    try:
        return tuple(int(v) for v in fields[:6]), tuple(float(v) for v in fields[6:11])
    except ValueError:
        return None


def parse_tesseract_tsv(text: str, *, page_number: int = 1) -> EngineResult:
    """Tesseract TSV -> one engine-result page: words grouped into lines, union bbox, mean confidence."""
    rows = text.splitlines()
    if not rows or tuple(rows[0].split("\t")) != _TSV_HEADER:
        raise OcrContractError("ENGINE_OUTPUT_INVALID")
    width = height = None
    lines: dict[tuple[int, int, int, int], list[tuple[str, float, float, float, float, float]]] = {}
    for row in rows[1:]:
        fields = row.split("\t")
        if len(fields) == len(_TSV_HEADER) - 1:
            fields.append("")
        if len(fields) != len(_TSV_HEADER):
            raise OcrContractError("ENGINE_OUTPUT_INVALID")
        numbers = _tsv_numbers(fields)
        if numbers is None:  # raised outside the handler: the ValueError quotes engine output
            raise OcrContractError("ENGINE_OUTPUT_INVALID")
        (level, page, block, par, line, _word), (left, top, w, h, conf) = numbers
        if level == 1:
            width, height = w, h
        elif level == 5 and fields[11].strip():
            lines.setdefault((page, block, par, line), []).append((fields[11], left, top, left + w, top + h, conf))
        if len(lines) > MAX_BLOCKS_PER_PAGE:
            raise OcrContractError("ENGINE_OUTPUT_TOO_LARGE")
    if width is None or height is None or width <= 0 or height <= 0:
        raise OcrContractError("ENGINE_OUTPUT_INVALID")
    engine_lines = []
    for words in lines.values():
        scored = [conf for *_, conf in words if conf >= 0]
        engine_lines.append(EngineLine(
            text=" ".join(word[0] for word in words),
            bbox=(min(w[1] for w in words), min(w[2] for w in words), max(w[3] for w in words),
                  max(w[4] for w in words)),
            confidence=min(1.0, max(0.0, sum(scored) / len(scored) / 100.0)) if scored else None))
    return EngineResult(pages=(EnginePage(page_number=page_number, width=width, height=height, unit="px",
                                          lines=tuple(engine_lines)),))


class TesseractCliAdapter(OcrStageAdapter):
    requires_isolation = True

    def __init__(self, config: TesseractConfig | None = None) -> None:
        super().__init__(tesseract_descriptor(config.engine_version if config else "n/a"))
        self._config = config

    def _ready(self) -> bool:
        """Configured, and the binary passes ``executable_refusal`` (no symlinked path, regular, ...)."""
        config = self._config
        return config is not None and executable_refusal(str(config.executable)) is None

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        config = self._config
        reason = self.unavailable_reason()
        if config is None or reason is not None:
            raise CapabilityAdapterError(reason or "ADAPTER_UNAVAILABLE")
        source = source_input(request, params)
        if source.media_type not in IMAGE_MEDIA_TYPES:
            raise CapabilityAdapterError("MEDIA_TYPE_UNSUPPORTED")  # PDFs need rasterization first
        manifest = read_manifest(request, context, source)
        argv = tesseract_argv(config, tesseract_languages(params.language_hints))
        budget = config.budget
        if source.size_bytes > budget.max_input_bytes:
            raise CapabilityAdapterError(OCR_SOURCE_TOO_LARGE)
        failure: RunFailure | None = None
        completed: BoundedRun | None = None
        try:
            completed = run_bounded(argv, stdin=context.artifacts.get(source), env=tesseract_environment(),
                                    budget=budget)
        except BoundedRunError as exc:
            failure = exc.reason
        if failure is not None or completed is None:  # raised outside the handler: no chained context
            code = _ENGINE_CODES[failure or RunFailure.START_FAILED]
            raise CapabilityAdapterError(code, retryable=code == "ENGINE_TIMEOUT")
        if completed.returncode != 0:
            raise CapabilityAdapterError("ENGINE_FAILED")
        result = parse_tesseract_tsv(completed.stdout.decode("utf-8", errors="replace"))
        return recognition_output(self.descriptor, request, context, source, manifest, result)


# --- engines reached through an operator-configured binding (always in the binding worker) -----------

@dataclass(frozen=True)
class EngineBinding:
    """Operator configuration of an engine binding; never derived from task or document input.

    ``target`` is ``"package.module:function"``. It is NEVER imported or called in the PADOS process:
    ``_isolated.isolated_binding`` runs it in ``_binding_worker`` (``python -I -B -m``) under this
    budget, with ``RLIMIT_NPROC = 0``. The module must be installed into the PADOS environment
    (``-I`` ignores ``PYTHONPATH``, the user site and the working directory). The function is called as
    ``function(payload, *, media_type, pages, languages)`` and returns ``ocr.engine-result/v1``; it may
    raise ``_binding_worker.BindingRefusal(code)`` with one of ``BINDING_CODES``.
    """

    target: str
    timeout_seconds: int = 300
    memory_mb: int = 4096
    max_output_bytes: int = 16 * MIB
    max_stderr_bytes: int = 64 * 1024
    max_open_files: int = 256

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not BINDING_TARGET.fullmatch(self.target):
            raise ValueError("ENGINE_BINDING_TARGET_INVALID")
        if (not 1 <= self.timeout_seconds <= 600 or not 256 <= self.memory_mb <= 64 * 1024
                or not 1024 <= self.max_output_bytes <= 64 * MIB or not 1024 <= self.max_stderr_bytes <= MIB
                or not 16 <= self.max_open_files <= 1024):
            raise ValueError("ENGINE_BINDING_LIMITS_INVALID")

    @property
    def module(self) -> str:
        return self.target.partition(":")[0]

    @property
    def budget(self) -> ProcessBudget:
        return ProcessBudget(wall_seconds=float(self.timeout_seconds), cpu_seconds=self.timeout_seconds,
                             memory_bytes=self.memory_mb * MIB, max_input_bytes=MAX_SOURCE_BYTES,
                             max_stdout_bytes=self.max_output_bytes, max_stderr_bytes=self.max_stderr_bytes,
                             max_open_files=self.max_open_files)


def binding_environment() -> dict[str, str]:
    """Minimal environment for the binding worker: nothing inherited, native thread pools pinned to 1."""
    return {"OMP_NUM_THREADS": "1", "OMP_THREAD_LIMIT": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
            "LC_ALL": "C"}


def _module_present(name: str) -> bool:
    """Whether the TOP-LEVEL package of ``name`` is findable; nothing is imported or executed."""
    try:
        return find_spec(name.partition(".")[0]) is not None
    except (ImportError, ValueError):
        return False


class _BoundEngineAdapter(OcrStageAdapter):
    requires_isolation = True  # the binding runs only in the bounded binding worker
    module_name = ""
    media_types: frozenset[str] = frozenset()

    def __init__(self, descriptor: AdapterDescriptor, binding: EngineBinding | None) -> None:
        super().__init__(descriptor)
        if binding is not None and not isinstance(binding, EngineBinding):
            raise TypeError("ENGINE_BINDING_INVALID")  # an in-process object is never accepted
        self._binding = binding

    def _ready(self) -> bool:
        """Configured, and the engine package and the binding package are findable (no code runs)."""
        binding = self._binding
        return binding is not None and _module_present(self.module_name) and _module_present(binding.module)

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        binding = self._binding
        reason = self.unavailable_reason()
        if binding is None or reason is not None:
            raise CapabilityAdapterError(reason or "ADAPTER_UNAVAILABLE")
        source = source_input(request, params)
        if source.media_type not in self.media_types:
            raise CapabilityAdapterError("MEDIA_TYPE_UNSUPPORTED")
        manifest = read_manifest(request, context, source)
        budget = binding.budget
        if source.size_bytes > budget.max_input_bytes:
            raise CapabilityAdapterError(OCR_SOURCE_TOO_LARGE)
        raw = isolated_binding(binding.target, context.artifacts.get(source),
                               {"media_type": source.media_type, "pages": list(manifest.selected_pages),
                                "languages": list(params.language_hints)},
                               env=binding_environment(), budget=budget,
                               codes=BINDING_CODES | {BINDING_IMPORT_FAILED})
        return recognition_output(self.descriptor, request, context, source, manifest, raw)


def paddle_descriptor(engine_version: str = "n/a") -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=PADDLE_ADAPTER_ID, capability_id=DOCUMENT_OCR, version="1", backend="paddleocr",
        determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, resources=ResourceRequirement(cpu_cores=2, ram_mb=2048),
        license=LicenseInfo(spdx="Apache-2.0", engine_version=engine_version,
                            distribution=Distribution.RUNTIME_OPTIONAL))


class PaddleOcrAdapter(_BoundEngineAdapter):
    module_name = "paddleocr"
    media_types = IMAGE_MEDIA_TYPES

    def __init__(self, binding: EngineBinding | None = None, *, engine_version: str = "n/a") -> None:
        super().__init__(paddle_descriptor(engine_version), binding)


def mineru_descriptor(engine_version: str = "n/a") -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=MINERU_ADAPTER_ID, capability_id=DOCUMENT_OCR, version="1", backend="mineru",
        determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, lifecycle=LifecycleState.EXPERIMENTAL,
        resources=ResourceRequirement(cpu_cores=4, ram_mb=8192),
        license=LicenseInfo(spdx="AGPL-3.0-only", engine_version=engine_version,
                            distribution=Distribution.RUNTIME_OPTIONAL))


class MinerUStyleAdapter(_BoundEngineAdapter):
    """MinerU-style full-document parser; EXPERIMENTAL and license review required (AGPL-3.0-only)."""

    module_name = "mineru"
    media_types = frozenset({PDF_MEDIA_TYPE, *IMAGE_MEDIA_TYPES})

    def __init__(self, binding: EngineBinding | None = None, *, engine_version: str = "n/a") -> None:
        super().__init__(mineru_descriptor(engine_version), binding)


# --- vision-language model (MODEL_RUNTIME delegate) -------------------------------------------

def _call_binding(call: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
    """Run the router bridge; any failure becomes a closed code raised with no ``__cause__``/``__context__``.

    The bridge's own exception (which may quote document text or credentials) is dropped inside the
    handler and the closed error is raised after it, so nothing of it is chained or retained.
    """
    code: str | None = None
    retryable = False
    try:
        return call()
    except CapabilityAdapterError as exc:  # even a bridge's own adapter error is re-issued, never re-raised
        code = exc.code if exc.code in BINDING_CODES else "ENGINE_FAILED"
        retryable = exc.retryable
    except Exception:  # bridge failures never echo engine messages
        code = "ENGINE_FAILED"
    raise CapabilityAdapterError(code, retryable=retryable) from None


class VisionModelBinding(Protocol):
    """Bridge to the worker the PADOS Worker Router selected; never selects a provider itself."""

    def ready(self) -> bool: ...

    def transcribe(self, payload: bytes, *, media_type: str, pages: tuple[int, ...], languages: tuple[str, ...],
                   router_selection_hash: str) -> Mapping[str, Any]: ...


def vision_descriptor(*, cloud: bool) -> AdapterDescriptor:
    if cloud:
        return AdapterDescriptor(
            adapter_id=VISION_CLOUD_ADAPTER_ID, capability_id=VISION_MODEL_OCR_CLOUD, version="1",
            backend="worker-router", determinism=Determinism.PROBABILISTIC,
            max_classification=DataClassification.INTERNAL, network_requirement=NetworkRequirement.WAN_REQUIRED,
            lifecycle=LifecycleState.EXPERIMENTAL, delegates_to_worker_router=True,
            license=LicenseInfo(spdx="LicenseRef-Provider-Terms", distribution=Distribution.EXTERNAL_SERVICE))
    return AdapterDescriptor(
        adapter_id=VISION_LOCAL_ADAPTER_ID, capability_id=VISION_MODEL_OCR, version="1", backend="worker-router",
        determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.CONFIDENTIAL,
        network_requirement=NetworkRequirement.LOCAL_ONLY, lifecycle=LifecycleState.EXPERIMENTAL,
        delegates_to_worker_router=True, resources=ResourceRequirement(gpu_required=True, vram_mb=8192),
        license=LicenseInfo(spdx="LicenseRef-Model-Specific", distribution=Distribution.RUNTIME_OPTIONAL))


class VisionModelOcrAdapter(OcrStageAdapter):
    media_types = IMAGE_MEDIA_TYPES

    def __init__(self, binding: VisionModelBinding | None = None, *, cloud: bool = False) -> None:
        super().__init__(vision_descriptor(cloud=cloud))
        self._binding = binding

    def _ready(self) -> bool:
        return self._binding is not None and bool(self._binding.ready())

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        binding = self._binding
        if binding is None or not self.available():
            raise _unavailable()
        selection = request.router_selection_hash
        if not isinstance(selection, str) or not re.fullmatch(r"[0-9a-f]{64}", selection):
            raise CapabilityAdapterError("WORKER_ROUTER_SELECTION_REQUIRED")
        source = source_input(request, params)
        if source.media_type not in self.media_types:
            raise CapabilityAdapterError("MEDIA_TYPE_UNSUPPORTED")
        manifest = read_manifest(request, context, source)
        payload = context.artifacts.get(source)
        raw = _call_binding(lambda: binding.transcribe(payload, media_type=source.media_type,
                                                       pages=manifest.selected_pages, languages=params.language_hints,
                                                       router_selection_hash=selection))
        return recognition_output(self.descriptor, request, context, source, manifest, raw,
                                  extra={"router_selection_hash": selection})


__all__ = ["BINDING_CODES", "LICENSE_REVIEW_REQUIRED", "TESSERACT_LANGUAGES", "BindingRefusal", "EngineBinding",
           "MinerUStyleAdapter", "PaddleOcrAdapter", "TesseractCliAdapter", "TesseractConfig", "VisionModelBinding",
           "VisionModelOcrAdapter", "binding_environment", "parse_tesseract_tsv", "tesseract_argv",
           "tesseract_environment", "tesseract_languages"]
