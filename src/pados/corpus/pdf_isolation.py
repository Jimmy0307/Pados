# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, isolated runner for untrusted text-PDF parsing (P2-11).

pypdf never parses an untrusted document in the API process. ``run_pdf_parse`` starts
``sys.executable -I -B -m pados.corpus._pdf_parse_worker <limits>`` (isolated mode: no user site, no
environment-driven imports, no cwd on ``sys.path``; no ``.pyc`` writes) with a fixed argv,
``shell=False``, a minimal environment, the PDF bytes on stdin and stdout drained under a hard cap.
It ports the OCR pack's ``_isolated``/``_pdf_worker`` pattern into Core (Core may not import a pack) and
the DI-1 model worker's Windows handling:

* every platform: a wall-clock deadline that starts BEFORE the spawn, a stdout byte cap, stderr
  discarded, an input-size cap, a page cap and the pypdf decode budget (applied in the child), a polled
  resident-memory watchdog over the worker's process tree (``psutil``), and a bounded number of
  concurrent workers. On any overrun the whole worker tree is killed and reaped (bounded);
* POSIX: the child is started in its own session with NO ``preexec_fn`` and applies its own rlimits as
  its first action (Linux: ``RLIMIT_AS``, ``RLIMIT_CPU``, ``RLIMIT_FSIZE = 0``, ``RLIMIT_NOFILE``,
  ``RLIMIT_CORE = 0``, ``RLIMIT_NPROC = 0``; it refuses to run if any does not hold). On overrun its
  process group is SIGKILLed;
* Windows: ``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`` (the DI-1 model worker's flags). Windows has
  no ``RLIMIT``: memory is bounded by the polled watchdog (not a kernel cap) and CPU by the wall clock.
  A Job Object is the production follow-up (owner decision, as for the OCR pack).

Every failure is a closed ``PDF_*`` code; no child output, stderr, exception text or path is surfaced.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, Any

from . import _pdf_parse_worker as worker

WORKER_MODULE = "pados.corpus._pdf_parse_worker"
MIB = 1024 * 1024
_CHUNK = 64 * 1024
_POLL_SECONDS = 0.05
_TREE_EVERY = 10  # re-walk the worker's process tree every 10 polls (0.5 s)
REAP_SECONDS = 5.0
MAX_CONCURRENT_WORKERS = 2
_OUTPUT_OVERHEAD = 64 * 1024  # JSON structure around the measured text

PDF_SOURCE_TOO_LARGE = "PDF_SOURCE_TOO_LARGE"
PDF_SOURCE_UNREADABLE = "PDF_SOURCE_UNREADABLE"
PDF_PARSE_TIMEOUT = "PDF_PARSE_TIMEOUT"
PDF_PARSE_BUSY = "PDF_PARSE_BUSY"
PDF_PARSE_MEMORY_EXCEEDED = "PDF_PARSE_MEMORY_EXCEEDED"
PDF_PARSE_CPU_EXCEEDED = "PDF_PARSE_CPU_EXCEEDED"
PDF_PARSE_OUTPUT_EXCEEDED = "PDF_PARSE_OUTPUT_EXCEEDED"
PDF_PARSE_WORKER_FAILED = "PDF_PARSE_WORKER_FAILED"
PDF_ISOLATION_UNAVAILABLE = "PDF_ISOLATION_UNAVAILABLE"
# Content failures: the document itself is malformed. Everything else in FAILURE_CODES is a bound that
# was hit or a worker that could not run: callers must admit nothing from such a parse.
CONTENT_FAILURE_CODES = frozenset({worker.PDF_MALFORMED, worker.PDF_PARSE_ERROR})
RESOURCE_FAILURE_CODES = frozenset({
    worker.PDF_DECODE_BUDGET_EXCEEDED, worker.PDF_PAGE_COUNT_EXCEEDED, worker.PDF_TEXT_BUDGET_EXCEEDED,
    PDF_SOURCE_TOO_LARGE, PDF_SOURCE_UNREADABLE, PDF_PARSE_TIMEOUT, PDF_PARSE_BUSY, PDF_PARSE_MEMORY_EXCEEDED,
    PDF_PARSE_CPU_EXCEEDED, PDF_PARSE_OUTPUT_EXCEEDED, PDF_PARSE_WORKER_FAILED, PDF_ISOLATION_UNAVAILABLE,
})
FAILURE_CODES = CONTENT_FAILURE_CODES | RESOURCE_FAILURE_CODES

_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_WORKERS)

if sys.platform != "win32":
    import signal

    _CPU_SIGNALS = frozenset({-signal.SIGXCPU})  # an unexplained SIGKILL stays PDF_PARSE_WORKER_FAILED


@dataclass(frozen=True)
class PdfParseBudget:
    """Hard limits for one isolated parse (validated on construction).

    Text budgets are measured on the JSON-encoded page text, so the stdout cap derived from them is exact.
    Content streams carry operators besides the text, so the per-stream decode cap is larger than the
    per-page text cap.
    """

    wall_seconds: float = 120.0
    cpu_seconds: int = 120
    memory_bytes: int = 1024 * MIB
    max_input_bytes: int = 64 * MIB
    max_pages: int = 10_000
    max_page_text_bytes: int = 8 * MIB
    max_text_bytes: int = 48 * MIB
    max_stream_bytes: int = 64 * MIB
    max_content_bytes: int = 384 * MIB
    max_open_files: int = 32

    def __post_init__(self) -> None:
        if not (math.isfinite(self.wall_seconds) and 0.1 <= self.wall_seconds <= 600
                and 1 <= self.cpu_seconds <= 600 and 64 * MIB <= self.memory_bytes <= 64 * 1024 ** 3
                and 1 <= self.max_input_bytes <= 1024 ** 3 and 1 <= self.max_pages <= 1_000_000
                and 1024 <= self.max_page_text_bytes <= self.max_text_bytes <= 256 * MIB
                and 1024 <= self.max_stream_bytes <= self.max_content_bytes <= 4 * 1024 ** 3
                and 16 <= self.max_open_files <= 1024):
            raise ValueError("PDF_PARSE_BUDGET_INVALID")

    @property
    def max_output_bytes(self) -> int:
        return self.max_text_bytes + _OUTPUT_OVERHEAD


PDF_PARSE_BUDGET = PdfParseBudget()


@dataclass(frozen=True)
class PdfParseOutcome:
    """Either ``code`` (a closed failure) or ``result`` (the worker's validated reply)."""

    code: str | None
    result: dict[str, Any] | None = None


def worker_command(budget: PdfParseBudget) -> list[str]:
    limits = worker.encode_limits(budget.memory_bytes, budget.cpu_seconds, budget.max_open_files)
    return [sys.executable, "-I", "-B", "-m", WORKER_MODULE, limits]


def worker_environment() -> dict[str, str]:
    """No inherited variables (and so no credentials). Windows needs ``SYSTEMROOT`` to start Python."""
    if sys.platform == "win32" and "SYSTEMROOT" in os.environ:
        return {"SYSTEMROOT": os.environ["SYSTEMROOT"]}
    return {}


def _header(budget: PdfParseBudget) -> bytes:
    return json.dumps({"op": "parse", "max_pages": budget.max_pages,
                       "max_page_text_bytes": budget.max_page_text_bytes, "max_text_bytes": budget.max_text_bytes,
                       "max_stream_bytes": budget.max_stream_bytes, "max_content_bytes": budget.max_content_bytes,
                       "max_declared_stream_bytes": budget.max_input_bytes},
                      sort_keys=True, separators=(",", ":")).encode("ascii")


def _start(budget: PdfParseBudget) -> subprocess.Popen[bytes] | None:
    try:
        if sys.platform == "win32":
            return subprocess.Popen(worker_command(budget), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, shell=False, env=worker_environment(),
                                    close_fds=True,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        return subprocess.Popen(worker_command(budget), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, shell=False, env=worker_environment(), close_fds=True,
                                start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _tree(pid: int, *, descendants: bool = True) -> list[Any]:
    """The worker and its descendants (on Windows a venv ``python.exe`` launches the real interpreter)."""
    import psutil  # type: ignore[import-untyped]  # declared runtime dependency; no stubs are pinned

    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)] if descendants else [root]
    except psutil.Error:
        return []


def _tree_rss(pid: int, members: list[Any] | None = None) -> int:
    import psutil

    total = 0
    for member in members if members is not None else _tree(pid):
        try:
            total += int(member.memory_info().rss)
        except psutil.Error:
            continue
    return total


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill every member of a worker tree that is still running; POSIX also SIGKILLs its process group.

    A worker that already exited has been reaped by ``wait``: its pid may be reused, so nothing is signalled
    (it could start no descendant: ``RLIMIT_NPROC = 0`` on Linux, and on Windows the venv launcher only
    exits after the interpreter it started). An unreaped worker's pid cannot be reused (POSIX zombie,
    Windows process handle held by ``Popen``)."""
    import psutil

    if proc.poll() is not None:
        return
    members = _tree(proc.pid)
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for member in reversed(members):
        try:
            member.kill()
        except psutil.Error:
            continue
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _feed(stream: IO[bytes], payload: bytes) -> None:
    view = memoryview(payload)
    try:
        for offset in range(0, len(view), _CHUNK):
            stream.write(view[offset:offset + _CHUNK])
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass  # the child closed its stdin (or was killed); its reply decides
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _drain(stream: IO[bytes], sink: bytearray, cap: int, overflow: threading.Event) -> None:
    fd = stream.fileno()
    try:
        while True:
            chunk = os.read(fd, min(_CHUNK, cap - len(sink) + 1))
            if not chunk:
                return
            sink.extend(chunk)
            if len(sink) > cap:
                overflow.set()
                return
    except OSError:
        return


def _watch(proc: subprocess.Popen[bytes], budget: PdfParseBudget, deadline: float,
           overflow: threading.Event) -> str | None:
    """Wait for the worker under every bound; the closed code of the first bound hit, else ``None``.

    Resident memory is polled every ``_POLL_SECONDS``. The process tree (a whole-host enumeration on
    Windows) is re-walked only every ``_TREE_EVERY`` polls; POSIX workers can have no descendants."""
    members: list[Any] = []
    for poll in itertools.count():
        try:
            proc.wait(timeout=_POLL_SECONDS)
            return None
        except subprocess.TimeoutExpired:
            pass
        if overflow.is_set():
            return PDF_PARSE_OUTPUT_EXCEEDED
        if time.monotonic() >= deadline:
            return PDF_PARSE_TIMEOUT
        if poll % _TREE_EVERY == 0 or not members:
            members = _tree(proc.pid, descendants=sys.platform == "win32")
        if _tree_rss(proc.pid, members) > budget.memory_bytes:
            return PDF_PARSE_MEMORY_EXCEEDED
    return None  # pragma: no cover - itertools.count() never ends


def _exit_code(returncode: int) -> str | None:
    if returncode == worker.EXIT_OK:
        return None
    if returncode == worker.EXIT_MEMORY:
        return PDF_PARSE_MEMORY_EXCEEDED
    if returncode == worker.EXIT_LIMITS_REFUSED:
        return PDF_ISOLATION_UNAVAILABLE
    if sys.platform != "win32" and returncode in _CPU_SIGNALS:
        return PDF_PARSE_CPU_EXCEEDED
    return PDF_PARSE_WORKER_FAILED


def _run(payload: bytes, budget: PdfParseBudget, deadline: float) -> PdfParseOutcome:
    proc = _start(budget)
    if proc is None:
        return PdfParseOutcome(PDF_ISOLATION_UNAVAILABLE)
    assert proc.stdin is not None and proc.stdout is not None
    stdout = bytearray()
    overflow = threading.Event()
    feeder = threading.Thread(target=_feed, args=(proc.stdin, payload), daemon=True, name="pados-pdf-feed")
    reader = threading.Thread(target=_drain, args=(proc.stdout, stdout, budget.max_output_bytes, overflow),
                              daemon=True, name="pados-pdf-drain")
    code: str | None = None
    try:
        feeder.start()
        reader.start()
        code = _watch(proc, budget, deadline, overflow)
        if code is None:
            reader.join(max(0.0, deadline - time.monotonic()))
            if reader.is_alive():  # the pipe is still held open past the deadline
                code = PDF_PARSE_TIMEOUT
            elif overflow.is_set():
                code = PDF_PARSE_OUTPUT_EXCEEDED
    finally:
        _kill_tree(proc)  # on overrun AND on success: nothing of the worker tree survives
        try:
            proc.wait(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:
            code = code or PDF_PARSE_TIMEOUT
        for thread in (feeder, reader):
            if thread.ident is not None:
                thread.join(REAP_SECONDS)
        for stream, thread in ((proc.stdin, feeder), (proc.stdout, reader)):
            if thread.is_alive():  # still blocked on the pipe: closing could block or reuse its fd
                continue
            try:
                stream.close()
            except OSError:
                pass
    if code is not None:
        return PdfParseOutcome(code)
    failure = _exit_code(proc.returncode)
    if failure is not None:
        return PdfParseOutcome(failure)
    return _decode(bytes(stdout))


def _decode(stdout: bytes) -> PdfParseOutcome:
    try:
        reply = json.loads(stdout.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return PdfParseOutcome(PDF_PARSE_WORKER_FAILED)
    if not isinstance(reply, dict) or set(reply) not in ({"ok", "result"}, {"ok", "code"}):
        return PdfParseOutcome(PDF_PARSE_WORKER_FAILED)
    if reply["ok"] is not True:
        refused = reply.get("code")
        return PdfParseOutcome(refused if isinstance(refused, str) and refused in worker.WORKER_CODES
                               else PDF_PARSE_WORKER_FAILED)
    result = reply.get("result")
    return PdfParseOutcome(None, result) if isinstance(result, dict) else PdfParseOutcome(PDF_PARSE_WORKER_FAILED)


def run_pdf_parse(payload: bytes, budget: PdfParseBudget = PDF_PARSE_BUDGET) -> PdfParseOutcome:
    """Parse ``payload`` in the isolated worker under ``budget``; never raises for hostile input."""
    if len(payload) > budget.max_input_bytes:
        return PdfParseOutcome(PDF_SOURCE_TOO_LARGE)
    deadline = time.monotonic() + budget.wall_seconds  # BEFORE the spawn: waiting and startup count
    if not _SLOTS.acquire(timeout=max(0.0, deadline - time.monotonic())):
        return PdfParseOutcome(PDF_PARSE_BUSY)
    try:
        return _run(_header(budget) + b"\n" + payload, budget, deadline)
    finally:
        _SLOTS.release()


__all__ = ["CONTENT_FAILURE_CODES", "FAILURE_CODES", "PDF_PARSE_BUDGET", "RESOURCE_FAILURE_CODES",
           "PdfParseBudget", "PdfParseOutcome", "run_pdf_parse", "worker_command", "worker_environment"]
