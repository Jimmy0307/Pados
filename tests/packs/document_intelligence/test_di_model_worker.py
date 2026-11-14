# SPDX-License-Identifier: AGPL-3.0-only
"""DI-1 / DI-2 hardening: a verified private model copy, and an opt-in killable model worker process.

The worker boundary is exercised with the dependency-free ``hashing-test-engine`` (CI has no model engine): the
engine sees only a private verified copy, a missed deadline kills the process (no runaway thread) and the next
call starts a fresh worker, a crash and a memory overrun are closed codes, and a source changed after the copy
cannot reach the loaded model. Live parity with the in-process engine runs only where the pinned models exist.
"""
from __future__ import annotations

import hashlib
import math
import os
import stat
from pathlib import Path

import psutil
import pytest
from di_support import live_models_present

from pados.packs.document_intelligence.local_models import (
    EngineIdentity,
    LocalModelError,
    PinnedCrossEncoder,
    PinnedTextEmbedder,
    default_model_root,
)
from pados.packs.document_intelligence.materialize import MaterializeError, discard, materialize_verified
from pados.packs.document_intelligence.model_pin import BUILTIN_PINS, ModelFile, ModelPin, ModelRole
from pados.packs.document_intelligence.model_worker import SubprocessModelBackend, subprocess_factory

IDENTITY = EngineIdentity(engine="hashing", engine_version="1.0", runtime="worker", runtime_version="1.0",
                          providers=("CPUExecutionProvider",), device="cpu", threads=1)


def _pin(tmp_path: Path, role: ModelRole = ModelRole.EMBEDDING, **overrides: object) -> tuple[ModelPin, Path]:
    root = tmp_path / "operator-models"
    (root / "onnx").mkdir(parents=True, exist_ok=True)
    (root / "onnx" / "model.onnx").write_bytes(b"weights")
    values: dict[str, object] = dict(
        pin_id="worker-test", role=role, engine="fastembed", model_name="test/model", source_repo="test/model",
        revision="d" * 40, license_spdx="MIT",
        files=(ModelFile(path="onnx/model.onnx", sha256=hashlib.sha256(b"weights").hexdigest(), size=7),),
        dimension=8 if role is ModelRole.EMBEDDING else None, max_batch=8, max_input_chars=512,
        timeout_seconds=20.0, threads=1, max_ram_mb=256)
    values.update(overrides)
    return ModelPin(**values), root


# -- DI-2: verified private copy ----------------------------------------------------------------------------------
def test_the_private_copy_holds_exactly_the_pinned_bytes_read_only(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    copy = materialize_verified(pin, root)
    try:
        assert copy != root and (copy / "onnx" / "model.onnx").read_bytes() == b"weights"
        assert not os.stat(copy / "onnx" / "model.onnx").st_mode & stat.S_IWUSR
        (root / "onnx" / "model.onnx").write_bytes(b"WEIGHTS")  # the operator file changes after the copy
        assert (copy / "onnx" / "model.onnx").read_bytes() == b"weights"
    finally:
        discard(copy)
    assert not copy.exists()


@pytest.mark.parametrize(("content", "code"), [(b"WEIGHTS", "MODEL_FILE_DIGEST_MISMATCH"),
                                               (b"weights!", "MODEL_FILE_SIZE_MISMATCH")])
def test_a_wrong_source_never_produces_a_copy(tmp_path: Path, content: bytes, code: str) -> None:
    pin, root = _pin(tmp_path)
    (root / "onnx" / "model.onnx").write_bytes(content)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(MaterializeError) as excinfo:
        materialize_verified(pin, root, parent=scratch)
    assert excinfo.value.code == code and list(scratch.iterdir()) == []  # nothing half-copied is left behind


def test_in_process_engines_can_load_from_the_private_copy(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    seen: list[Path] = []

    class Backend:
        def embed(self, documents: list[str], batch_size: int) -> list[list[float]]:
            return [[1.0] + [0.0] * 7 for _ in documents]

    def factory(_pin: ModelPin, directory: Path) -> Backend:
        seen.append(directory)
        return Backend()

    engine = PinnedTextEmbedder(pin, root, factory=factory, identity=IDENTITY, private_copy=True)
    engine.embed(["abc"])
    assert seen and seen[0] != root and seen[0].exists()
    engine.close()
    assert not seen[0].exists()


# -- DI-1: worker process -----------------------------------------------------------------------------------------
def _engine(tmp_path: Path, **overrides: object) -> tuple[PinnedTextEmbedder, ModelPin, Path]:
    pin, root = _pin(tmp_path, **overrides)
    engine = PinnedTextEmbedder(pin, root, factory=subprocess_factory("hashing-test-engine"), identity=IDENTITY)
    return engine, pin, root


def test_the_worker_embeds_through_the_unchanged_engine_contract(tmp_path: Path) -> None:
    engine, _, root = _engine(tmp_path)
    try:
        vectors = engine.embed(["alpha", "beta"]).vectors
        assert len(vectors) == 2 and all(math.isclose(sum(v * v for v in vec), 1.0, abs_tol=1e-5) for vec in vectors)
        backend = engine._backend
        assert isinstance(backend, SubprocessModelBackend) and backend.worker_pid != os.getpid()
        assert backend.model_dir is not None and backend.model_dir != root  # the worker sees only the private copy
    finally:
        engine.close()


def test_a_missed_deadline_kills_the_worker_and_the_next_call_recovers(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, timeout_seconds=3.0)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine", deadline=1.0)
    try:
        pid = backend.worker_pid
        with pytest.raises(LocalModelError) as excinfo:
            backend.embed(["__sleep__:30"], batch_size=1)
        assert excinfo.value.code == "LOCAL_MODEL_TIMEOUT"
        assert pid is not None and not psutil.pid_exists(pid) or psutil.Process(pid).status() == "zombie"
        assert len(backend.embed(["again"], batch_size=1)) == 1  # a fresh worker
        assert backend.worker_pid != pid
    finally:
        backend.close()


def test_a_crashing_worker_is_a_closed_code_then_restarts(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    try:
        with pytest.raises(LocalModelError) as excinfo:
            backend.embed(["__crash__"], batch_size=1)
        assert excinfo.value.code == "MODEL_WORKER_EXITED"
        assert len(backend.embed(["ok"], batch_size=1)) == 1
    finally:
        backend.close()


def test_the_memory_watchdog_kills_an_overrunning_worker(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, max_ram_mb=128)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine", deadline=15.0)
    try:
        with pytest.raises(LocalModelError) as excinfo:
            backend.embed(["__alloc_mb__:400"], batch_size=1)
        assert excinfo.value.code == "MODEL_WORKER_MEMORY_LIMIT"
    finally:
        backend.close()


def test_a_tampered_operator_directory_never_starts_a_worker(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    (root / "onnx" / "model.onnx").write_bytes(b"WEIGHTS")
    with pytest.raises(LocalModelError) as excinfo:
        SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    assert excinfo.value.code == "MODEL_FILE_DIGEST_MISMATCH"


def test_close_removes_the_process_and_the_private_copy(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    backend = SubprocessModelBackend(pin, root, engine="hashing-test-engine")
    pid, copy = backend.worker_pid, backend.model_dir
    backend.close()
    assert copy is not None and not copy.exists()
    assert pid is not None and (not psutil.pid_exists(pid) or psutil.Process(pid).status() == "zombie")


def test_an_unknown_engine_is_refused(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    with pytest.raises(LocalModelError) as excinfo:
        SubprocessModelBackend(pin, root, engine="python-eval")
    assert excinfo.value.code == "MODEL_WORKER_ENGINE_UNKNOWN"


@pytest.mark.skipif(not live_models_present(), reason="pinned models not fetched")
def test_live_worker_matches_the_in_process_engine() -> None:
    root = default_model_root()
    texts = ["which bearing failed", "The spindle bearing failed from wear.", "The invoice was paid in March."]
    embed_pin, cross_pin = BUILTIN_PINS["bge-small-en-v1.5-onnx-q"], BUILTIN_PINS["ms-marco-minilm-l6-v2-onnx"]
    local = PinnedTextEmbedder(embed_pin, root / embed_pin.pin_id, private_copy=True)
    remote = PinnedTextEmbedder(embed_pin, root / embed_pin.pin_id, factory=subprocess_factory())
    cross_local = PinnedCrossEncoder(cross_pin, root / cross_pin.pin_id)
    cross_remote = PinnedCrossEncoder(cross_pin, root / cross_pin.pin_id, factory=subprocess_factory())
    try:
        for a, b in zip(local.embed(texts).vectors, remote.embed(texts).vectors, strict=True):
            assert max(abs(x - y) for x, y in zip(a, b, strict=True)) < 1e-4
        for a, b in zip(cross_local.score(texts[0], texts[1:]), cross_remote.score(texts[0], texts[1:]), strict=True):
            assert abs(a - b) < 1e-3
    finally:
        for engine in (local, remote, cross_local, cross_remote):
            engine.close()
