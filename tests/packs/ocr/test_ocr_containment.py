"""CR-15b OCR-08 / OCR-09 / OCR-10 / OCR-01 (decoded-text budget): process containment.

* OCR-08: no ``preexec_fn``; the PDF worker applies its own limits first and refuses without them; an
  engine binary gets them from the ``_rlimit_exec`` launcher; the wall deadline covers startup; a
  multithreaded caller holding a lock still launches within the deadline.
* OCR-09: on Windows the runner fails closed without spawning, and isolation-dependent adapters are
  unavailable with ``OCR_ISOLATION_UNAVAILABLE`` (``sys.platform`` is monkeypatched; nothing runs).
* OCR-10: the PDF worker cannot create a process or thread (``RLIMIT_NPROC = 0``), and neither can the
  engine: since CR-15c that is mandatory, so the ``setsid()`` escape that the guarantee
  (``ORIGINAL_PROCESS_GROUP``) excludes cannot even be attempted (the fork is refused).
* OCR-01: decoded text and the number of XObjects examined are budgeted inside the worker.

New pack symbols are read through their modules at call time, so this file also collects against the
pre-fix source (the "before" run). No real engine is used; synthetic PDFs are built in ``tmp_path``.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from ocr_support import ROOT_DOCS, fixture_bytes, load_builder, permitted_context, put_source, run_stage, task

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageRequest
from pados.capability.artifacts import ObjectStoreArtifacts
from pados.capability.policy import PolicyCode, evaluate
from pados.packs.ocr import _isolated, _pdf_worker
from pados.packs.ocr._isolated import BoundedRunError, ProcessBudget, RunFailure, run_bounded
from pados.packs.ocr.descriptors import OCR_CAPABILITIES
from pados.packs.ocr.engines import TesseractCliAdapter, TesseractConfig
from pados.packs.ocr.reference import (
    DocumentPreprocessAdapter,
    HeuristicStructureAdapter,
    LineLayoutAdapter,
    XObjectFigureAdapter,
)
from pados.packs.ocr.text_layer import PdfTextLayerAdapter

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits / process groups")
unprivileged = pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                                  reason="RLIMIT_NPROC is not enforced for a privileged uid")
MIB = 1024 * 1024
PACK = Path(__file__).resolve().parents[3] / "src" / "pados" / "packs" / "ocr"
# A launched engine binary may not be reached through a symlink (CR-15c OCR-08): the venv's python is one.
ENGINE_PYTHON = os.path.realpath(sys.executable)


def _launcher() -> Any:
    return importlib.import_module("pados.packs.ocr._rlimit_exec")


def _token(**overrides: Any) -> str:
    return str(_isolated.limits_token(ProcessBudget(**overrides)))


def _pdf_bytes() -> bytes:
    return fixture_bytes("text_and_scan.pdf")


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state not in {"Z", "X"}


# --- OCR-08: limits are applied by the child itself; no Python runs between fork and exec ----------------

def test_no_pack_module_uses_preexec_fn() -> None:
    for module in sorted(PACK.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        assert "preexec_fn=" not in source.replace(" ", ""), module.name


class _Recorder:
    """Stands in for ``resource`` limits and stdin: records the order of every action, applies nothing."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.table: dict[int, tuple[int, int]] = {}

    def getrlimit(self, kind: int) -> tuple[int, int]:
        return self.table.get(kind, (-1, -1))

    def setrlimit(self, kind: int, value: tuple[int, int]) -> None:
        self.events.append(f"setrlimit:{kind}")
        self.table[kind] = value

    def read(self) -> bytes:
        self.events.append("stdin")
        header = ('{"op":"inspect","max_pages":5,"max_stream_bytes":1048576,'
                  '"max_declared_stream_bytes":1048576}')
        return header.encode() + b"\n%PDF-1.4 not a pdf"


class _Stream:
    def __init__(self, buffer: Any) -> None:
        self.buffer = buffer


@posix_only
def test_worker_applies_every_limit_before_it_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    recorder = _Recorder()
    monkeypatch.setattr(resource, "setrlimit", recorder.setrlimit)  # never limit the test process itself
    monkeypatch.setattr(resource, "getrlimit", recorder.getrlimit)
    monkeypatch.setattr(_launcher(), "lock_privileges", lambda: recorder.events.append("no_new_privs") or True)
    monkeypatch.setattr(sys, "stdin", _Stream(recorder))
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", _Stream(out))
    assert _pdf_worker.main([_token(memory_bytes=300 * MIB, cpu_seconds=7, max_open_files=24)]) == 0
    kinds = [resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_FSIZE, resource.RLIMIT_NOFILE,
             resource.RLIMIT_CORE, resource.RLIMIT_NPROC]
    assert recorder.events == [f"setrlimit:{kind}" for kind in kinds] + ["no_new_privs", "stdin"]
    assert recorder.table[resource.RLIMIT_AS] == (300 * MIB, 300 * MIB)
    assert recorder.table[resource.RLIMIT_CPU] == (7, 8) and recorder.table[resource.RLIMIT_NPROC] == (0, 0)
    assert json.loads(out.getvalue()) == {"code": "PDF_UNREADABLE", "ok": False}


@posix_only
@pytest.mark.parametrize("failure", ["setrlimit-raises", "not-read-back", "malformed", "missing", "extra"])
def test_worker_refuses_to_run_when_limits_cannot_be_applied(failure: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    recorder = _Recorder()

    def refuse(kind: int, value: tuple[int, int]) -> None:
        raise OSError("EPERM")

    monkeypatch.setattr(resource, "setrlimit", refuse if failure == "setrlimit-raises" else lambda k, v: None)
    monkeypatch.setattr(sys, "stdin", _Stream(recorder))
    monkeypatch.setattr(_pdf_worker, "_reader", lambda data: pytest.fail("pypdf was reached"))
    argv = {"malformed": ["pados-rlimits/1:as=1"], "missing": [], "extra": [_token(), "--"]}.get(failure, [_token()])
    assert _pdf_worker.main(argv) == _launcher().EXIT_LIMITS_REFUSED
    assert recorder.events == []  # stdin was never read


@posix_only
def test_real_worker_without_valid_limits_exits_before_doing_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    command = [sys.executable, "-I", "-B", "-m", _isolated.PDF_WORKER_MODULE, "pados-rlimits/1:as=1,cpu=1"]
    with pytest.raises(BoundedRunError) as refused:
        run_bounded(command, stdin=b"{}\n", env={}, budget=ProcessBudget(), self_limiting=True)
    assert refused.value.reason is RunFailure.ISOLATION_UNAVAILABLE
    monkeypatch.setattr(_isolated, "pdf_worker_command", lambda budget: command)
    with pytest.raises(CapabilityAdapterError) as failed:
        _isolated.isolated_pdf("inspect", _pdf_bytes(), {"max_pages": 5})
    assert failed.value.code == "OCR_ISOLATION_UNAVAILABLE"


@posix_only
def test_launcher_applies_the_limits_and_execs_only_an_absolute_binary(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.write_text("\n".join([
        f"#!{sys.executable} -I", "import json, resource, sys",
        "kinds = (resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_FSIZE, resource.RLIMIT_NOFILE, "
        "resource.RLIMIT_CORE, resource.RLIMIT_NPROC)",
        "print(json.dumps({'soft': [resource.getrlimit(k)[0] for k in kinds],"
        " 'hard': [resource.getrlimit(k)[1] for k in kinds], 'argv': sys.argv[1:]}))", ""]), encoding="utf-8")
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
    budget = ProcessBudget(cpu_seconds=9, memory_bytes=320 * MIB, max_open_files=40)
    run = run_bounded([str(probe), "--psm", "3"], stdin=b"", env={}, budget=budget)  # probe: a regular file
    report = json.loads(run.stdout)
    assert report["soft"] == [320 * MIB, 9, 0, 40, 0, 0]
    assert report["hard"] == [320 * MIB, 10, 0, 40, 0, 0]  # hard limits lowered too: the binary cannot raise them
    assert report["argv"] == ["--psm", "3"]
    # The launcher itself refuses a relative binary or malformed limits before setting anything.
    assert _launcher().main([_token(), "--", "tesseract"]) == _launcher().EXIT_LIMITS_REFUSED
    assert _launcher().main(["pados-rlimits/2:bogus", "--", str(probe)]) == _launcher().EXIT_LIMITS_REFUSED
    assert _launcher().main([_token(), str(probe)]) == _launcher().EXIT_LIMITS_REFUSED


@posix_only
def test_launch_from_a_multithreaded_parent_holding_a_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    held, release = threading.Lock(), threading.Event()

    def holder() -> None:
        with held:
            release.wait(30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    spawned: list[dict[str, Any]] = []
    real_popen = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        spawned.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    try:
        assert held.locked() and threading.active_count() >= 2
        started = time.monotonic()
        run = run_bounded([ENGINE_PYTHON, "-I", "-B", "-c", "print('ok')"], stdin=b"", env={},
                          budget=ProcessBudget(wall_seconds=10.0))
        assert run.returncode == 0 and run.stdout.strip() == b"ok"
        assert time.monotonic() - started < 10.0
    finally:
        release.set()
        thread.join(5)
    assert len(spawned) == 1
    assert spawned[0].get("preexec_fn") is None and spawned[0]["start_new_session"] is True


@posix_only
def test_wall_deadline_starts_before_the_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    real_start = _isolated._start  # noqa: SLF001

    def slow_start(*args: Any) -> Any:  # startup that outlives the whole wall budget
        proc = real_start(*args)
        time.sleep(0.8)
        return proc

    monkeypatch.setattr(_isolated, "_start", slow_start)
    started = time.monotonic()
    with pytest.raises(BoundedRunError) as overrun:
        run_bounded([ENGINE_PYTHON, "-I", "-B", "-c", "pass"], stdin=b"", env={},
                    budget=ProcessBudget(wall_seconds=0.5))
    assert overrun.value.reason is RunFailure.TIMEOUT
    assert time.monotonic() - started < 5


# --- OCR-09: Windows fails closed without spawning anything -------------------------------------------------

ISOLATED_ADAPTERS = ("ocr.preprocess.reference", "ocr.pdf-text-layer", "ocr.figure.pdf-xobject", "ocr.tesseract-cli")


def _fake_engine(tmp_path: Path) -> Path:
    script = tmp_path / "tesseract"
    script.write_text(f"#!{sys.executable} -I\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_windows_fails_closed_without_spawning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ObjectStoreArtifacts(tmp_path / "store")
    png = put_source(store, fixture_bytes("scan_page.png"), "image/png")
    tesseract = TesseractCliAdapter(TesseractConfig(executable=_fake_engine(tmp_path), engine_version="5.4.1"))
    adapters = [DocumentPreprocessAdapter(), PdfTextLayerAdapter(), XObjectFigureAdapter(), tesseract,
                LineLayoutAdapter(), HeuristicStructureAdapter()]
    if sys.platform != "win32":
        assert all(adapter.available() for adapter in adapters)  # all present before simulated Windows refusal
    else:
        assert all(adapter.available() is (adapter.descriptor.adapter_id not in ISOLATED_ADAPTERS)
                   for adapter in adapters)

    def no_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("a process was spawned")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(BoundedRunError) as refused:
        run_bounded([sys.executable, "-c", "pass"], stdin=b"", env={}, budget=ProcessBudget())
    assert refused.value.reason.value == "ISOLATION_UNAVAILABLE"
    with pytest.raises(CapabilityAdapterError) as failed:
        _isolated.isolated_pdf("inspect", _pdf_bytes(), {"max_pages": 5})
    assert failed.value.code == "OCR_ISOLATION_UNAVAILABLE"
    for adapter in adapters:
        isolated = adapter.descriptor.adapter_id in ISOLATED_ADAPTERS
        assert adapter.available() is not isolated, adapter.descriptor.adapter_id
        assert getattr(adapter, "unavailable_reason")() == ("OCR_ISOLATION_UNAVAILABLE" if isolated else None)
        capability = OCR_CAPABILITIES[adapter.descriptor.capability_id]
        decision = evaluate(capability, adapter.descriptor, permitted_context(adapter),
                            adapter_available=adapter.available)
        assert (decision.code is PolicyCode.ADAPTER_UNAVAILABLE) is isolated
    # Even when called directly (bypassing the policy), the image path of preprocess refuses up front.
    request = StageRequest("preprocess", "DOCUMENT_PREPROCESS", (png,), task(png).stage_parameters(),
                           task(png).classification)
    context = StageContext(artifacts=store, project_id="proj-ocr", stage_id="preprocess")  # type: ignore[arg-type]
    with pytest.raises(CapabilityAdapterError) as direct:
        DocumentPreprocessAdapter().execute(request, context=context)
    assert direct.value.code == "OCR_ISOLATION_UNAVAILABLE"


def test_windows_runner_branch_was_removed() -> None:
    runner = (PACK / "_isolated.py").read_text(encoding="utf-8")
    for residue in ("tempfile", "_windows_overrun", "CREATE_NEW_PROCESS_GROUP", "time.sleep"):
        assert residue not in runner


# --- OCR-10: no descendants from the worker; the engine guarantee is the original process group ------------

@unprivileged
def test_pdf_worker_cannot_create_a_process_or_a_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "\n".join([
        "import os, sys, subprocess, threading",
        "from pados.packs.ocr import _pdf_worker as worker",
        "def probe(header, data):",
        "    result = {}",
        "    try:",
        "        child = os.fork()",
        "    except OSError:",
        "        result['fork'] = 'refused'",
        "    else:",
        "        if child == 0:",
        "            os._exit(0)",
        "        result['fork'] = 'created'",
        "    try:",
        "        subprocess.run([sys.executable, '-c', 'pass'], check=False)",
        "        result['spawn'] = 'created'",
        "    except OSError:",
        "        result['spawn'] = 'refused'",
        "    try:",
        "        threading.Thread(target=lambda: None).start()",
        "        result['thread'] = 'created'",
        "    except RuntimeError:",
        "        result['thread'] = 'refused'",
        "    return {'ok': True, 'result': result}",
        "worker._dispatch = lambda raw: probe(None, raw)",
        "sys.exit(worker.main())"])
    monkeypatch.setattr(_isolated, "pdf_worker_command",
                        lambda budget: [sys.executable, "-I", "-B", "-c", body, _isolated.limits_token(budget)])
    result = _isolated.isolated_pdf("inspect", _pdf_bytes(), {"max_pages": 5})
    assert result == {"fork": "refused", "spawn": "refused", "thread": "refused"}


def _engine(tmp_path: Path, body: str, **config: Any) -> tuple[Path, TesseractCliAdapter]:
    marks = tmp_path / "marks"
    marks.mkdir(exist_ok=True)
    script = tmp_path / "bin" / "tesseract"
    script.parent.mkdir(exist_ok=True)
    tsv = "\n".join("\t".join(row) for row in [
        ("level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top", "width", "height",
         "conf", "text"),
        ("1", "1", "0", "0", "0", "0", "0", "0", "64", "16", "-1", ""),
        ("5", "1", "1", "1", "1", "1", "2", "2", "20", "10", "91", "Item")])
    script.write_text("\n".join([f"#!{sys.executable} -I", "import os, sys, time", f"marks = {str(marks)!r}",
                                 "open(os.path.join(marks, 'pid-%d' % os.getpid()), 'w').close()",
                                 "sys.stdin.buffer.read()", body, f"sys.stdout.write({tsv!r})", ""]),
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return marks, TesseractCliAdapter(TesseractConfig(executable=script, engine_version="5.4.1", **config))


def _recognize(tmp_path: Path, adapter: TesseractCliAdapter) -> None:
    store = ObjectStoreArtifacts(tmp_path / "store")
    source = put_source(store, fixture_bytes("scan_page.png"), "image/png")
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), task(source)).outputs[0]
    run_stage(adapter, store, (source, manifest), task(source))


FORK = "\n".join([
    "try:",
    "    child = os.fork()",
    "except OSError:",
    "    open(os.path.join(marks, 'fork-refused'), 'w').close()",
    "else:",
    "    if child == 0:",
    "        os.setsid()",  # leaves the original process group and session
    "        open(os.path.join(marks, 'escaped-%d' % os.getpid()), 'w').close()",
    "        os.close(0); os.close(1); os.close(2)",
    "        time.sleep(120)",
    "        os._exit(0)",
    "    deadline = time.monotonic() + 5",
    "    while not [n for n in os.listdir(marks) if n.startswith('escaped-')] and time.monotonic() < deadline:",
    "        time.sleep(0.01)"])


@unprivileged
def test_engine_cannot_create_descendants_by_default(tmp_path: Path) -> None:
    marks, adapter = _engine(tmp_path, FORK)
    _recognize(tmp_path, adapter)
    assert (marks / "fork-refused").exists()
    assert not list(marks.glob("escaped-*"))


@unprivileged
def test_setsid_escape_cannot_be_attempted(tmp_path: Path) -> None:
    """The group kill would not reach a descendant that left the original group; RLIMIT_NPROC = 0 (now
    mandatory) means no such descendant can exist: the fork that would create it is refused."""
    marks, adapter = _engine(tmp_path, FORK)
    escaped: list[int] = []
    try:
        _recognize(tmp_path, adapter)
        escaped = [int(p.name.split("-")[1]) for p in marks.glob("escaped-*")]
        assert escaped == [] and (marks / "fork-refused").exists()
        assert len(list(marks.glob("pid-*"))) == 1
        assert getattr(_isolated, "CONTAINMENT_GUARANTEE", None) == "ORIGINAL_PROCESS_GROUP"
        doc = " ".join(ROOT_DOCS.read_text(encoding="utf-8").split())
        assert "descendants that stay in the original process group" in doc
        assert "deny_process_creation" not in doc.replace("`deny_process_creation` no longer exists", "")
    finally:
        for pid in escaped:
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while any(_alive(pid) for pid in escaped) and time.monotonic() < deadline:
            time.sleep(0.02)


def test_engine_environment_is_single_threaded() -> None:
    from pados.packs.ocr.engines import tesseract_environment

    assert tesseract_environment() == {"OMP_THREAD_LIMIT": "1", "LC_ALL": "C"}


def test_no_unconditional_descendant_claims_remain() -> None:
    texts = [ROOT_DOCS.read_text(encoding="utf-8")] + [m.read_text(encoding="utf-8") for m in PACK.glob("*.py")]
    for text in texts:
        flat = " ".join(text.split()).lower()
        assert "no descendant outlives" not in flat and "no descendant survives" not in flat


# --- OCR-01: decoded-text and XObject budgets inside the worker ---------------------------------------------

def _text_layer(tmp_path: Path, data: bytes, budget: ProcessBudget) -> Any:
    store = ObjectStoreArtifacts(tmp_path)
    source = put_source(store, data, "application/pdf")
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), task(source)).outputs[0]
    return run_stage(PdfTextLayerAdapter(budget=budget), store, (source, manifest), task(source))


@posix_only
def test_decoded_text_is_budgeted_per_page_and_in_total(tmp_path: Path) -> None:
    builder = load_builder()
    dense = builder.long_text_pdf(1, 60, 6)  # one page with ~4 KiB of decoded layout text
    with pytest.raises(CapabilityAdapterError) as per_page:
        _text_layer(tmp_path / "a", dense, ProcessBudget(max_page_text_bytes=1024))
    assert per_page.value.code == "OCR_PARSE_BUDGET_EXCEEDED"
    pages = builder.long_text_pdf(3, 12, 6)  # three pages of < 1 KiB each
    with pytest.raises(CapabilityAdapterError) as running:
        _text_layer(tmp_path / "b", pages, ProcessBudget(max_page_text_bytes=1024, max_text_bytes=1500))
    assert running.value.code == "OCR_PARSE_BUDGET_EXCEEDED"
    within = _text_layer(tmp_path / "c", pages, ProcessBudget(max_page_text_bytes=1024, max_text_bytes=4096))
    assert within.evidence["pages"] == 3 and within.evidence["pages_failed"] == 0


@posix_only
def test_figure_scan_bounds_the_xobjects_it_examines(tmp_path: Path) -> None:
    builder = load_builder()
    data = builder.many_xobjects_pdf(40)
    store = ObjectStoreArtifacts(tmp_path)
    source = put_source(store, data, "application/pdf")
    ocr_task = task(source)
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), ocr_task).outputs[0]
    document = run_stage(PdfTextLayerAdapter(), store, (source, manifest), ocr_task).outputs[0]
    for budget in (ProcessBudget(max_xobjects_per_page=10), ProcessBudget(max_xobjects=10)):
        with pytest.raises(CapabilityAdapterError) as exceeded:
            run_stage(XObjectFigureAdapter(budget=budget), store, (source, document), ocr_task)
        assert exceeded.value.code == "OCR_PARSE_BUDGET_EXCEEDED"
    listed = run_stage(XObjectFigureAdapter(budget=ProcessBudget(max_xobjects_per_page=40)), store,
                       (source, document), ocr_task)
    assert listed.evidence["figures"] == 40
