# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded child-process runner for untrusted parsing and operator-configured engine binaries.

Everything that parses an untrusted document with a third-party library (pypdf) or runs an engine
binary (Tesseract) goes through ``run_bounded``: a fixed argv, ``shell=False``, a minimal environment,
the input on stdin and the output drained incrementally under hard caps. Nothing is buffered without
a cap (no ``capture_output``, no ``communicate``) and no thread, timer or scheduler is created.

* POSIX: nothing is spawned by a privileged process (``isolation_refusal``: real/effective uid 0,
  or ``CAP_SYS_RESOURCE``/``CAP_SYS_ADMIN`` held, for which ``RLIMIT_NPROC`` is not enforced), and
  nothing is exec'd that fails validation (``_rlimit_exec.executable_refusal``: absolute, no
  symlinked component, regular, executable, not setuid/setgid, no file capabilities; the PADOS
  interpreter is checked by its resolved file). The child is started by a plain ``subprocess.Popen``
  (``_posixsubprocess`` fork+exec in C, NO ``preexec_fn``: no Python code runs between fork and exec,
  so a multithreaded caller cannot deadlock it) in its own session (``start_new_session``). The child
  then applies its own rlimits as its first action (``_rlimit_exec``): the PDF and binding workers do
  so themselves, and every other program is wrapped in the ``_rlimit_exec`` launcher, which sets them
  and execs the binary. A child that cannot apply them exits without doing anything
  (``OCR_ISOLATION_UNAVAILABLE``). Every child gets ``RLIMIT_NPROC = 0`` (no process, no thread;
  there is no way to relax it). The wall-clock deadline starts BEFORE the process is spawned, so
  startup counts against it; if ``Popen`` returns after it expired the child is killed at once. It
  and the stdout/stderr caps are enforced with ``selectors``. On any overrun, and again before
  returning, the child's ORIGINAL PROCESS GROUP is SIGKILLed (``CONTAINMENT_GUARANTEE``); with no
  descendants possible, that group is the child itself. Reaping after the kill is bounded too.
* Windows: there is no Job Object containment yet (``WINDOWS_NATIVE_VALIDATION = PENDING``), so the
  runner FAILS CLOSED: it spawns nothing and raises ``ISOLATION_UNAVAILABLE``
  (``OCR_ISOLATION_UNAVAILABLE``), and every adapter that needs it reports ``available() == False``.

PDF parsing runs in ``pados.packs.ocr._pdf_worker`` and operator engine bindings run in
``pados.packs.ocr._binding_worker``, both launched as ``sys.executable -I -B -m``; each returns bounded
canonical JSON that is validated here. Every failure is a closed code; no child output, stderr or
exception text is ever surfaced.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pados.capability.adapter import CapabilityAdapterError
from pados.packs.ocr import _rlimit_exec
from pados.packs.ocr._rlimit_exec import (
    EXIT_EXEC_FAILED,
    EXIT_EXECUTABLE_REFUSED,
    EXIT_LIMITS_REFUSED,
    Limits,
    encode_limits,
    launcher_argv,
)

if sys.platform != "win32":
    import selectors
    import signal

PDF_WORKER_MODULE = "pados.packs.ocr._pdf_worker"
BINDING_WORKER_MODULE = "pados.packs.ocr._binding_worker"
WORKER_EXIT_BUDGET = 3  # the worker caught MemoryError (kept in sync with _pdf_worker.EXIT_BUDGET)
# Content streams carry operators, positioning and graphics besides the text they show, so a page's
# decoded content stream may be up to this many times its decoded-text budget (and likewise in total).
CONTENT_DECODE_FACTOR = 8
REAP_SECONDS = 5.0  # after SIGKILL, how long the runner waits to reap before it returns anyway
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
_CHUNK = 64 * 1024
MIB = 1024 * 1024

OCR_SOURCE_TOO_LARGE = "OCR_SOURCE_TOO_LARGE"
OCR_PARSE_BUDGET_EXCEEDED = "OCR_PARSE_BUDGET_EXCEEDED"
OCR_PARSE_TIMEOUT = "OCR_PARSE_TIMEOUT"
OCR_PARSE_FAILED = "OCR_PARSE_FAILED"
OCR_DECODE_BUDGET_EXCEEDED = "OCR_DECODE_BUDGET_EXCEEDED"
OCR_ISOLATION_UNAVAILABLE = "OCR_ISOLATION_UNAVAILABLE"
CONTAINMENT_GUARANTEE = "ORIGINAL_PROCESS_GROUP"  # what the group kill reaches (RLIMIT_NPROC=0: the child)
# Closed codes a PDF worker may report; anything else becomes OCR_PARSE_FAILED.
WORKER_CODES = frozenset({"PDF_ENCRYPTED", "PDF_UNREADABLE", "PAGE_RANGE_OUT_OF_BOUNDS", "PAGE_COUNT_UNSUPPORTED",
                          "FIGURES_TOO_MANY", "MANIFEST_PAGE_COUNT_MISMATCH", OCR_PARSE_BUDGET_EXCEEDED,
                          OCR_DECODE_BUDGET_EXCEEDED})


@dataclass(frozen=True)
class ProcessBudget:
    """Hard limits for one child process (all bounded; validated on construction).

    Every child also gets ``RLIMIT_NPROC = 0`` (no process, no thread); that is not configurable. The
    text, content-stream and XObject budgets bound what the PDF worker decodes (ignored by other
    programs): pypdf's own decompression limits are set from ``max_page_content_bytes`` before any
    parsing, and each page's content streams are decoded under it before text is extracted.
    """

    wall_seconds: float = 60.0
    cpu_seconds: int = 30
    memory_bytes: int = 1024 * MIB
    max_input_bytes: int = MAX_SOURCE_BYTES
    max_stdout_bytes: int = 64 * MIB
    max_stderr_bytes: int = 64 * 1024
    max_open_files: int = 32
    max_page_text_bytes: int = 8 * MIB
    max_text_bytes: int = 48 * MIB
    max_xobjects_per_page: int = 1024
    max_xobjects: int = 16_384

    def __post_init__(self) -> None:
        if not (math.isfinite(self.wall_seconds) and 0.1 <= self.wall_seconds <= 600
                and 1 <= self.cpu_seconds <= 600 and 64 * MIB <= self.memory_bytes <= 64 * 1024 ** 3
                and 1 <= self.max_input_bytes <= 1024 ** 3 and 1 <= self.max_stdout_bytes <= 256 * MIB
                and 1 <= self.max_stderr_bytes <= 16 * MIB and 16 <= self.max_open_files <= 1024
                and 1024 <= self.max_page_text_bytes <= 256 * MIB and 1024 <= self.max_text_bytes <= 1024 ** 3
                and 1 <= self.max_xobjects_per_page <= 100_000 and 1 <= self.max_xobjects <= 1_000_000):
            raise ValueError("PROCESS_BUDGET_INVALID")

    @property
    def limits(self) -> Limits:
        return Limits(memory_bytes=self.memory_bytes, cpu_seconds=self.cpu_seconds,
                      max_open_files=self.max_open_files)

    @property
    def max_page_content_bytes(self) -> int:
        """Decoded content-stream bytes per page, and pypdf's per-stream decompression cap."""
        return self.max_page_text_bytes * CONTENT_DECODE_FACTOR

    @property
    def max_content_bytes(self) -> int:
        """Decoded content-stream bytes over all pages of one call."""
        return self.max_text_bytes * CONTENT_DECODE_FACTOR


PDF_PARSE_BUDGET = ProcessBudget()


class RunFailure(StrEnum):
    START_FAILED = "START_FAILED"
    TIMEOUT = "TIMEOUT"
    STDOUT_LIMIT = "STDOUT_LIMIT"
    STDERR_LIMIT = "STDERR_LIMIT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"  # the kernel enforced an rlimit (SIGXCPU / SIGKILL / SIGXFSZ)
    ISOLATION_UNAVAILABLE = "ISOLATION_UNAVAILABLE"  # no containment here (platform, privilege), or limits refused
    EXECUTABLE_INVALID = "EXECUTABLE_INVALID"  # the exec target failed validation (before or after the spawn)


class BoundedRunError(Exception):
    """A child run that exceeded a budget or could not start (``reason`` is closed; no child text)."""

    def __init__(self, reason: RunFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class BoundedRun:
    returncode: int
    stdout: bytes
    stderr_bytes: int  # stderr is counted and discarded, never surfaced


def isolation_refusal() -> str | None:
    """The explicit reason no child can be contained here, or ``None``.

    ``WINDOWS_NO_JOB_OBJECT``; ``PRIVILEGED_UID`` / ``PRIVILEGED_EUID`` (uid 0);
    ``CAP_SYS_RESOURCE`` / ``CAP_SYS_ADMIN`` (Linux capability sets, for which ``RLIMIT_NPROC`` is not
    enforced); ``CAPABILITIES_UNREADABLE`` (Linux, ``/proc/self/status`` unreadable). The probe is
    ``_rlimit_exec.privilege_refusal`` (its ``_identity`` and ``_proc_status`` are the test seams).
    """
    return _rlimit_exec.privilege_refusal()


def isolation_unavailable_reason() -> str | None:
    """``OCR_ISOLATION_UNAVAILABLE`` where no child can be contained (see ``isolation_refusal``)."""
    return OCR_ISOLATION_UNAVAILABLE if isolation_refusal() is not None else None


def minimal_environment() -> dict[str, str]:
    """No inherited variables (and so no credentials)."""
    return {}


def limits_token(budget: ProcessBudget) -> str:
    """The argv token a self-limiting child (the PDF worker) applies as its first action."""
    return encode_limits(budget.limits)


def run_bounded(argv: Sequence[str], *, stdin: bytes, env: Mapping[str, str], budget: ProcessBudget,
                self_limiting: bool = False) -> BoundedRun:
    """Run ``argv`` (never through a shell) under ``budget``; raise ``BoundedRunError`` on any overrun.

    ``argv[0]`` must be an absolute path. Unless ``self_limiting`` (the program is the PADOS
    interpreter and applies ``limits_token(budget)`` itself before anything else, as the PDF and
    binding workers do), the command is wrapped in the ``_rlimit_exec`` launcher, and ``argv[0]`` must
    pass ``executable_refusal``. Refused before anything is spawned: Windows, a privileged process,
    an invalid exec target.
    """
    if sys.platform == "win32":  # no Job Object containment yet: nothing is spawned
        raise BoundedRunError(RunFailure.ISOLATION_UNAVAILABLE)
    else:
        if not argv or not os.path.isabs(argv[0]) or len(stdin) > budget.max_input_bytes + MAX_HEADER_BYTES:
            raise BoundedRunError(RunFailure.START_FAILED)
        if isolation_refusal() is not None:  # RLIMIT_NPROC would not hold: nothing is spawned
            raise BoundedRunError(RunFailure.ISOLATION_UNAVAILABLE)
        if self_limiting and argv[0] != sys.executable:
            raise BoundedRunError(RunFailure.START_FAILED)
        if (_rlimit_exec.interpreter_refusal(sys.executable) is not None
                or (not self_limiting and _rlimit_exec.executable_refusal(argv[0]) is not None)):
            raise BoundedRunError(RunFailure.EXECUTABLE_INVALID)
        command = list(argv) if self_limiting else launcher_argv(budget.limits, argv)
        run = _run(command, stdin, dict(env), budget)
        if run.returncode == EXIT_LIMITS_REFUSED:
            raise BoundedRunError(RunFailure.ISOLATION_UNAVAILABLE)
        if not self_limiting and run.returncode == EXIT_EXECUTABLE_REFUSED:
            raise BoundedRunError(RunFailure.EXECUTABLE_INVALID)
        if run.returncode == EXIT_EXEC_FAILED and not self_limiting:
            raise BoundedRunError(RunFailure.START_FAILED)
        return run


_clock = time.monotonic  # the runner's clock (a test seam; never the global time module)

if sys.platform != "win32":
    _RESOURCE_SIGNALS = frozenset({-signal.SIGXCPU, -signal.SIGKILL, -signal.SIGXFSZ})

    def _start(argv: list[str], env: dict[str, str]) -> subprocess.Popen[bytes] | None:
        """Plain fork+exec (no ``preexec_fn``); limits are applied by the child itself."""
        try:
            return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    shell=False, env=env, close_fds=True, start_new_session=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def _kill_group(proc: subprocess.Popen[bytes]) -> None:
        """SIGKILL the child's original process group (the containment guarantee), then the child."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if proc.poll() is None:  # the group kill failed: at least the child itself must not survive
            proc.kill()

    def _drain(proc: subprocess.Popen[bytes], stdin: bytes, budget: ProcessBudget, deadline: float,
               stdout: bytearray) -> tuple[RunFailure | None, int]:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stderr_bytes, offset = 0, 0
        view = memoryview(stdin)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, "out")
            selector.register(proc.stderr, selectors.EVENT_READ, "err")
            if stdin:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE, "in")
            else:
                proc.stdin.close()
            while selector.get_map():
                remaining = deadline - _clock()
                if remaining <= 0:
                    return RunFailure.TIMEOUT, stderr_bytes
                for key, _ in selector.select(remaining):
                    if key.data == "in":
                        try:
                            offset += os.write(key.fd, view[offset:offset + _CHUNK])
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError):
                            offset = len(view)  # the child closed its stdin; it decides what that means
                        if offset >= len(view):
                            selector.unregister(key.fileobj)
                            proc.stdin.close()
                        continue
                    room = (budget.max_stdout_bytes - len(stdout) if key.data == "out"
                            else budget.max_stderr_bytes - stderr_bytes)
                    chunk = os.read(key.fd, min(_CHUNK, room + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "out":
                        stdout.extend(chunk)
                        if len(stdout) > budget.max_stdout_bytes:
                            return RunFailure.STDOUT_LIMIT, stderr_bytes
                    else:
                        stderr_bytes += len(chunk)
                        if stderr_bytes > budget.max_stderr_bytes:
                            return RunFailure.STDERR_LIMIT, stderr_bytes
        return None, stderr_bytes

    def _run(argv: list[str], stdin: bytes, env: dict[str, str], budget: ProcessBudget) -> BoundedRun:
        deadline = _clock() + budget.wall_seconds  # BEFORE the spawn: startup counts against it
        proc = _start(argv, env)
        if proc is None:
            raise BoundedRunError(RunFailure.START_FAILED)
        stdout = bytearray()
        failure: RunFailure | None = None
        stderr_bytes = 0
        try:
            if _clock() >= deadline:  # Popen itself outlived the budget: kill at once, write nothing
                failure = RunFailure.TIMEOUT
            else:
                failure, stderr_bytes = _drain(proc, stdin, budget, deadline, stdout)
            if failure is None:
                try:
                    proc.wait(timeout=max(0.0, deadline - _clock()))
                except subprocess.TimeoutExpired:
                    failure = RunFailure.TIMEOUT
        finally:
            _kill_group(proc)  # on overrun AND on success: nothing left in the original group survives
            try:  # bounded: a child stuck in uninterruptible kernel sleep is reaped later by subprocess
                proc.wait(timeout=REAP_SECONDS)
            except subprocess.TimeoutExpired:
                failure = failure or RunFailure.TIMEOUT
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        if failure is not None:
            raise BoundedRunError(failure)
        if proc.returncode in _RESOURCE_SIGNALS:
            raise BoundedRunError(RunFailure.RESOURCE_LIMIT)
        return BoundedRun(proc.returncode, bytes(stdout), stderr_bytes)


# --- isolated PDF parsing ----------------------------------------------------------------------

def pdf_worker_command(budget: ProcessBudget) -> list[str]:
    """``sys.executable -I -B -m <worker> <limits>``: isolated mode (no user site, no env, no cwd), no .pyc
    writes; the worker applies ``<limits>`` itself before it reads anything."""
    return [sys.executable, "-I", "-B", "-m", PDF_WORKER_MODULE, limits_token(budget)]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


_PARSE_CODES = {RunFailure.START_FAILED: OCR_PARSE_FAILED, RunFailure.TIMEOUT: OCR_PARSE_TIMEOUT,
                RunFailure.STDOUT_LIMIT: OCR_PARSE_BUDGET_EXCEEDED,
                RunFailure.STDERR_LIMIT: OCR_PARSE_BUDGET_EXCEEDED,
                RunFailure.RESOURCE_LIMIT: OCR_PARSE_BUDGET_EXCEEDED,
                RunFailure.ISOLATION_UNAVAILABLE: OCR_ISOLATION_UNAVAILABLE,
                RunFailure.EXECUTABLE_INVALID: OCR_ISOLATION_UNAVAILABLE}


def _decode_reply(stdout: bytes) -> dict[str, Any] | None:
    try:
        reply = json.loads(stdout.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(reply, dict) or canonical_json(reply) != stdout:
        return None
    return reply


def isolated_pdf(operation: str, payload: bytes, request: Mapping[str, Any], *,
                 budget: ProcessBudget = PDF_PARSE_BUDGET) -> dict[str, Any]:
    """Run one PDF operation in the worker; returns its validated ``result`` or raises a closed code."""
    unavailable = isolation_unavailable_reason()
    if unavailable is not None:
        raise CapabilityAdapterError(unavailable)
    if len(payload) > budget.max_input_bytes:
        raise CapabilityAdapterError(OCR_SOURCE_TOO_LARGE)
    header = canonical_json({**request, "op": operation, "max_page_text_bytes": budget.max_page_text_bytes,
                             "max_text_bytes": budget.max_text_bytes,
                             "max_stream_bytes": budget.max_page_content_bytes,
                             "max_content_bytes": budget.max_content_bytes,
                             "max_declared_stream_bytes": budget.max_input_bytes,
                             "max_xobjects_per_page": budget.max_xobjects_per_page,
                             "max_xobjects": budget.max_xobjects})
    if len(header) > MAX_HEADER_BYTES:
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    code: str | None = None
    run: BoundedRun | None = None
    try:
        run = run_bounded(pdf_worker_command(budget), stdin=header + b"\n" + payload, env=minimal_environment(),
                          budget=budget, self_limiting=True)
    except BoundedRunError as exc:
        code = _PARSE_CODES[exc.reason]
    if run is None or code is not None:
        raise CapabilityAdapterError(code or OCR_PARSE_FAILED, retryable=code == OCR_PARSE_TIMEOUT)
    if run.returncode != 0:
        raise CapabilityAdapterError(OCR_PARSE_BUDGET_EXCEEDED if run.returncode == WORKER_EXIT_BUDGET
                                     else OCR_PARSE_FAILED)
    reply = _decode_reply(run.stdout)
    if reply is None or set(reply) not in ({"ok", "result"}, {"ok", "code"}):
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    if reply["ok"] is not True:
        refused = reply.get("code")
        raise CapabilityAdapterError(refused if isinstance(refused, str) and refused in WORKER_CODES
                                     else OCR_PARSE_FAILED)
    result = reply.get("result")
    if not isinstance(result, dict):
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    return result


# --- isolated engine bindings (operator module:function, never in-process) -----------------------

BINDING_TARGET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,7}"
                            r":[A-Za-z_][A-Za-z0-9_]{0,63}")
_BINDING_RUN_CODES = {RunFailure.START_FAILED: "ENGINE_START_FAILED", RunFailure.TIMEOUT: "ENGINE_TIMEOUT",
                      RunFailure.STDOUT_LIMIT: "ENGINE_OUTPUT_TOO_LARGE",
                      RunFailure.STDERR_LIMIT: "ENGINE_OUTPUT_TOO_LARGE",
                      RunFailure.RESOURCE_LIMIT: "ENGINE_BUDGET_EXCEEDED",
                      RunFailure.ISOLATION_UNAVAILABLE: OCR_ISOLATION_UNAVAILABLE,
                      RunFailure.EXECUTABLE_INVALID: OCR_ISOLATION_UNAVAILABLE}


def binding_worker_command(target: str, budget: ProcessBudget) -> list[str]:
    """``sys.executable -I -B -m <binding worker> <limits> <module:function>``; the worker applies
    ``<limits>`` before it reads stdin or imports the operator module."""
    return [sys.executable, "-I", "-B", "-m", BINDING_WORKER_MODULE, limits_token(budget), target]


def isolated_binding(target: str, payload: bytes, request: Mapping[str, Any], *, env: Mapping[str, str],
                     budget: ProcessBudget, codes: frozenset[str]) -> dict[str, Any]:
    """Run an operator engine binding (``module:function``) in the binding worker under ``budget``.

    Returns the binding's validated JSON object (the caller validates it as ``ocr.engine-result/v1``)
    or raises a closed code: the run failures above, ``ENGINE_BUDGET_EXCEEDED`` (MemoryError in the
    child), ``ENGINE_OUTPUT_INVALID`` (no canonical object), one of ``codes`` reported by the binding,
    else ``ENGINE_FAILED``.
    """
    unavailable = isolation_unavailable_reason()
    if unavailable is not None:
        raise CapabilityAdapterError(unavailable)
    if not BINDING_TARGET.fullmatch(target):
        raise CapabilityAdapterError("ENGINE_FAILED")
    if len(payload) > budget.max_input_bytes:
        raise CapabilityAdapterError(OCR_SOURCE_TOO_LARGE)
    header = canonical_json(dict(request))
    if len(header) > MAX_HEADER_BYTES:
        raise CapabilityAdapterError("ENGINE_FAILED")
    code: str | None = None
    run: BoundedRun | None = None
    try:
        run = run_bounded(binding_worker_command(target, budget), stdin=header + b"\n" + payload, env=env,
                          budget=budget, self_limiting=True)
    except BoundedRunError as exc:
        code = _BINDING_RUN_CODES[exc.reason]
    if run is None or code is not None:
        raise CapabilityAdapterError(code or "ENGINE_FAILED", retryable=code == "ENGINE_TIMEOUT")
    if run.returncode != 0:
        raise CapabilityAdapterError("ENGINE_BUDGET_EXCEEDED" if run.returncode == WORKER_EXIT_BUDGET
                                     else "ENGINE_FAILED")
    reply = _decode_reply(run.stdout)
    if reply is None or set(reply) not in ({"ok", "result"}, {"ok", "code"}):
        raise CapabilityAdapterError("ENGINE_OUTPUT_INVALID")
    if reply["ok"] is not True:
        refused = reply.get("code")
        closed = refused if isinstance(refused, str) and refused in codes else "ENGINE_FAILED"
        raise CapabilityAdapterError(closed, retryable=closed == "ENGINE_TIMEOUT")
    result = reply.get("result")
    if not isinstance(result, dict):
        raise CapabilityAdapterError("ENGINE_OUTPUT_INVALID")
    return result


def as_int(value: Any, *, low: int = 0, high: int = 1_000_000) -> int:
    """A worker integer field (bool rejected) inside ``[low, high]``, else OCR_PARSE_FAILED."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    return int(value)


def as_dimension(value: Any) -> float | None:
    """A worker page dimension: ``None`` or a finite non-negative float."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    return float(value)


def as_list(value: Any, *, max_items: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > max_items:
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    return value


def as_row(value: Any, size: int) -> list[Any]:
    """A worker tuple encoded as a JSON list of exactly ``size`` items."""
    if not isinstance(value, list) or len(value) != size:
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    return value


def page_rows(value: Any, size: int, expected: Sequence[int]) -> list[tuple[int, list[Any]]]:
    """Per-page worker rows ``[page_number, ...]`` that must cover exactly ``expected``, in order."""
    rows = [as_row(row, size) for row in as_list(value, max_items=len(expected))]
    numbers = [as_int(row[0], low=1, high=100_000) for row in rows]
    if numbers != list(expected):
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    return [(number, row[1:]) for number, row in zip(numbers, rows, strict=True)]


__all__ = ["CONTAINMENT_GUARANTEE", "CONTENT_DECODE_FACTOR", "MAX_SOURCE_BYTES", "OCR_DECODE_BUDGET_EXCEEDED",
           "OCR_ISOLATION_UNAVAILABLE", "OCR_PARSE_BUDGET_EXCEEDED", "OCR_PARSE_FAILED", "OCR_PARSE_TIMEOUT",
           "OCR_SOURCE_TOO_LARGE", "PDF_PARSE_BUDGET", "BoundedRun", "BoundedRunError", "ProcessBudget",
           "RunFailure", "binding_worker_command", "isolated_binding", "isolated_pdf", "isolation_refusal",
           "isolation_unavailable_reason", "limits_token", "minimal_environment", "pdf_worker_command",
           "run_bounded"]
