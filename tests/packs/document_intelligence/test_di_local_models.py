# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned local engines: bounded, verified, fail-closed.

The engine loads only after the operator directory verifies against the pin; inputs are bounded by the pin;
outputs are validated (count, dimension, finite) and quantized; a call past the pin's deadline fails closed and
poisons the engine; the enterprise ``Reranker`` view never lets a failure escape as anything but a RerankError.
The live test runs only when the pinned models are present (``PADOS_MODEL_ROOT``).
"""
from __future__ import annotations

import hashlib
import math
import os
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from pados.enterprise.manifest import CapabilityHealth
from pados.enterprise.rerank import RerankCandidate, RerankError, RerankMode, rerank
from pados.packs.document_intelligence.local_models import (
    CrossEncoderReranker,
    EngineIdentity,
    LocalModelError,
    PinnedCrossEncoder,
    PinnedTextEmbedder,
    default_model_root,
)
from pados.packs.document_intelligence.model_pin import BUILTIN_PINS, ModelFile, ModelPin, ModelRole

IDENTITY = EngineIdentity(engine="fake", engine_version="1.0", runtime="fake-rt", runtime_version="1.0",
                          providers=("CPUExecutionProvider",), device="cpu", threads=1)


def _pin(tmp_path: Path, role: ModelRole = ModelRole.EMBEDDING, **overrides: object) -> tuple[ModelPin, Path]:
    root = tmp_path / role.value.lower()
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.onnx").write_bytes(b"weights")
    values: dict[str, object] = dict(
        pin_id="fake-model", role=role, engine="fastembed", model_name="fake/model", source_repo="fake/model",
        revision="b" * 40, license_spdx="Apache-2.0",
        files=(ModelFile(path="model.onnx", sha256=hashlib.sha256(b"weights").hexdigest(), size=7),),
        dimension=4 if role is ModelRole.EMBEDDING else None, max_batch=2, max_input_chars=64,
        timeout_seconds=2.0, threads=1, max_ram_mb=256)
    values.update(overrides)
    return ModelPin(**values), root


class FakeEmbedding:
    def __init__(self, vector: Sequence[float] | None = None, *, block: threading.Event | None = None) -> None:
        self.vector = vector
        self.block = block
        self.batches: list[int] = []
        self.seen: list[str] = []

    def embed(self, documents: list[str], batch_size: int) -> Iterable[Sequence[float]]:
        if self.block is not None:
            self.block.wait(10)
        self.batches.append(batch_size)
        for text in documents:
            self.seen.append(text)
            yield self.vector if self.vector is not None else [float(len(text)), 1.0, 0.0, 0.0]


class FakeCross:
    def __init__(self, scores: Sequence[float] | None = None) -> None:
        self.scores = scores

    def rerank(self, query: str, documents: list[str], batch_size: int) -> Iterable[float]:
        if self.scores is not None:
            return list(self.scores)
        return [float(sum(1 for word in query.split() if word in document)) for document in documents]


def _embedder(pin: ModelPin, root: Path, backend: object) -> PinnedTextEmbedder:
    return PinnedTextEmbedder(pin, root, factory=lambda _pin, _root: backend, identity=IDENTITY)


def test_embeddings_are_unit_length_quantized_and_bounded_by_the_pin(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    backend = FakeEmbedding()
    embedder = _embedder(pin, root, backend)
    assert embedder.status() is None
    result = embedder.embed(["abc", "x" * 100])
    assert backend.batches == [2] and backend.seen[1] == "x" * 64  # max_batch and max_input_chars applied
    assert result.truncated == 1 and len(result.vectors) == 2
    for vector in result.vectors:
        assert len(vector) == 4 and math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-5)
        assert all(round(v, 6) == v for v in vector)


@pytest.mark.parametrize(("vector", "code"), [([1.0, 2.0], "LOCAL_MODEL_DIMENSION_MISMATCH"),
                                               ([1.0, float("nan"), 0.0, 0.0], "LOCAL_MODEL_OUTPUT_INVALID"),
                                               ([0.0, 0.0, 0.0, 0.0], "LOCAL_MODEL_OUTPUT_INVALID")])
def test_malformed_engine_output_fails_closed(tmp_path: Path, vector: list[float], code: str) -> None:
    pin, root = _pin(tmp_path)
    with pytest.raises(LocalModelError) as excinfo:
        _embedder(pin, root, FakeEmbedding(vector)).embed(["abc"])
    assert excinfo.value.code == code


def test_a_tampered_model_directory_never_loads(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    (root / "model.onnx").write_bytes(b"WEIGHTS")
    loads: list[int] = []
    embedder = PinnedTextEmbedder(pin, root, factory=lambda *_: loads.append(1), identity=IDENTITY)
    assert embedder.status() == "MODEL_FILE_DIGEST_MISMATCH"
    with pytest.raises(LocalModelError) as excinfo:
        embedder.embed(["abc"])
    assert excinfo.value.code == "MODEL_FILE_DIGEST_MISMATCH" and loads == []


def test_the_engine_not_installed_is_a_status_not_an_error(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    embedder = PinnedTextEmbedder(pin, root, factory=lambda *_: FakeEmbedding(), identity=None)
    assert embedder.status() == "ENGINE_NOT_INSTALLED"


def test_a_role_mismatch_is_refused(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, ModelRole.CROSS_ENCODER)
    assert _embedder(pin, root, FakeEmbedding()).status() == "MODEL_PIN_ROLE_MISMATCH"


@pytest.mark.parametrize("texts", [[], ["ok", 3], ["a"] * 5000])
def test_inputs_outside_the_bounds_are_refused(tmp_path: Path, texts: list[object]) -> None:
    pin, root = _pin(tmp_path)
    with pytest.raises(LocalModelError) as excinfo:
        _embedder(pin, root, FakeEmbedding()).embed(texts)  # type: ignore[arg-type]
    assert excinfo.value.code == "LOCAL_MODEL_INPUT_INVALID"


def test_a_call_past_the_deadline_fails_closed_and_poisons_the_engine(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, timeout_seconds=0.2)
    release = threading.Event()
    embedder = _embedder(pin, root, FakeEmbedding(block=release))
    try:
        with pytest.raises(LocalModelError) as excinfo:
            embedder.embed(["abc"])
        assert excinfo.value.code == "LOCAL_MODEL_TIMEOUT"
        assert embedder.status() == "ENGINE_POISONED"
        with pytest.raises(LocalModelError):
            embedder.embed(["abc"])
    finally:
        release.set()


def test_an_engine_exception_never_echoes_its_message(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)

    class Exploding:
        def embed(self, documents: list[str], batch_size: int) -> Iterable[Sequence[float]]:
            raise RuntimeError("secret document text")

    with pytest.raises(LocalModelError) as excinfo:
        _embedder(pin, root, Exploding()).embed(["abc"])
    assert excinfo.value.code == "LOCAL_MODEL_FAILED" and "secret" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None


def test_cross_encoder_scores_are_validated(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, ModelRole.CROSS_ENCODER)
    encoder = PinnedCrossEncoder(pin, root, factory=lambda *_: FakeCross(), identity=IDENTITY)
    assert encoder.score("bearing wear", ["bearing wear seen", "paint"]) == (2.0, 0.0)
    for bad in ([1.0], [float("inf"), 0.0]):
        broken = PinnedCrossEncoder(pin, root, factory=lambda *_, s=bad: FakeCross(s), identity=IDENTITY)
        with pytest.raises(LocalModelError) as excinfo:
            broken.score("q", ["a", "b"])
        assert excinfo.value.code == "LOCAL_MODEL_OUTPUT_INVALID"


def test_the_enterprise_reranker_view_obeys_shadow_and_active_modes(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, ModelRole.CROSS_ENCODER)
    reranker = CrossEncoderReranker(PinnedCrossEncoder(pin, root, factory=lambda *_: FakeCross(), identity=IDENTITY))
    assert reranker.health() is CapabilityHealth.READY and reranker.version == pin.revision
    candidates = [RerankCandidate("c1", "paint", 0.9), RerankCandidate("c2", "bearing wear", 0.5)]
    shadow, report = rerank(reranker, RerankMode.SHADOW, "bearing wear", candidates)
    assert [cid for cid, _ in shadow] == ["c1", "c2"] and report["shadow_ranking"] == ["c2", "c1"]
    active, report = rerank(reranker, RerankMode.ACTIVE, "bearing wear", candidates)
    assert [cid for cid, _ in active] == ["c2", "c1"] and report["applied"] is True

    broken = CrossEncoderReranker(PinnedCrossEncoder(pin, root, factory=lambda *_: FakeCross([1.0]),
                                                     identity=IDENTITY))
    with pytest.raises(RerankError):
        rerank(broken, RerankMode.ACTIVE, "bearing wear", candidates)  # ACTIVE fails closed
    kept, report = rerank(broken, RerankMode.SHADOW, "bearing wear", candidates)
    assert [cid for cid, _ in kept] == ["c1", "c2"] and report["error"] == "LOCAL_MODEL_OUTPUT_INVALID"


def test_an_unavailable_cross_encoder_reports_unavailable_health(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, ModelRole.CROSS_ENCODER)
    reranker = CrossEncoderReranker(PinnedCrossEncoder(pin, root, factory=lambda *_: FakeCross(), identity=None))
    assert reranker.health() is CapabilityHealth.UNAVAILABLE


# -- live: the real pinned models (skipped unless an operator fetched them) -----------------------------------
_ROOT = default_model_root()
_LIVE = all((_ROOT / pin_id).is_dir() for pin_id in BUILTIN_PINS) and os.environ.get("PADOS_SKIP_LIVE_MODELS") != "1"


@pytest.mark.skipif(not _LIVE, reason="pinned models not fetched (scripts/models/fetch_pinned_models.py)")
def test_live_pinned_models_embed_and_rerank() -> None:
    embedder = PinnedTextEmbedder(BUILTIN_PINS["bge-small-en-v1.5-onnx-q"], _ROOT / "bge-small-en-v1.5-onnx-q")
    assert embedder.status() is None
    query, relevant, other = ("which bearing failed", "The spindle bearing failed from wear.",
                              "The invoice was paid in March.")
    vectors = embedder.embed([query, relevant, other]).vectors
    assert all(len(vector) == 384 for vector in vectors)
    cosine = [sum(a * b for a, b in zip(vectors[0], vector, strict=True)) for vector in vectors[1:]]
    assert cosine[0] > cosine[1]
    assert embedder.embed([query]).vectors == embedder.embed([query]).vectors  # same process: identical bytes
    encoder = PinnedCrossEncoder(BUILTIN_PINS["ms-marco-minilm-l6-v2-onnx"], _ROOT / "ms-marco-minilm-l6-v2-onnx")
    scores = encoder.score(query, [relevant, other])
    assert scores[0] > scores[1]
    assert encoder.identity is not None and encoder.identity.engine == "fastembed"
