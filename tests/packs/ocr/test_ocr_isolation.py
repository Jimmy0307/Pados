"""CR-15 OCR-01 / OCR-02: untrusted parsing and engine binaries run in a bounded child process.

The expensive workloads are TEST-ONLY hooks: ``pdf_worker_command`` is monkeypatched to a small Python
program that runs the REAL worker entry point (``_pdf_worker.main``, so the real limits are applied
first) with every operation replaced by the workload, and the Tesseract adapter is pointed at a fake
executable written to ``tmp_path``. No real engine is used or installed. Every child records its PID
(an empty marker file: RLIMIT_FSIZE = 0 still allows creating empty files) so the tests can prove that
no process survives a call; every workload first tries to fork and records that the fork was refused
(RLIMIT_NPROC = 0).
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from ocr_support import fixture_bytes, put_source, run_stage, task

from pados.capability.adapter import CapabilityAdapter, CapabilityAdapterError
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.packs.ocr import _isolated
from pados.packs.ocr._isolated import (
    BoundedRunError,
    ProcessBudget,
    RunFailure,
    minimal_environment,
    run_bounded,
)
from pados.packs.ocr._pdf_worker import CONTROL_PATTERN
from pados.packs.ocr.contracts import _CONTROL, OcrDocument, PageStatus
from pados.packs.ocr.engines import TesseractCliAdapter, TesseractConfig
from pados.packs.ocr.reference import DocumentPreprocessAdapter, XObjectFigureAdapter
from pados.packs.ocr.text_layer import PdfTextLayerAdapter

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits / process groups; Windows PENDING")
MIB = 1024 * 1024
ENGINE_PYTHON = os.path.realpath(sys.executable)  # an engine binary may not be reached through a symlink
TIGHT = ProcessBudget(wall_seconds=20.0, cpu_seconds=20, memory_bytes=256 * MIB, max_stdout_bytes=4096,
                      max_stderr_bytes=4096)


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state not in {"Z", "X"}


def _assert_all_dead(marks: Path, expected: int) -> None:
    pids = [int(p.name.split("-")[1]) for p in marks.glob("pid-*")]
    assert len(pids) == expected, pids
    deadline = time.monotonic() + 5.0
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [pid for pid in pids if _alive(pid)]


def _worker_hook(marks: Path, body: str, budget: ProcessBudget) -> list[str]:
    """A test-only worker: records its PID, runs the real ``_pdf_worker.main`` (limits first) with every
    operation replaced by ``work``, which tries to fork and then runs ``body``."""
    lines = ["import os, sys, time",
             f"marks = {str(marks)!r}",
             "open(os.path.join(marks, 'pid-%d' % os.getpid()), 'w').close()",
             "from pados.packs.ocr import _pdf_worker as worker",
             "def work(header, data):",
             "    try:",
             "        child = os.fork()",
             "    except OSError:",
             "        open(os.path.join(marks, 'fork-refused'), 'w').close()",
             "    else:",
             "        if child == 0:",
             "            open(os.path.join(marks, 'pid-%d' % os.getpid()), 'w').close()",
             "            time.sleep(120)",
             "            os._exit(0)",
             *("    " + line for line in body.splitlines()),
             "for name in list(worker.OPERATIONS):",
             "    worker.OPERATIONS[name] = work",
             "sys.exit(worker.main())"]
    return [sys.executable, "-I", "-B", "-c", "\n".join(lines), _isolated.limits_token(budget)]


def _program(marks: Path, body: str) -> list[str]:
    """A plain child for ``run_bounded`` (wrapped in the rlimit launcher): PID marker, a fork attempt, ``body``."""
    lines = ["import os, sys, time",
             f"marks = {str(marks)!r}",
             "open(os.path.join(marks, 'pid-%d' % os.getpid()), 'w').close()",
             "try:",
             "    child = os.fork()",
             "except OSError:",
             "    open(os.path.join(marks, 'fork-refused'), 'w').close()",
             "else:",
             "    if child == 0:",
             "        open(os.path.join(marks, 'pid-%d' % os.getpid()), 'w').close()",
             "        os.close(0); os.close(1); os.close(2)",
             "        time.sleep(120)",
             "        os._exit(0)",
             "    deadline = time.monotonic() + 5",  # the parent waits until the child has registered itself
             "    while len([n for n in os.listdir(marks) if n.startswith('pid-')]) < 2 and time.monotonic() < deadline:",
             "        time.sleep(0.01)",
             body]
    return [ENGINE_PYTHON, "-I", "-B", "-c", "\n".join(lines)]


def _pdf_source(tmp_path: Path) -> tuple[ObjectStoreArtifacts, ArtifactRef]:
    store = ObjectStoreArtifacts(tmp_path / "store")
    return store, put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf")


def _failing_code(call: Callable[[], object]) -> CapabilityAdapterError:
    with pytest.raises(CapabilityAdapterError) as failed:
        call()
    assert failed.value.__cause__ is None and failed.value.__context__ is None
    return failed.value


WORKLOADS = {
    "memory": ("return {'x': bytearray(8 << 30)}", TIGHT, "OCR_PARSE_BUDGET_EXCEEDED"),
    "cpu": ("while True:\n    pass",
            ProcessBudget(wall_seconds=30.0, cpu_seconds=1, memory_bytes=256 * MIB), "OCR_PARSE_BUDGET_EXCEEDED"),
    "deadline": ("time.sleep(120)", ProcessBudget(wall_seconds=1.0, cpu_seconds=30, memory_bytes=256 * MIB),
                 "OCR_PARSE_TIMEOUT"),
    "stdout_flood": ("while True:\n    os.write(1, b'{' * 65536)", TIGHT, "OCR_PARSE_BUDGET_EXCEEDED"),
    "stderr_flood": ("while True:\n    os.write(2, b'E' * 65536)", TIGHT, "OCR_PARSE_BUDGET_EXCEEDED"),
}


@posix_only
@pytest.mark.parametrize("workload", sorted(WORKLOADS))
def test_expensive_pdf_worker_is_killed_by_its_budget(workload: str, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    body, budget, code = WORKLOADS[workload]
    marks = tmp_path / "marks"
    marks.mkdir()
    monkeypatch.setattr(_isolated, "pdf_worker_command", lambda b: _worker_hook(marks, body, b))
    store, source = _pdf_source(tmp_path)
    started = time.monotonic()
    error = _failing_code(lambda: run_stage(DocumentPreprocessAdapter(budget=budget), store, (source,),
                                            task(source)))
    assert error.code == code
    assert time.monotonic() - started < 20  # every budget fires well before the hook would finish
    assert (marks / "fork-refused").exists()  # RLIMIT_NPROC = 0: the worker could not fork
    _assert_all_dead(marks, expected=1)


@posix_only
@pytest.mark.parametrize("make", [PdfTextLayerAdapter, XObjectFigureAdapter], ids=["text-layer", "figures"])
def test_text_and_figure_parsing_share_the_bounded_worker(make: Callable[..., CapabilityAdapter], tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    store, source = _pdf_source(tmp_path)
    ocr_task = task(source)
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), ocr_task).outputs[0]
    inputs = (source, manifest)
    if make is XObjectFigureAdapter:
        inputs = (source, run_stage(PdfTextLayerAdapter(), store, inputs, ocr_task).outputs[0])
    marks = tmp_path / "marks"
    marks.mkdir()
    monkeypatch.setattr(_isolated, "pdf_worker_command", lambda b: _worker_hook(marks, "time.sleep(120)", b))
    budget = ProcessBudget(wall_seconds=1.0, cpu_seconds=30, memory_bytes=256 * MIB)
    error = _failing_code(lambda: run_stage(make(budget=budget), store, inputs, ocr_task))
    assert error.code == "OCR_PARSE_TIMEOUT" and error.retryable
    assert (marks / "fork-refused").exists()
    _assert_all_dead(marks, expected=1)


def test_oversized_source_is_refused_before_any_child_starts(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    store, source = _pdf_source(tmp_path)
    ocr_task = task(source)
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), ocr_task).outputs[0]
    document = run_stage(PdfTextLayerAdapter(), store, (source, manifest), ocr_task).outputs[0]
    started: list[object] = []

    def never(*args: object) -> None:
        started.append(args)
        raise AssertionError("a child process was started")

    monkeypatch.setattr(_isolated, "_run", never)
    small = ProcessBudget(max_input_bytes=source.size_bytes - 1)
    for adapter, inputs in [(DocumentPreprocessAdapter(budget=small), (source,)),
                            (PdfTextLayerAdapter(budget=small), (source, manifest)),
                            (XObjectFigureAdapter(budget=small), (source, document))]:
        error = _failing_code(lambda a=adapter, i=inputs: run_stage(a, store, i, ocr_task))
        assert error.code == "OCR_SOURCE_TOO_LARGE"
    with pytest.raises(CapabilityAdapterError, match="OCR_SOURCE_TOO_LARGE"):
        _isolated.isolated_pdf("inspect", b"%PDF-" + b"0" * 64, {}, budget=ProcessBudget(max_input_bytes=8))
    assert started == []


@posix_only
def test_malformed_worker_replies_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, source = _pdf_source(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    for reply, code in [("{\"ok\": true, \"result\": {}}", "OCR_PARSE_FAILED"),  # not canonical
                        ('{"code":"rm -rf","ok":false}', "OCR_PARSE_FAILED"),  # not a closed code
                        ('{"code":"PDF_ENCRYPTED","ok":false}', "PDF_ENCRYPTED"),
                        ('{"ok":true,"result":{"geometry":[],"page_count":true}}', "OCR_PARSE_FAILED")]:
        program = f"import sys; sys.stdout.write({reply!r}); sys.stdout.flush()"
        monkeypatch.setattr(_isolated, "pdf_worker_command",
                            lambda budget, p=program: [sys.executable, "-I", "-B", "-c", p])
        error = _failing_code(lambda: run_stage(DocumentPreprocessAdapter(), store, (source,), task(source)))
        assert error.code == code


LIMITS_PROBE = ("import json, os, resource; print(json.dumps({'limits': [resource.getrlimit(k)[0] for k in "
                "(resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_FSIZE, resource.RLIMIT_NOFILE, "
                "resource.RLIMIT_CORE, resource.RLIMIT_NPROC)], 'env': sorted(os.environ), 'leader': "
                "os.getpgid(0) == os.getpid() == os.getsid(0)}))")


@posix_only
def test_runner_applies_rlimits_and_a_minimal_environment() -> None:
    budget = ProcessBudget(cpu_seconds=7, memory_bytes=300 * MIB, max_open_files=24)
    run = run_bounded([ENGINE_PYTHON, "-I", "-B", "-c", LIMITS_PROBE], stdin=b"", env=minimal_environment(),
                      budget=budget)
    assert run.returncode == 0
    report = json.loads(run.stdout)
    assert report["limits"] == [300 * MIB, 7, 0, 24, 0, 0]  # applied by the launcher, then exec
    assert report["leader"] is True  # its own session and process group
    assert set(report["env"]) <= {"LC_CTYPE"}  # nothing inherited; LC_CTYPE is Python's own locale coercion
    with pytest.raises(TypeError):  # CR-15c OCR-10: RLIMIT_NPROC = 0 can no longer be relaxed
        ProcessBudget(deny_process_creation=False)  # type: ignore[call-arg]


@posix_only
def test_runner_cuts_output_at_the_cap_and_kills_the_group(tmp_path: Path) -> None:
    marks = tmp_path / "marks"
    marks.mkdir()
    flood = _program(marks, "while True:\n    os.write(1, b'x' * 65536)")
    with pytest.raises(BoundedRunError) as overrun:
        run_bounded(flood, stdin=b"", env={}, budget=ProcessBudget(max_stdout_bytes=1000, memory_bytes=256 * MIB))
    assert overrun.value.reason is RunFailure.STDOUT_LIMIT and overrun.value.__context__ is None
    assert (marks / "fork-refused").exists()
    _assert_all_dead(marks, expected=1)
    quiet = run_bounded([ENGINE_PYTHON, "-I", "-B", "-c", "import sys; sys.stdout.write(sys.stdin.read()[::-1])"],
                        stdin=b"abc" * 50_000, env={}, budget=ProcessBudget(max_stdout_bytes=150_000))
    assert quiet.returncode == 0 and quiet.stdout == b"cba" * 50_000  # stdin is streamed without deadlock
    with pytest.raises(BoundedRunError) as missing:
        run_bounded([str(tmp_path / "does-not-exist")], stdin=b"", env={}, budget=ProcessBudget())
    assert missing.value.reason.value == "EXECUTABLE_INVALID"  # validated before anything is spawned
    with pytest.raises(BoundedRunError) as relative:
        run_bounded(["python3", "-c", "pass"], stdin=b"", env={}, budget=ProcessBudget())
    assert relative.value.reason is RunFailure.START_FAILED


def test_worker_control_character_rule_matches_the_contract() -> None:
    assert CONTROL_PATTERN == _CONTROL.pattern


def test_process_budget_is_bounded() -> None:
    for bad in ({"wall_seconds": 0.0}, {"wall_seconds": float("inf")}, {"cpu_seconds": 0},
                {"memory_bytes": 1024}, {"max_stdout_bytes": 0}, {"max_open_files": 3}):
        with pytest.raises(ValueError, match="PROCESS_BUDGET_INVALID"):
            ProcessBudget(**bad)  # type: ignore[arg-type]


# --- OCR-02: Tesseract through the same runner (fake executable; real tesseract is never used) -----

def _fake_tesseract(directory: Path, body: str, marks: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "tesseract"
    script.write_text("\n".join([f"#!{sys.executable} -I", "import os, sys, time",
                                 f"open(os.path.join({str(marks)!r}, 'pid-%d' % os.getpid()), 'w').close()",
                                 body, ""]), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


TSV = "\n".join("\t".join(row) for row in [
    ("level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top", "width", "height", "conf",
     "text"),
    ("1", "1", "0", "0", "0", "0", "0", "0", "64", "16", "-1", ""),
    ("5", "1", "1", "1", "1", "1", "2", "2", "20", "10", "91", "Item"),
    ("5", "1", "1", "1", "1", "2", "30", "2", "20", "10", "89", "Pen")])


def _tesseract_run(tmp_path: Path, body: str, **config: object) -> tuple[Path, Callable[[], object]]:
    marks = tmp_path / "marks"
    marks.mkdir(exist_ok=True)
    script = _fake_tesseract(tmp_path / "bin", body, marks)
    adapter = TesseractCliAdapter(TesseractConfig(executable=script, engine_version="5.4.1",
                                                  **config))  # type: ignore[arg-type]
    assert adapter.available()
    store = ObjectStoreArtifacts(tmp_path / "store")
    source = put_source(store, fixture_bytes("scan_page.png"), "image/png")
    ocr_task = task(source)
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), ocr_task).outputs[0]

    def run() -> object:
        output = run_stage(adapter, store, (source, manifest), ocr_task)
        return OcrDocument.from_bytes(store.get(output.outputs[0]))

    return marks, run


@posix_only
def test_fake_tesseract_success_path_uses_stdin_and_parses_tsv(tmp_path: Path) -> None:
    body = ("data = sys.stdin.buffer.read()\n"
            "assert data.startswith(b'\\x89PNG') and sys.argv[1:3] == ['stdin', 'stdout']\n"
            "assert set(os.environ) <= {'OMP_THREAD_LIMIT', 'LC_ALL', 'LC_CTYPE'}\n"
            "assert os.environ['OMP_THREAD_LIMIT'] == '1'\n"
            f"sys.stdout.write({TSV!r})")
    marks, run = _tesseract_run(tmp_path, body)
    document = run()
    assert isinstance(document, OcrDocument)
    page = document.pages[0]
    assert page.status is PageStatus.RECOGNIZED and page.blocks[0].text == "Item Pen"
    _assert_all_dead(marks, expected=1)


@posix_only
@pytest.mark.parametrize(("body", "config", "code"), [
    ("while True:\n    os.write(1, b'x' * 65536)", {"max_output_bytes": 4096}, "ENGINE_OUTPUT_TOO_LARGE"),
    ("while True:\n    os.write(2, b'E' * 65536)", {"max_stderr_bytes": 1024}, "ENGINE_OUTPUT_TOO_LARGE"),
    ("time.sleep(120)", {"timeout_seconds": 1}, "ENGINE_TIMEOUT"),
    ("try:\n    os.fork()\nexcept OSError:\n    open(os.path.join(" + "{marks!r}" + ", 'fork-refused'), 'w').close()\n"
     "time.sleep(120)", {"timeout_seconds": 1}, "ENGINE_TIMEOUT"),
    # The fake engine would succeed if it could allocate 1 GiB: the memory rlimit makes it fail.
    (f"blob = bytearray(1 << 30)\nsys.stdout.write({TSV!r})", {"memory_mb": 256}, "ENGINE_FAILED"),
], ids=["stdout-flood", "stderr-flood", "hang", "hang-after-refused-fork", "memory"])
def test_fake_tesseract_overruns_are_cut_and_killed(body: str, config: dict[str, object], code: str,
                                                    tmp_path: Path) -> None:
    body = body.replace("{marks!r}", repr(str(tmp_path / "marks")))
    marks, run = _tesseract_run(tmp_path, body, **config)
    started = time.monotonic()
    error = _failing_code(run)
    assert error.code == code and time.monotonic() - started < 20
    if "fork" in body:
        assert (marks / "fork-refused").exists()  # RLIMIT_NPROC = 0, always
    _assert_all_dead(marks, expected=1)
