# SPDX-License-Identifier: AGPL-3.0-only
"""OS-enforced isolation of the opt-in model worker (see docs/packs/document_intelligence/MODEL_WORKER_ISOLATION.md).

Each guarantee is checked on the platform that enforces it and skipped, with the reason, elsewhere:

* memory: an allocation past the kernel cap (Windows Job Object memory limit, Linux ``RLIMIT_AS``) is REFUSED
  by the kernel, far below the size at which the RSS watchdog would act, and the parent reports a closed code;
* containment: the whole worker tree is inside its job (Windows) or carries the address-space limit and the
  seccomp filter (Linux), confirmed by the parent from outside; a failure to set any of it fails closed;
* network: the pinned current guarantees (empty environment, offline variables, in-process refusal) plus what
  the OS adds (Linux: no socket at all, also for children; Windows: no further process). The Windows residual,
  a raw ``_socket`` connection, is pinned too so the design document cannot silently go stale.

Never call ``apply_worker_limits`` in the test process: it would confine pytest itself.
"""
from __future__ import annotations

import hashlib
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from pados.packs.document_intelligence import model_worker, worker_isolation
from pados.packs.document_intelligence.local_models import LocalModelError
from pados.packs.document_intelligence.model_pin import ModelFile, ModelPin, ModelRole
from pados.packs.document_intelligence.model_worker import SubprocessModelBackend
from pados.packs.document_intelligence.worker_isolation import (
    ISOLATION_FAILED,
    LINUX_ADDRESS_SPACE_HEADROOM_MB,
    MEMORY_REFUSED,
    MIB,
    WINDOWS_COMMIT_HEADROOM_MB,
    IsolationError,
    linux_isolation_observed,
    memory_cap_bytes,
    seccomp_program,
    supported,
)

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
needs_windows = pytest.mark.skipif(not WINDOWS, reason="Job Object containment exists only on Windows")
needs_linux = pytest.mark.skipif(not (LINUX and supported()),
                                 reason="RLIMIT_AS + seccomp containment needs Linux on x86_64 or aarch64")
needs_os_cap = pytest.mark.skipif(not supported(), reason="no OS-enforced isolation on this platform (fails closed)")
EXPECTED_ENVIRONMENT = {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS", "RAYON_NUM_THREADS", "PYTHONIOENCODING",
                        "PYTHONUTF8", *(() if WINDOWS else ("MALLOC_ARENA_MAX",))}
PASSED_THROUGH = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}
VENV_LAUNCHER = "__PYVENV_LAUNCHER__"  # added by the Windows venv launcher to its interpreter child
LOCALE_COERCION = "LC_CTYPE"  # set by the interpreter itself in a C locale (PEP 538), never inherited


def _pin(tmp_path: Path, **overrides: object) -> tuple[ModelPin, Path]:
    root = tmp_path / "operator-models"
    (root / "onnx").mkdir(parents=True, exist_ok=True)
    (root / "onnx" / "model.onnx").write_bytes(b"weights")
    values: dict[str, object] = dict(
        pin_id="isolation-test", role=ModelRole.EMBEDDING, engine="fastembed", model_name="test/model",
        source_repo="test/model", revision="e" * 40, license_spdx="MIT",
        files=(ModelFile(path="onnx/model.onnx", sha256=hashlib.sha256(b"weights").hexdigest(), size=7),),
        dimension=8, max_batch=8, max_input_chars=512, timeout_seconds=20.0, threads=1, max_ram_mb=128)
    values.update(overrides)
    return ModelPin(**values), root


@pytest.fixture
def listener() -> Iterator[int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    try:
        yield int(server.getsockname()[1])
    finally:
        server.close()


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


# -- pure: the cap and the filter program ---------------------------------------------------------------------
def test_the_cap_is_max_ram_plus_the_measured_named_headroom() -> None:
    assert memory_cap_bytes(1024, "win32") == (1024 + WINDOWS_COMMIT_HEADROOM_MB) * MIB
    assert memory_cap_bytes(1024, "linux") == (1024 + LINUX_ADDRESS_SPACE_HEADROOM_MB) * MIB


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_the_seccomp_program_checks_the_arch_first_and_allows_by_default(machine: str) -> None:
    rows = seccomp_program(machine)
    assert rows[0] == (0x20, 0, 0, 4) and rows[2] == (0x06, 0, 0, 0x80000000)  # foreign arch: kill
    assert rows[-1] == (0x06, 0, 0, 0x7FFF0000)
    socket_nr = 41 if machine == "x86_64" else 198
    index = rows.index((0x15, 0, 1, socket_nr))
    assert rows[index + 1] == (0x06, 0, 0, 0x00050000 | 13)  # socket(): EACCES, every family
    assert ((0x35, 0, 1, 0x40000000) in rows) is (machine == "x86_64")  # x32 system calls: kill


def test_an_unknown_arch_has_no_filter() -> None:
    with pytest.raises(IsolationError):
        seccomp_program("riscv64")


@pytest.mark.parametrize("setting", ["", "0", "-5", "1e9", "abc", "9" * 16])
def test_a_malformed_isolation_setting_is_refused_before_anything_applies(setting: str) -> None:
    assert model_worker._enter_isolation(setting) is False


# -- memory: the kernel refuses an allocation past the cap ----------------------------------------------------
@needs_os_cap
def test_the_kernel_refuses_an_allocation_past_the_cap_and_the_parent_reports_a_closed_code(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, max_ram_mb=128)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine", deadline=15.0)
    try:
        cap_mb = memory_cap_bytes(pin.max_ram_mb) // MIB
        pid = backend.worker_pid
        with pytest.raises(LocalModelError) as excinfo:
            backend.embed([f"__alloc_mb__:{cap_mb + 512}"], batch_size=1)
        # MEMORY_REFUSED comes from the worker after the kernel refused the allocation; the watchdog's code would
        # be MODEL_WORKER_MEMORY_LIMIT. The refused pages were never touched, so the watchdog never fired.
        assert excinfo.value.code == MEMORY_REFUSED
        assert pid is not None and _gone(pid)  # the worker exits after a refusal (its state is suspect)
        if backend.job is not None:
            limit, _, peak = backend.job.limits()
            assert limit == memory_cap_bytes(pin.max_ram_mb) and peak < limit
        assert len(backend.embed(["again"], batch_size=1)) == 1  # a fresh, again capped, worker
        assert backend.worker_pid != pid
    finally:
        backend.close()


@needs_os_cap
def test_an_allocation_under_the_cap_but_over_max_ram_is_still_the_watchdogs(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, max_ram_mb=128)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine", deadline=15.0)
    try:
        with pytest.raises(LocalModelError) as excinfo:
            backend.embed(["__alloc_mb__:400"], batch_size=1)  # 400 MiB < cap: committed, filled, then killed
        assert excinfo.value.code == "MODEL_WORKER_MEMORY_LIMIT"
    finally:
        backend.close()


# -- containment, confirmed from outside ----------------------------------------------------------------------
@needs_windows
def test_the_whole_worker_tree_runs_inside_a_sealed_job(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    try:
        job, pid = backend.job, backend.worker_pid
        assert job is not None and pid is not None
        tree = [pid, *(child.pid for child in psutil.Process(pid).children(recursive=True))]
        assert all(job.contains(member) for member in tree)
        limit, processes, _ = job.limits()
        assert limit == memory_cap_bytes(pin.max_ram_mb)
        workers = [member for member in tree if psutil.Process(member).name().lower() != "conhost.exe"]
        assert processes == len(workers) and job.active_processes() == len(tree)  # sealed: no further process
    finally:
        backend.close()
    assert all(_gone(member) for member in tree)


@needs_windows
def test_closing_the_job_handle_alone_ends_the_worker(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    try:
        job, pid = backend.job, backend.worker_pid
        assert job is not None and pid is not None
        tree = [pid, *(child.pid for child in psutil.Process(pid).children(recursive=True))]
        job.close()  # what happens if the parent dies: its handle closes, kill-on-close ends the tree
        psutil.wait_procs([psutil.Process(member) for member in tree if psutil.pid_exists(member)], timeout=10)
        assert all(_gone(member) for member in tree)
    finally:
        backend.close()


@needs_windows
def test_a_job_that_cannot_be_created_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_memory: int, _processes: int) -> None:
        raise IsolationError("JOB_CREATE_FAILED")

    monkeypatch.setattr(model_worker, "WindowsJob", refuse)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    pin, root = _pin(tmp_path)
    with pytest.raises(LocalModelError) as excinfo:
        SubprocessModelBackend(pin, root, engine="hashing-test-engine", scratch=scratch)
    assert excinfo.value.code == ISOLATION_FAILED
    assert list(scratch.iterdir()) == []  # the private copy is gone; no worker was started


@needs_windows
def test_a_worker_that_cannot_be_assigned_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []

    def refuse(_job: object, pid: int) -> None:
        started.append(pid)
        assert psutil.Process(pid).status() == psutil.STATUS_STOPPED  # still suspended: nothing of it ran
        raise IsolationError("JOB_ASSIGN_FAILED")

    monkeypatch.setattr(model_worker, "_enter_job", refuse)
    pin, root = _pin(tmp_path)
    with pytest.raises(LocalModelError) as excinfo:
        SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    assert excinfo.value.code == ISOLATION_FAILED
    assert started and _gone(started[0])


@needs_linux
def test_the_linux_worker_carries_the_address_space_limit_and_the_filter(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    try:
        assert backend.worker_pid is not None and backend.address_space is not None
        assert linux_isolation_observed(backend.worker_pid) == (backend.address_space, "2")
        status = Path(f"/proc/{backend.worker_pid}/status").read_text(encoding="ascii")
        assert "NoNewPrivs:\t1" in status
    finally:
        backend.close()


@needs_linux
def test_linux_limits_not_observed_from_outside_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_worker, "linux_isolation_observed", lambda _pid: (None, "0"))
    pin, root = _pin(tmp_path)
    with pytest.raises(LocalModelError) as excinfo:
        SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    assert excinfo.value.code == ISOLATION_FAILED


def test_an_unsupported_platform_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_worker, "supported", lambda: False)
    pin, root = _pin(tmp_path)
    with pytest.raises(LocalModelError) as excinfo:
        SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    assert excinfo.value.code == ISOLATION_FAILED


# -- network: pin the current guarantees ----------------------------------------------------------------------
@pytest.mark.parametrize("os_isolation", [True, False])
def test_the_worker_network_guarantees(tmp_path: Path, listener: int, monkeypatch: pytest.MonkeyPatch,
                                       os_isolation: bool) -> None:
    if os_isolation and not supported():
        pytest.skip("no OS-enforced isolation on this platform (fails closed)")
    for name, value in (("HTTPS_PROXY", "http://proxy.invalid:3128"), ("HF_TOKEN", "hf_not_a_token"),
                        ("PADOS_SECRET_PROBE", "x")):
        monkeypatch.setenv(name, value)
    pin, root = _pin(tmp_path)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine", os_isolation=os_isolation)
    try:
        seen = backend._call({"op": "probe", "port": listener, "texts": []})
    finally:
        backend.close()
    environment = set(seen["environment"])
    assert EXPECTED_ENVIRONMENT <= environment <= EXPECTED_ENVIRONMENT | PASSED_THROUGH | {VENV_LAUNCHER, LOCALE_COERCION}
    assert seen["offline"] == {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    assert seen["connect"] == seen["resolve"] == "REFUSED_IN_PROCESS"  # in-process refusal, every platform
    if os_isolation and LINUX:
        assert seen["raw_connect"] == "REFUSED_BY_OS:EACCES"  # seccomp: socket() itself fails, native code too
        assert seen["spawn"] == "CHILD_SOCKET_REFUSED"  # a child inherits the filter
    elif os_isolation and WINDOWS:
        assert seen["raw_connect"] == "ALLOWED"  # RESIDUAL: no OS network isolation on Windows (owner decision)
        assert seen["spawn"] == "REFUSED_BY_OS:WINERROR_1816"  # sealed job: ERROR_NOT_ENOUGH_QUOTA, no new process
    else:
        assert (seen["raw_connect"], seen["spawn"]) == ("ALLOWED", "CHILD_SOCKET_ALLOWED")  # the OS layer, by contrast


def test_a_production_engine_never_answers_a_probe(tmp_path: Path) -> None:
    source = Path(model_worker.__file__).read_text(encoding="utf-8")
    assert 'request["op"] == "probe" and engine == "hashing-test-engine"' in source
    assert model_worker.ENGINES[0] == "fastembed"
    assert worker_isolation.ISOLATION_FAILED in model_worker.__dict__.values()
