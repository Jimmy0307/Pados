"""CR-15c remediation: OCR-14, OCR-15/OCR-12, OCR-02/OCR-10, OCR-01, OCR-08 and OCR-03.

* OCR-14: page coverage is trusted only from the exact ``DocumentPreprocessAdapter`` implementation:
  a subclass (even with the registered descriptor) or an instance-level override is refused.
* OCR-15 / OCR-12: the recognition measurement is the unweighted mean and minimum of the LINE
  confidences only; high-confidence non-LINE blocks cannot lift it, and the runner carries it.
* OCR-02 / OCR-10: ``RLIMIT_NPROC = 0`` is mandatory (no ``deny_process_creation``); a privileged
  process (uid 0, ``CAP_SYS_RESOURCE``, ``CAP_SYS_ADMIN``) spawns nothing; the child also refuses.
* OCR-01: pypdf's decompression limits come from the budget, and every page's content streams are
  decoded under the per-page cap BEFORE ``extract_text`` runs.
* OCR-08: the exec target is validated (no symlinked component, regular, executable, not setid);
  ``Popen`` returning after the deadline kills the child at once; reaping is bounded.
* OCR-03: operator engine bindings are ``module:function`` references run only in the bounded binding
  worker; the TEST-ONLY binding module (``tests/fixtures/ocr/bindings``) is made importable for
  ``python -I`` by a temporary ``.pth`` in the venv's site-packages, removed afterwards.

New pack symbols are read through their modules at call time, so this file also collects against the
pre-fix source (the "before" run). No real engine or model is used.
"""
from __future__ import annotations

import importlib
import io
import os
import signal
import stat
import subprocess
import sys
import sysconfig
import time
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from ocr_support import (
    FIXTURES,
    fixture_bytes,
    fixed_clock,
    image_assignments,
    load_builder,
    pdf_assignments,
    permitted_context,
    put_source,
    run_stage,
    scope,
    task,
)

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ObjectStoreArtifacts
from pados.capability.policy import PolicyCode, evaluate
from pados.packs.ocr import _isolated, _pdf_worker, engines, pipeline
from pados.packs.ocr._isolated import BoundedRunError, ProcessBudget, RunFailure, run_bounded
from pados.packs.ocr.contracts import (
    KIND_DOCUMENT,
    KIND_PREPROCESSED,
    Block,
    BlockKind,
    OcrDocument,
    OcrPage,
    PageStatus,
    PreprocessManifest,
    QualityRequirement,
    page_measurement,
)
from pados.packs.ocr.descriptors import OCR_CAPABILITIES
from pados.packs.ocr.engines import PaddleOcrAdapter, TesseractCliAdapter, TesseractConfig
from pados.packs.ocr.fixture import TEST_FIXTURE_MODE, FixtureOcrAdapter
from pados.packs.ocr.normalize import EngineLine, EnginePage, EngineResult
from pados.packs.ocr.pipeline import (
    IMAGE_TEMPLATE,
    PDF_TEXT_LAYER_TEMPLATE,
    OcrPipelineRefused,
    PipelineResult,
    run_pipeline,
)
from pados.packs.ocr.reference import DocumentPreprocessAdapter, XObjectFigureAdapter
from pados.packs.ocr.text_layer import PdfTextLayerAdapter

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits; Windows fails closed (PENDING)")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux capability sets / prctl")
MIB = 1024 * 1024
PROJECT = "proj-ocr"
BINDINGS = FIXTURES / "bindings"
BINDING_MODULE = "pados_ocr_test_binding"
UNPRIVILEGED_STATUS = "CapInh:\t0000000000000000\nCapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n" \
                      "CapBnd:\t000001ffffffffff\nCapAmb:\t0000000000000000\n"


def _limits() -> Any:
    return importlib.import_module("pados.packs.ocr._rlimit_exec")


def _run(store: Any, template: Any, ocr_task: Any, assignments: Any, **kwargs: Any) -> PipelineResult:
    options: dict[str, Any] = {"store": store, "scope": scope(), "revalidate": scope, "project_id": PROJECT,
                               "plan_id": "plan-ocr-c", "allow_experimental": True, "clock": fixed_clock()}
    options.update(kwargs)
    return run_pipeline(template, ocr_task, assignments, **options)


def _pdf_task(store: ObjectStoreArtifacts, **overrides: Any) -> Any:
    return task(put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf"), **overrides)


# --- OCR-14: the preprocess identity check rejects subclasses and instance overrides ------------------------

class _FalseCount(DocumentPreprocessAdapter):
    """Keeps the registered descriptor but overrides ``_execute`` to claim the PDF has a single page."""

    def _execute(self, request: StageRequest, params: Any, context: StageContext) -> StageOutput:
        output = super()._execute(request, params, context)
        real = PreprocessManifest.from_bytes(context.artifacts.get(output.outputs[0]))
        forged = real.model_copy(update={"page_count": 1, "selected_pages": (1,), "geometry": real.geometry[:1]})
        ref = context.artifacts.put(forged.to_bytes(), media_type="application/json", kind=KIND_PREPROCESSED)
        return StageOutput(outputs=(ref,), metrics=output.metrics, evidence=output.evidence)


def test_preprocess_subclass_with_the_registered_descriptor_is_refused(tmp_path: Path) -> None:
    adapter = _FalseCount()
    assert adapter.descriptor == DocumentPreprocessAdapter().descriptor  # same digest: metadata only
    assert not pipeline.is_registered_preprocess(adapter)
    store = ObjectStoreArtifacts(tmp_path)
    with pytest.raises(OcrPipelineRefused) as refused:
        _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf_task(store), {**pdf_assignments(), "preprocess": adapter})
    assert (refused.value.code, refused.value.stage_id) == ("PREPROCESS_ADAPTER_NOT_REGISTERED", "preprocess")


@pytest.mark.parametrize("name", ["_execute", "execute", "_pdf", "available"])
def test_preprocess_instance_overrides_are_refused(name: str) -> None:
    adapter = DocumentPreprocessAdapter()
    assert pipeline.is_registered_preprocess(adapter)
    setattr(adapter, name, lambda *args, **kwargs: None)
    assert not pipeline.is_registered_preprocess(adapter)


def test_preprocess_budget_must_be_a_process_budget() -> None:
    adapter = DocumentPreprocessAdapter()
    adapter._budget = object()  # type: ignore[assignment]  # noqa: SLF001
    assert not pipeline.is_registered_preprocess(adapter)
    assert pipeline.is_registered_preprocess(DocumentPreprocessAdapter(budget=ProcessBudget(wall_seconds=5.0)))


# --- OCR-15 / OCR-12: LINE-only, unweighted measurement carried by the runner -----------------------------

def _page(lines: list[float], paragraphs: list[float]) -> OcrPage:
    blocks = [Block(block_id=f"p1.l{i}", kind=BlockKind.LINE, text=f"line {i}", confidence=c)
              for i, c in enumerate(lines)]
    blocks += [Block(block_id=f"p1.x{i}", kind=BlockKind.PARAGRAPH, text=f"para {i}", confidence=c)
               for i, c in enumerate(paragraphs)]
    return OcrPage(page_number=1, status=PageStatus.RECOGNIZED, producer="ocr.fixture-truth", blocks=tuple(blocks))


def test_measurement_policy_is_unweighted_line_mean_and_min() -> None:
    contracts = importlib.import_module("pados.packs.ocr.contracts")
    assert contracts.MEASUREMENT_POLICY == "LINE_UNWEIGHTED_MEAN_MIN/v1"
    measured = page_measurement(_page([0.2, 0.4], [0.99, 0.99, 0.99, 0.99]))
    assert measured is not None
    assert (measured.mean, measured.minimum, measured.scored_blocks) == (0.3, 0.2, 2)
    assert page_measurement(_page([], [0.99])) is None  # paragraph-only: unmeasured
    unscored = OcrPage(page_number=1, status=PageStatus.RECOGNIZED, blocks=(
        Block(block_id="a", kind=BlockKind.LINE, text="x", confidence=0.9),
        Block(block_id="b", kind=BlockKind.LINE, text="y")))
    assert page_measurement(unscored) is None  # partly scored: unmeasured


def _with_paragraphs(payload: bytes, kind: str) -> bytes:
    if kind != KIND_DOCUMENT:
        return payload
    document = OcrDocument.from_bytes(payload)
    pages = tuple(page.model_copy(update={"blocks": page.blocks + tuple(
        Block(block_id=f"p{page.page_number}.boost{i}", kind=BlockKind.PARAGRAPH, text="boost", confidence=0.99)
        for i in range(8))}) if page.status is PageStatus.RECOGNIZED else page for page in document.pages)
    return document.model_copy(update={"pages": pages}).to_bytes()


class _Intercept:
    """Wraps the stage's artifact facade and rewrites the adapter's output before it is stored."""

    def __init__(self, inner: Any, mutate: Callable[[bytes, str], bytes]) -> None:
        self._inner, self._mutate = inner, mutate

    def get(self, ref: Any) -> bytes:
        return bytes(self._inner.get(ref))

    def put(self, payload: bytes, *, media_type: str, kind: str) -> Any:
        return self._inner.put(self._mutate(payload, kind), media_type=media_type, kind=kind)


class _Boosting(FixtureOcrAdapter):
    """Recognition output with low LINE confidences plus many high-confidence PARAGRAPH blocks."""

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        wrapped = StageContext(artifacts=_Intercept(context.artifacts, _with_paragraphs),  # type: ignore[arg-type]
                               project_id=context.project_id, stage_id=context.stage_id)
        return super().execute(request, context=wrapped)


def test_high_confidence_paragraphs_do_not_lift_the_measurement(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    source = put_source(store, fixture_bytes("scan_page.png"), "image/png")
    low = EngineResult(pages=(EnginePage(page_number=1, width=64, height=16, unit="px", lines=(
        EngineLine(text="faint one", bbox=(2, 0, 60, 4), confidence=0.40),
        EngineLine(text="faint two", bbox=(2, 8, 60, 11), confidence=0.50))),))
    floor = QualityRequirement(min_mean_confidence=0.8)
    result = _run(store, IMAGE_TEMPLATE, task(source, quality=floor),
                  image_assignments(_Boosting({source.sha256: low})), test_fixtures=TEST_FIXTURE_MODE)
    recognized = OcrDocument.from_bytes(store.get(result.stage_outputs["recognize"]))
    assert any(b.kind is BlockKind.PARAGRAPH and b.confidence == 0.99 for b in recognized.pages[0].blocks)
    assert result.quality.met is False and "CONFIDENCE_BELOW_REQUIREMENT" in result.quality.reasons
    measured = getattr(result, "measurements")[1]
    assert (measured.mean, measured.minimum, measured.scored_blocks) == (0.45, 0.4, 2)


# --- OCR-02 / OCR-10: RLIMIT_NPROC = 0 is mandatory; a privileged process spawns nothing ---------------------

def test_process_creation_can_no_longer_be_allowed(tmp_path: Path) -> None:
    for make in (lambda: ProcessBudget(deny_process_creation=False),  # type: ignore[call-arg]
                 lambda: TesseractConfig(executable=tmp_path / "t", engine_version="5.4.1",
                                         deny_process_creation=False)):  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            make()
    limits = _limits()
    assert "deny_process_creation" not in limits.Limits.__dataclass_fields__
    limits_arg = _isolated.limits_token(ProcessBudget())
    assert limits_arg.endswith(",nproc=0") and limits.parse_limits(limits_arg) is not None
    assert limits.parse_limits(limits_arg.replace("nproc=0", "nproc=inherit")) is None


@pytest.mark.parametrize(("uid", "euid", "status", "reason"), [
    (0, 0, UNPRIVILEGED_STATUS, "PRIVILEGED_UID"),
    (1000, 0, UNPRIVILEGED_STATUS, "PRIVILEGED_EUID"),
    (1000, 1000, UNPRIVILEGED_STATUS.replace("CapEff:\t0000000000000000", "CapEff:\t0000000001000000"),
     "CAP_SYS_RESOURCE"),
    (1000, 1000, UNPRIVILEGED_STATUS.replace("CapEff:\t0000000000000000", "CapEff:\t0000000000200000"),
     "CAP_SYS_ADMIN"),
    (1000, 1000, UNPRIVILEGED_STATUS.replace("CapPrm:\t0000000000000000", "CapPrm:\t0000000001000000"),
     "CAP_SYS_RESOURCE"),
    (1000, 1000, UNPRIVILEGED_STATUS.replace("CapAmb:\t0000000000000000", "CapAmb:\t0000000000200000"),
     "CAP_SYS_ADMIN"),
    (1000, 1000, UNPRIVILEGED_STATUS.replace("CapEff:\t0000000000000000", "CapEff:\t0000000000000080"),
     "CAPABILITIES_HELD"),  # CAP_SETUID (bit 7): CR-15d OCR-16
    (1000, 1000, UNPRIVILEGED_STATUS.replace("CapPrm:\t0000000000000000", "CapPrm:\t0000000000000001"),
     "CAPABILITIES_HELD"),  # CAP_CHOWN permitted: any retained capability is refused
    (1000, 1000, None, "CAPABILITIES_UNREADABLE"),
    (1000, 1000, "CapEff:\tzz\n", "CAPABILITIES_UNREADABLE"),
    (1000, 1000, UNPRIVILEGED_STATUS, None),
], ids=["uid0", "euid0", "cap-sys-resource", "cap-sys-admin", "permitted", "ambient", "cap-setuid",
        "any-permitted", "unreadable", "garbled", "unprivileged"])
def test_privilege_refusal_reasons(uid: int, euid: int, status: str | None, reason: str | None) -> None:
    assert _limits().privilege_refusal_for(uid, euid, status, linux=True) == reason


@pytest.fixture
def privileged(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Simulates a process holding CAP_SYS_RESOURCE; records any spawn attempt (there must be none)."""
    limits = _limits()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(limits, "_identity", lambda: (1000, 1000))
    monkeypatch.setattr(limits, "_proc_status", lambda: UNPRIVILEGED_STATUS.replace(
        "CapEff:\t0000000000000000", "CapEff:\t0000000001000000"))
    spawned: list[object] = []

    def no_spawn(*args: object, **kwargs: object) -> None:
        spawned.append(args)
        raise AssertionError("a process was spawned")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    yield spawned


@posix_only
def test_privileged_process_refuses_without_spawning(privileged: list[object], tmp_path: Path) -> None:
    assert _isolated.isolation_refusal() == "CAP_SYS_RESOURCE"
    assert _isolated.isolation_unavailable_reason() == "OCR_ISOLATION_UNAVAILABLE"
    with pytest.raises(BoundedRunError) as refused:
        run_bounded([os.path.realpath(sys.executable), "-c", "pass"], stdin=b"", env={}, budget=ProcessBudget())
    assert refused.value.reason is RunFailure.ISOLATION_UNAVAILABLE
    with pytest.raises(CapabilityAdapterError, match="OCR_ISOLATION_UNAVAILABLE"):
        _isolated.isolated_pdf("inspect", fixture_bytes("text_and_scan.pdf"), {"max_pages": 5})
    script = tmp_path / "tesseract"
    script.write_text(f"#!{sys.executable} -I\n", encoding="utf-8")
    script.chmod(0o755)
    for adapter in (DocumentPreprocessAdapter(), PdfTextLayerAdapter(), XObjectFigureAdapter(),
                    TesseractCliAdapter(TesseractConfig(executable=script, engine_version="5.4.1")),
                    _ImportablePaddle(engines.EngineBinding(f"{BINDING_MODULE}:recognize"))):
        assert adapter.available() is False
        assert getattr(adapter, "unavailable_reason")() == "OCR_ISOLATION_UNAVAILABLE"
        capability = OCR_CAPABILITIES[adapter.descriptor.capability_id]
        decision = evaluate(capability, adapter.descriptor, permitted_context(adapter),
                            adapter_available=adapter.available)
        assert decision.code is PolicyCode.ADAPTER_UNAVAILABLE
    assert privileged == []


@posix_only
def test_privileged_child_refuses_before_any_limit_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    limits = _limits()
    monkeypatch.setattr(limits, "_identity", lambda: (0, 0))
    monkeypatch.setattr(resource, "setrlimit", lambda *a: pytest.fail("a limit was set"))
    assert limits.enter_limits([_isolated.limits_token(ProcessBudget())]) is False


@linux_only
def test_child_runs_with_no_new_privileges_and_no_process_creation() -> None:
    probe = ("import resource\nstatus = open('/proc/self/status').read()\n"
             "print([l for l in status.splitlines() if l.startswith('NoNewPrivs')][0].split()[1],"
             " resource.getrlimit(resource.RLIMIT_NPROC)[0])")
    run = run_bounded([os.path.realpath(sys.executable), "-I", "-B", "-c", probe], stdin=b"", env={},
                      budget=ProcessBudget())
    assert run.stdout.split() == [b"1", b"0"]


# --- OCR-08: exec-target validation, expired deadline after start, bounded reap ---------------------------

def _script(path: Path, body: str = "print('ran')") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable} -I\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@posix_only
def test_executable_validation_refuses_unsafe_targets(tmp_path: Path) -> None:
    refusal = _limits().executable_refusal
    real = _script(tmp_path / "bin" / "engine")
    assert refusal(str(real)) is None
    link = tmp_path / "bin" / "link"
    link.symlink_to(real)
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(tmp_path / "bin")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    plain = _script(tmp_path / "plain")
    plain.chmod(0o644)
    setid = _script(tmp_path / "setid")
    setid.chmod(0o755 | stat.S_ISUID)
    cases = {str(link): "EXECUTABLE_PATH_SYMLINK", str(linked_dir / "engine"): "EXECUTABLE_PATH_SYMLINK",
             str(fifo): "EXECUTABLE_NOT_REGULAR", str(tmp_path / "bin"): "EXECUTABLE_NOT_REGULAR",
             str(plain): "EXECUTABLE_NOT_EXECUTABLE", str(tmp_path / "missing"): "EXECUTABLE_UNREADABLE",
             "relative/engine": "EXECUTABLE_PATH_INVALID", str(tmp_path / "bin" / ".." / "bin" / "engine"):
             "EXECUTABLE_PATH_INVALID"}
    if setid.stat().st_mode & stat.S_ISUID:  # some filesystems drop the bit
        cases[str(setid)] = "EXECUTABLE_SETID"
    for path, reason in cases.items():
        assert refusal(path) == reason, path
    assert _limits().interpreter_refusal(sys.executable) is None  # the venv symlink is checked resolved


@posix_only
def test_symlinked_or_non_regular_engine_is_refused_without_spawning(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    real = _script(tmp_path / "bin" / "tesseract")
    link = tmp_path / "tesseract-link"
    link.symlink_to(real)
    fifo = tmp_path / "tesseract-fifo"
    os.mkfifo(fifo)
    assert TesseractCliAdapter(TesseractConfig(executable=real, engine_version="5.4.1")).available()
    spawned: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("spawned"))
    for target in (link, fifo):
        adapter = TesseractCliAdapter(TesseractConfig(executable=target, engine_version="5.4.1"))
        assert adapter.available() is False and getattr(adapter, "unavailable_reason")() == "ADAPTER_UNAVAILABLE"
        with pytest.raises(BoundedRunError) as refused:
            run_bounded([str(target)], stdin=b"", env={}, budget=ProcessBudget())
        assert refused.value.reason.value == "EXECUTABLE_INVALID"
    assert spawned == []
    # The launcher re-validates in the child, right before its exec (checked here without exec'ing).
    limits_arg = _isolated.limits_token(ProcessBudget())
    assert _limits().main([limits_arg, "--", str(link)]) == _limits().EXIT_EXECUTABLE_REFUSED


@posix_only
def test_popen_returning_after_the_deadline_kills_the_child_at_once(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "read-stdin"
    body = f"import sys\nsys.stdin.buffer.read()\nopen({str(marker)!r}, 'w').close()\nimport time\ntime.sleep(60)"
    started: list[subprocess.Popen[bytes]] = []
    real_start = _isolated._start  # noqa: SLF001
    now = [1000.0]
    monkeypatch.setattr(_isolated, "_clock", lambda: now[0])

    def late_start(*args: Any) -> Any:
        proc = real_start(*args)
        started.append(proc)
        now[0] += 31.0  # Popen "took" longer than the 30 s wall budget
        return proc

    monkeypatch.setattr(_isolated, "_start", late_start)
    begin = time.monotonic()
    with pytest.raises(BoundedRunError) as overrun:
        run_bounded([os.path.realpath(sys.executable), "-I", "-B", "-c", body], stdin=b"x" * 10, env={},
                    budget=ProcessBudget(wall_seconds=30.0))
    assert overrun.value.reason is RunFailure.TIMEOUT
    assert time.monotonic() - begin < 10
    assert started and started[0].returncode == -signal.SIGKILL
    assert not marker.exists()  # nothing was written to it: it never got its input


@posix_only
def test_reaping_after_the_kill_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    real_start = _isolated._start  # noqa: SLF001
    procs: list[subprocess.Popen[bytes]] = []

    def unreapable(*args: Any) -> Any:
        proc = real_start(*args)
        real_wait = proc.wait

        def wait(timeout: float | None = None) -> int:
            if timeout == _isolated.REAP_SECONDS:  # as if stuck in uninterruptible sleep
                raise subprocess.TimeoutExpired(args[0], timeout)
            return real_wait(timeout)

        proc.wait = wait  # type: ignore[method-assign]
        procs.append(proc)
        return proc

    monkeypatch.setattr(_isolated, "_start", unreapable)
    with pytest.raises(BoundedRunError) as stuck:
        run_bounded([os.path.realpath(sys.executable), "-I", "-B", "-c", "pass"], stdin=b"", env={},
                    budget=ProcessBudget())
    assert stuck.value.reason is RunFailure.TIMEOUT
    for proc in procs:  # the test reaps it itself
        subprocess.Popen.wait(proc, timeout=10)


# --- OCR-01: decompression and content-stream decoding are bounded before extract_text --------------------

TEXT = b"BT /F1 12 Tf 72 700 Td (Hello) Tj ET\n"


def _layer(tmp_path: Path, data: bytes, budget: ProcessBudget) -> StageOutput:
    store = ObjectStoreArtifacts(tmp_path)
    source = put_source(store, data, "application/pdf")
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), task(source)).outputs[0]
    return run_stage(PdfTextLayerAdapter(budget=budget), store, (source, manifest), task(source))


@posix_only
@pytest.mark.parametrize("streams", [[TEXT + b" " * (4 * MIB)], [TEXT] + [b" " * (200 * 1024)] * 4],
                         ids=["one-bomb-stream", "array-over-the-page-cap"])
def test_decompression_bomb_is_refused_before_text_extraction(streams: list[bytes], tmp_path: Path) -> None:
    builder = load_builder()
    bomb = builder.flate_content_pdf(streams)
    assert len(bomb) < 64 * 1024  # a few KiB that decode to MiBs
    budget = ProcessBudget(max_page_text_bytes=64 * 1024)
    assert budget.max_page_content_bytes == 64 * 1024 * _isolated.CONTENT_DECODE_FACTOR == 512 * 1024
    with pytest.raises(CapabilityAdapterError) as refused:
        _layer(tmp_path / "bomb", bomb, budget)
    assert refused.value.code == "OCR_DECODE_BUDGET_EXCEEDED"
    small = builder.flate_content_pdf([TEXT + b" " * 1024])
    output = _layer(tmp_path / "ok", small, budget)
    assert output.evidence["pages"] == 1 and output.evidence["pages_failed"] == 0


@pytest.fixture
def pypdf_limits(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Snapshot pypdf's module limits so an in-process worker call cannot leak lowered values."""
    from pypdf import filters

    for name in _pdf_worker.DECODE_LIMITS:
        monkeypatch.setattr(filters, name, getattr(filters, name))
    return filters


def _header(**overrides: Any) -> dict[str, Any]:
    budget = ProcessBudget(max_page_text_bytes=64 * 1024)
    header = {"op": "text", "page_count": 1, "pages": [1], "max_blocks": 100, "max_block_text": 1000,
              "max_page_text_bytes": budget.max_page_text_bytes, "max_text_bytes": budget.max_text_bytes,
              "max_stream_bytes": budget.max_page_content_bytes, "max_content_bytes": budget.max_content_bytes,
              "max_declared_stream_bytes": budget.max_input_bytes}
    header.update(overrides)
    return header


def test_decode_limits_are_set_from_the_budget_and_observable(pypdf_limits: Any) -> None:
    assert set(_pdf_worker.DECODE_LIMITS) <= set(vars(pypdf_limits))  # the installed pypdf has every limit
    applied = _pdf_worker.apply_decode_limits(512 * 1024, 64 * MIB)
    for name, source in _pdf_worker.DECODE_LIMITS.items():
        assert getattr(pypdf_limits, name) == applied[name]
        assert applied[name] <= (512 * 1024 if source == "stream" else 64 * MIB)
    from pypdf.errors import LimitReachedError

    with pytest.raises(LimitReachedError):  # pypdf itself stops decompressing at the budget
        pypdf_limits.decompress(zlib.compress(b" " * (4 * MIB), 9))
    with pytest.raises(_pdf_worker.ProtocolError):
        _pdf_worker.apply_decode_limits(0, 64 * MIB)  # 0 would DISABLE pypdf's limits: refused


def test_content_streams_are_decoded_before_extract_text(pypdf_limits: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf import PageObject

    extracted: list[object] = []
    real_extract = PageObject.extract_text

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        extracted.append(self)
        return real_extract(self, *args, **kwargs)

    monkeypatch.setattr(PageObject, "extract_text", spy)
    builder = load_builder()
    for streams in ([TEXT + b" " * (4 * MIB)], [TEXT] + [b" " * (200 * 1024)] * 4):
        with pytest.raises(_pdf_worker.Refused) as refused:
            _pdf_worker.text(_header(), builder.flate_content_pdf(streams))
        assert refused.value.code == "OCR_DECODE_BUDGET_EXCEEDED"
    assert extracted == []  # refused before any text extraction
    assert pypdf_limits.ZLIB_MAX_OUTPUT_LENGTH == 512 * 1024
    many = builder.flate_content_pdf([TEXT] + [b" " * (100 * 1024)] * 3)  # under the page cap
    with pytest.raises(_pdf_worker.Refused):
        _pdf_worker.text(_header(max_content_bytes=200 * 1024), many)  # ... but over the running total
    result = _pdf_worker.text(_header(), builder.flate_content_pdf([TEXT]))
    assert result["pages"][0]["lines"] == [[0, "Hello"]] and len(extracted) == 1


def test_xobjects_are_counted_from_the_raw_dictionary(pypdf_limits: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf.generic import IndirectObject

    builder = load_builder()
    data = builder.many_xobjects_pdf(40)
    resolved: list[object] = []
    real = IndirectObject.get_object

    def spy(self: IndirectObject) -> Any:
        target = real(self)
        if getattr(target, "get", lambda key: None)("/Subtype") == "/Image":
            resolved.append(self)
        return target

    monkeypatch.setattr(IndirectObject, "get_object", spy)
    header = {"op": "figures", "pages": [1], "max_figures": 64, "max_xobjects_per_page": 10, "max_xobjects": 100,
              "max_stream_bytes": 1 << 20, "max_declared_stream_bytes": 1 << 26}
    with pytest.raises(_pdf_worker.Refused) as refused:
        _pdf_worker.figures(header, data)
    assert refused.value.code == "OCR_PARSE_BUDGET_EXCEEDED" and resolved == []  # no member was resolved


# --- OCR-03: engine bindings run only in the bounded binding worker -----------------------------------------

class _ImportablePaddle(PaddleOcrAdapter):
    module_name = "json"  # stands in for an installed engine package; the engine itself is never present


@pytest.fixture
def installed_bindings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Makes the TEST-ONLY binding module importable for ``python -I`` (a temporary .pth in the venv's
    site-packages, removed afterwards) and findable for the parent's readiness check (sys.path)."""
    site = Path(sysconfig.get_paths()["purelib"])
    if not os.access(site, os.W_OK):
        pytest.skip("the environment's site-packages is not writable: cannot install the test binding")
    pth = site / f"_pados_ocr_test_binding_{os.getpid()}.pth"
    pth.write_text(str(BINDINGS) + "\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(BINDINGS))
    try:
        yield pth
    finally:
        pth.unlink(missing_ok=True)


def _recognize(tmp_path: Path, function: str, **limits: Any) -> OcrDocument:
    store = ObjectStoreArtifacts(tmp_path)
    source = put_source(store, fixture_bytes("scan_page.png"), "image/png")
    ocr_task = task(source, language_hints=("en", "de"))
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), ocr_task).outputs[0]
    adapter = _ImportablePaddle(engines.EngineBinding(f"{BINDING_MODULE}:{function}", **limits))
    assert adapter.available()
    output = run_stage(adapter, store, (source, manifest), ocr_task)
    return OcrDocument.from_bytes(store.get(output.outputs[0]))


def _failure(tmp_path: Path, function: str, **limits: Any) -> CapabilityAdapterError:
    started = time.monotonic()
    with pytest.raises(CapabilityAdapterError) as failed:
        _recognize(tmp_path, function, **limits)
    assert time.monotonic() - started < 30
    assert failed.value.__cause__ is None and failed.value.__context__ is None
    assert "SECRET-DOCUMENT-TEXT" not in str(failed.value)
    return failed.value


@posix_only
def test_binding_runs_in_the_isolated_worker_and_returns_a_result(installed_bindings: Path,
                                                                  tmp_path: Path) -> None:
    document = _recognize(tmp_path, "recognize")
    page = document.pages[0]
    size = len(fixture_bytes("scan_page.png"))
    assert page.status is PageStatus.RECOGNIZED and page.blocks[0].text == f"BOUND {size} en+de"
    assert BINDING_MODULE not in sys.modules  # never imported in the PADOS process


@posix_only
@pytest.mark.parametrize(("function", "limits", "code"), [
    ("hog", {"memory_mb": 256}, "ENGINE_BUDGET_EXCEEDED"),
    ("spin", {"timeout_seconds": 1}, "ENGINE_TIMEOUT"),
    ("leaky", {}, "ENGINE_FAILED"),
    ("leaky_code", {}, "ENGINE_FAILED"),
    ("timeout_code", {}, "ENGINE_TIMEOUT"),
    ("not_a_mapping", {}, "ENGINE_OUTPUT_INVALID"),
    ("not_json", {}, "ENGINE_OUTPUT_INVALID"),
    ("missing_function", {}, "ENGINE_BINDING_IMPORT_FAILED"),
])
def test_binding_failures_are_bounded_and_closed(function: str, limits: dict[str, Any], code: str,
                                                 installed_bindings: Path, tmp_path: Path) -> None:
    error = _failure(tmp_path, function, **limits)
    assert error.code == code and error.retryable is (code == "ENGINE_TIMEOUT")


@posix_only
def test_binding_cannot_create_threads_or_processes(installed_bindings: Path, tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("privileged: nothing is launched at all")
    assert _recognize(tmp_path, "spawn").pages[0].blocks[0].text == "thread-refused fork-refused"


def test_in_process_binding_objects_are_never_accepted() -> None:
    class InProcess:
        def ready(self) -> bool:
            return True

        def recognize(self, payload: bytes, **_: object) -> dict[str, object]:
            raise AssertionError("called in-process")

    with pytest.raises(TypeError, match="ENGINE_BINDING_INVALID"):
        PaddleOcrAdapter(InProcess())  # type: ignore[arg-type]
    for target in ("os.system", "pkg:fn:x", "../evil:fn", "pkg:", ":fn", "pkg fn:x", "pkg:fn()"):
        with pytest.raises(ValueError, match="ENGINE_BINDING_TARGET_INVALID"):
            engines.EngineBinding(target)
    worker = importlib.import_module("pados.packs.ocr._binding_worker")
    assert worker.TARGET_PATTERN == _isolated.BINDING_TARGET.pattern
    source = Path(engines.__file__).read_text(encoding="utf-8")
    assert ".recognize(" not in source  # no in-process binding call path remains


def test_binding_worker_applies_limits_before_reading_or_importing(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = importlib.import_module("pados.packs.ocr._binding_worker")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"never read")))
    monkeypatch.setattr(worker.importlib, "import_module", lambda name: pytest.fail("imported"))
    assert worker.main(["pados-rlimits/2:bogus", f"{BINDING_MODULE}:recognize"]) == 117
    assert worker.main([f"{BINDING_MODULE}:recognize"]) == 117
