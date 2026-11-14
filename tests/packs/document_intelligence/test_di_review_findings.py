# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests for the independent code and security review of 2026-11-14 (each failed before its fix).

Engine robustness: the deadline applies per batch, a load gets its own longer deadline, a poisoned engine recovers
once its runaway call has returned, worker threads never block interpreter exit, total input is bounded, and a
model file changed after verification is refused (verification is cached on a stat fingerprint, not forever).
Model directories refuse unpinned files. Stage contracts: evidence ids stay within the contract for the longest
block ids, the SHADOW order is the reranker's own top-k, ACTIVE ties keep the prior order, dense scores stay in
the cosine range, every raisable code is declared, and an extension cannot claim an adapter it did not register.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest
from di_support import corpus, engines, live_models_present

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts, ScopedArtifacts
from pados.enterprise.classification import DataClassification
from pados.packs.document_intelligence import deterministic_stages as det
from pados.packs.document_intelligence import local_models
from pados.packs.document_intelligence.evidence import build_evidence
from pados.packs.document_intelligence.local_models import (
    EngineIdentity,
    LocalModelError,
    PinnedCrossEncoder,
    PinnedTextEmbedder,
    default_model_root,
)
from pados.packs.document_intelligence.model_pin import BUILTIN_PINS, ModelFile, ModelPin, ModelRole, verify_model_dir
from pados.packs.document_intelligence.model_stages import CrossEncoderRerankAdapter, DenseRetrievalAdapter
from pados.packs.document_intelligence.ranking import ScoredRanking, load_scored
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, KIND_DOCUMENT, KIND_SOURCE, PDF_MEDIA_TYPE
from pados.packs.problem_solving.contracts import EIGHT_D
from pados.packs.retrieval.lexical import LexicalRetrievalAdapter, Passage

IDENTITY = EngineIdentity(engine="fake", engine_version="1.0", runtime="fake-rt", runtime_version="1.0",
                          providers=("CPUExecutionProvider",), device="cpu", threads=1)
QUERY = "why did the pump housings crack"


def _pin(tmp_path: Path, **overrides: object) -> tuple[ModelPin, Path]:
    root = tmp_path / "model"
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.onnx").write_bytes(b"weights")
    values: dict[str, object] = dict(
        pin_id="fake-model", role=ModelRole.EMBEDDING, engine="fastembed", model_name="fake/model",
        source_repo="fake/model", revision="b" * 40, license_spdx="Apache-2.0",
        files=(ModelFile(path="model.onnx", sha256=hashlib.sha256(b"weights").hexdigest(), size=7),),
        dimension=4, max_batch=2, max_input_chars=64, timeout_seconds=2.0, threads=1, max_ram_mb=256)
    values.update(overrides)
    return ModelPin(**values), root


class Fake:
    def __init__(self, *, delay: float = 0.0, block: threading.Event | None = None) -> None:
        self.delay, self.block = delay, block

    def embed(self, documents: list[str], batch_size: int) -> Iterable[Sequence[float]]:
        if self.block is not None:
            self.block.wait(10)
        time.sleep(self.delay * -(-len(documents) // batch_size))  # one delay per batch of this call
        return [[1.0, 0.0, 0.0, 0.0] for _ in documents]


def _embedder(pin: ModelPin, root: Path, backend: object) -> PinnedTextEmbedder:
    return PinnedTextEmbedder(pin, root, factory=lambda *_: backend, identity=IDENTITY)


# -- engine robustness -------------------------------------------------------------------------------------------
def test_the_deadline_applies_per_batch_not_to_the_whole_call(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, timeout_seconds=0.5, max_batch=1)
    embedder = _embedder(pin, root, Fake(delay=0.2))  # four 0.2 s batches: 0.8 s in total, each within 0.5 s
    assert len(embedder.embed(["a", "b", "c", "d"]).vectors) == 4
    assert embedder.status() is None


def test_a_slow_load_gets_its_own_longer_deadline(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, timeout_seconds=0.2)

    def slow_factory(_pin: ModelPin, _root: Path) -> Fake:
        time.sleep(0.4)
        return Fake()

    embedder = PinnedTextEmbedder(pin, root, factory=slow_factory, identity=IDENTITY)
    assert len(embedder.embed(["abc"]).vectors) == 1


def test_a_poisoned_engine_recovers_once_the_runaway_call_has_ended(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, timeout_seconds=0.2)
    release = threading.Event()
    backend = Fake(block=release)
    embedder = _embedder(pin, root, backend)
    with pytest.raises(LocalModelError):
        embedder.embed(["abc"])
    assert embedder.status() == "ENGINE_POISONED"
    backend.block = None
    release.set()
    deadline = time.monotonic() + 5
    while embedder.status() is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert embedder.status() is None  # the runaway returned: no computation is still running
    assert len(embedder.embed(["abc"]).vectors) == 1


def test_the_worker_thread_never_blocks_interpreter_exit(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, timeout_seconds=0.2)
    release = threading.Event()
    embedder = _embedder(pin, root, Fake(block=release))
    try:
        with pytest.raises(LocalModelError):
            embedder.embed(["abc"])
        runaway = [thread for thread in threading.enumerate() if thread.name.startswith("pados-model-")]
        assert runaway and all(thread.daemon for thread in runaway)
    finally:
        release.set()


def test_total_input_size_is_bounded(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path, max_input_chars=2000)
    with pytest.raises(LocalModelError) as excinfo:
        _embedder(pin, root, Fake()).embed(["x" * 2000] * 1000)
    assert excinfo.value.code == "LOCAL_MODEL_INPUT_TOO_LARGE"


def test_a_model_file_changed_after_verification_is_refused_and_unloaded(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    loads: list[int] = []

    def factory(_pin: ModelPin, _root: Path) -> Fake:
        loads.append(1)
        return Fake()

    embedder = PinnedTextEmbedder(pin, root, factory=factory, identity=IDENTITY)
    embedder.embed(["abc"])
    (root / "model.onnx").write_bytes(b"WEIGHTS")  # same size, different bytes
    os.utime(root / "model.onnx", ns=(1, 1))
    assert embedder.status() == "MODEL_FILE_DIGEST_MISMATCH"
    with pytest.raises(LocalModelError):
        embedder.embed(["abc"])
    assert loads == [1]


def test_verification_is_not_repeated_while_the_files_are_unchanged(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    pin, root = _pin(tmp_path)
    calls: list[int] = []
    original = local_models.verify_model_dir

    def counting(*args: Any) -> str | None:
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(local_models, "verify_model_dir", counting)
    embedder = _embedder(pin, root, Fake())
    for _ in range(5):
        assert embedder.status() is None
    assert len(calls) == 1


def test_an_unpinned_file_in_the_model_directory_is_refused(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    (root / "model.onnx_data").write_bytes(b"external weights")  # could be loaded beside the pinned model
    assert verify_model_dir(pin, root) == "MODEL_DIR_UNPINNED_FILE"


def test_the_fetch_tool_cache_directory_is_tolerated(tmp_path: Path) -> None:
    pin, root = _pin(tmp_path)
    (root / ".cache" / "huggingface").mkdir(parents=True)
    (root / ".cache" / "huggingface" / "model.onnx.metadata").write_text("x")
    assert verify_model_dir(pin, root) is None


@pytest.mark.skipif(not live_models_present(), reason="pinned models not fetched")
def test_live_engines_load_with_the_network_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    root = default_model_root()
    embedder = PinnedTextEmbedder(BUILTIN_PINS["bge-small-en-v1.5-onnx-q"], root / "bge-small-en-v1.5-onnx-q")
    encoder = PinnedCrossEncoder(BUILTIN_PINS["ms-marco-minilm-l6-v2-onnx"], root / "ms-marco-minilm-l6-v2-onnx")
    assert len(embedder.embed(["offline"]).vectors[0]) == 384
    assert len(encoder.score("offline", ["offline"])) == 1


# -- stage contracts ---------------------------------------------------------------------------------------------
class Bench:
    def __init__(self, tmp_path: Path) -> None:
        self.store = ObjectStoreArtifacts(tmp_path / "objects")
        self.embedder, self.encoder = engines(tmp_path / "models")

    def document(self) -> ArtifactRef:
        cases = (corpus.case("case.ncr-2041"),)
        source = self.store.put(corpus.binder_pdf(cases), media_type=PDF_MEDIA_TYPE, kind=KIND_SOURCE)
        built: Any = corpus.ocr_document(source, cases)
        return self.store.put(built.to_bytes(), media_type=JSON_MEDIA_TYPE, kind=KIND_DOCUMENT)

    def run(self, adapter: Any, capability_id: str, inputs: tuple[ArtifactRef, ...],
            parameters: dict[str, Any]) -> StageOutput:
        scoped = ScopedArtifacts(self.store, readable=inputs, max_outputs=8, max_output_bytes=8 * 1024 * 1024)
        request = StageRequest("stage", capability_id, inputs, parameters, DataClassification.CONFIDENTIAL)
        return adapter.execute(request, context=StageContext(artifacts=scoped, project_id="proj-a", stage_id="stage"))

    def lexical(self, document: ArtifactRef, query: str = QUERY) -> ArtifactRef:
        return self.run(LexicalRetrievalAdapter(), "PASSAGE_RETRIEVAL", (document,),
                        {"query": query, "top_k": 10}).outputs[0]

    def scored(self, output: StageOutput) -> ScoredRanking:
        return load_scored(bytes(self.store.get(output.outputs[0])))


def test_evidence_ids_stay_within_the_contract_for_the_longest_block_ids() -> None:
    long_id = "p1." + "b" * 61  # the longest block id the OCR contract allows (64 characters)
    units = [Passage(long_id, None, 1, "Root cause: worn bearing\nContainment: hold stock", 0, 47),
             Passage("p1." + "c" * 61, None, 1, "plain text", 49, 59)]
    pack, mapped = build_evidence(units, case_id="case.long", source_id="src.long", profile=EIGHT_D, max_items=8)
    assert len(pack.items) == 3 and all(len(item.evidence_id) <= 64 for item in pack.items)
    assert [entry.passage_id for entry in mapped] == [long_id, long_id, "p1." + "c" * 61]


def test_shadow_order_is_the_rerankers_own_top_k(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    document = bench.document()
    adapter = CrossEncoderRerankAdapter(bench.encoder)
    lexical = bench.lexical(document)
    parameters = {"query": QUERY, "top_k": 2}
    shadow = bench.scored(bench.run(adapter, "PASSAGE_RERANK_CROSS_ENCODER", (document, lexical),
                                    {**parameters, "mode": "SHADOW"}))
    active = bench.scored(bench.run(adapter, "PASSAGE_RERANK_CROSS_ENCODER", (document, lexical),
                                    {**parameters, "mode": "ACTIVE"}))
    assert list(shadow.shadow_order) == [item.passage_id for item in active.results]
    assert shadow.rerank_outcome == "SHADOW_SCORED" and active.rerank_outcome == "APPLIED"
    assert len(shadow.results) == 2


def test_active_ties_keep_the_prior_order(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    document = bench.document()
    flat = type("Flat", (), {"rerank": lambda self, query, documents, batch_size: [1.0] * len(documents)})()
    pin = bench.encoder.pin
    tied = PinnedCrossEncoder(pin, bench.encoder.directory, factory=lambda *_: flat, identity=bench.encoder.identity)
    ranking = bench.scored(bench.run(CrossEncoderRerankAdapter(tied), "PASSAGE_RERANK_CROSS_ENCODER",
                                     (document, bench.lexical(document)), {"query": QUERY, "mode": "ACTIVE"}))
    assert [item.prior_rank for item in ranking.results] == sorted(item.prior_rank or 0 for item in ranking.results)


def test_a_dense_cosine_outside_the_unit_range_is_not_schema_valid(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    output = bench.run(DenseRetrievalAdapter(bench.embedder), "PASSAGE_RETRIEVAL_DENSE", (bench.document(),),
                       {"query": QUERY})
    payload = json.loads(bytes(bench.store.get(output.outputs[0])))
    payload["results"][0]["score"] = 1.5
    with pytest.raises(ValueError):
        ScoredRanking.model_validate(payload)


def test_every_code_a_stage_can_raise_is_declared(tmp_path: Path) -> None:
    from pados.packs.document_intelligence.boundary import closed
    from pados.packs.retrieval.lexical import RetrievalError

    def raises(_request: Any, _context: Any) -> Any:
        raise RetrievalError("RETRIEVAL_TOO_MANY_PASSAGES")

    with pytest.raises(CapabilityAdapterError) as excinfo:
        closed(raises, None, None)  # type: ignore[arg-type]
    assert excinfo.value.code == "DI_TOO_MANY_PASSAGES"
    assert excinfo.value.code in {f.code for f in det.MANIFEST.failure_codes}
    embedder, _ = engines(tmp_path)
    assert "LOCAL_MODEL_INPUT_TOO_LARGE" in {f.code for f in DenseRetrievalAdapter(embedder).manifest.failure_codes}


def test_an_extension_that_returns_an_unregistered_adapter_is_refused() -> None:
    from pados.packs.intelligence.provenance import AtomicInputProvenance
    from pados.packs.intelligence.wiring import WiringError, wire_pipeline

    def lying(registry: Any, templates: Any, schemas: Any, references: Any) -> dict[str, Any]:
        return {"never.registered": det.FusionAdapter()}

    with pytest.raises(WiringError) as excinfo:
        wire_pipeline(AtomicInputProvenance(), operator="operator.test", include_model_explanation=False,
                      extensions=(lying,))
    assert excinfo.value.code == "PIPELINE_EXTENSION_ADAPTER_NOT_REGISTERED"
