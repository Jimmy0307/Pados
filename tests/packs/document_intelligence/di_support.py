# SPDX-License-Identifier: AGPL-3.0-only
"""Workbench for the governed Document Intelligence tests: the EXISTING fabric planner and executor over the
pipeline wiring plus the Document Intelligence and Problem Solving extensions.

Engines are deterministic stand-ins by default (a hashed bag-of-words embedder and a term-overlap cross-encoder
behind real model pins over a temporary model directory), so CI needs no model download; ``live=True`` uses the
operator's pinned models when present.
"""
from __future__ import annotations

import hashlib
import itertools
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "pipeline"))
sys.path.insert(0, str(_HERE.parents[2] / "fixtures" / "document_intelligence"))

import corpus  # noqa: E402
from pipeline_support import OPERATOR, FixedClock, GrantStore, inventory  # noqa: E402

from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts  # noqa: E402
from pados.capability.executor import CapabilityExecutor, ExecutionResult  # noqa: E402
from pados.capability.planner import CapabilityPlanner, ExecutionPlan  # noqa: E402
from pados.capability.task import TaskContract, TaskInput  # noqa: E402
from pados.connectivity.requirements import NetworkRequirement  # noqa: E402
from pados.corpus.ingestion import deterministic_embedding  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.packs.document_intelligence.local_models import (  # noqa: E402
    EngineIdentity,
    PinnedCrossEncoder,
    PinnedTextEmbedder,
    default_model_root,
)
from pados.packs.document_intelligence.model_pin import BUILTIN_PINS, ModelFile, ModelPin, ModelRole  # noqa: E402
from pados.packs.document_intelligence.ranking import SCORED_CONTRACT  # noqa: E402
from pados.packs.document_intelligence.wiring import (  # noqa: E402
    DOCUMENT_INPUT,
    EVIDENCE_INPUT,
    SOLVE_TASK_TYPE,
    all_evidence_templates,
    document_intelligence_extension,
    problem_solving_extension,
    solve_template,
    stage_grants,
)
from pados.packs.document_intelligence.deterministic_stages import EVIDENCE_CONTRACT  # noqa: E402
from pados.packs.document_intelligence.workflow import ExtractiveClaimDrafter  # noqa: E402
from pados.packs.intelligence.provenance import AtomicInputProvenance  # noqa: E402
from pados.packs.intelligence.wiring import PipelineWiring, wire_pipeline  # noqa: E402
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, KIND_DOCUMENT, KIND_SOURCE, PDF_MEDIA_TYPE  # noqa: E402
from pados.packs.problem_solving import pack as ps  # noqa: E402

PROJECT = "proj-a"
WORK = "wc-1"
FAKE_IDENTITY = EngineIdentity(engine="fake", engine_version="1.0.0", runtime="fake-runtime", runtime_version="1.0.0",
                               providers=("CPUExecutionProvider",), device="cpu", threads=1)


class HashedEmbedding:
    """Deterministic stand-in: the PADOS hashed bag-of-words embedding (32 dimensions)."""

    def embed(self, documents: list[str], batch_size: int) -> Iterable[Sequence[float]]:
        return [deterministic_embedding(text) or [1.0] + [0.0] * 31 for text in documents]


class OverlapCross:
    """Deterministic stand-in cross-encoder: query term overlap (optionally failing)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def rerank(self, query: str, documents: list[str], batch_size: int) -> Iterable[float]:
        if self.fail:
            raise RuntimeError("engine exploded")
        terms = set(query.lower().split())
        return [float(len(terms & set(document.lower().split()))) for document in documents]


def fake_pin(root: Path, role: ModelRole) -> tuple[ModelPin, Path]:
    directory = root / f"fake-{role.value.lower()}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.onnx").write_bytes(b"fake weights")
    pin = ModelPin(pin_id=f"fake-{role.value.lower().replace('_', '-')}", role=role, engine="fastembed",
                   model_name="fake/model", source_repo="fake/model", revision="c" * 40, license_spdx="Apache-2.0",
                   files=(ModelFile(path="model.onnx", sha256=hashlib.sha256(b"fake weights").hexdigest(), size=12),),
                   dimension=32 if role is ModelRole.EMBEDDING else None, max_batch=16, max_input_chars=2000,
                   timeout_seconds=30.0, threads=1, max_ram_mb=256)
    return pin, directory


def engines(root: Path, *, live: bool = False, cross_fails: bool = False,
            installed: bool = True) -> tuple[PinnedTextEmbedder, PinnedCrossEncoder]:
    if live:
        models = default_model_root()
        embed_pin, cross_pin = BUILTIN_PINS["bge-small-en-v1.5-onnx-q"], BUILTIN_PINS["ms-marco-minilm-l6-v2-onnx"]
        return (PinnedTextEmbedder(embed_pin, models / embed_pin.pin_id),
                PinnedCrossEncoder(cross_pin, models / cross_pin.pin_id))
    identity = FAKE_IDENTITY if installed else None
    embed_pin, embed_dir = fake_pin(root, ModelRole.EMBEDDING)
    cross_pin, cross_dir = fake_pin(root, ModelRole.CROSS_ENCODER)
    return (PinnedTextEmbedder(embed_pin, embed_dir, factory=lambda *_: HashedEmbedding(), identity=identity),
            PinnedCrossEncoder(cross_pin, cross_dir, factory=lambda *_: OverlapCross(fail=cross_fails),
                               identity=identity))


def live_models_present() -> bool:
    root = default_model_root()
    return all((root / pin_id).is_dir() for pin_id in BUILTIN_PINS)


@dataclass
class Workbench:
    store: ObjectStoreArtifacts
    provenance: AtomicInputProvenance
    wiring: PipelineWiring
    grants: GrantStore
    embedder: PinnedTextEmbedder
    encoder: PinnedCrossEncoder
    run_ids: Any
    last: Any = None  # (task, plan, result) of the latest evidence run
    clock: Any = None  # the executor clock (a fixed test clock unless a caller measures wall time)

    def put_document(self, cases: tuple[corpus.Case, ...], *, project_id: str = PROJECT,
                     classification: DataClassification = DataClassification.CONFIDENTIAL) -> ArtifactRef:
        source = self.store.put(corpus.binder_pdf(cases), media_type=PDF_MEDIA_TYPE, kind=KIND_SOURCE)
        document = corpus.ocr_document(source, cases)
        ref = self.store.put(document.to_bytes(), media_type=JSON_MEDIA_TYPE, kind=KIND_DOCUMENT)  # type: ignore[attr-defined]
        self.provenance.register(ref.sha256, project_id=project_id, classification=classification)
        return ref

    def put_pdf(self, cases: tuple[corpus.Case, ...], *, project_id: str = PROJECT) -> ArtifactRef:
        source = self.store.put(corpus.binder_pdf(cases), media_type=PDF_MEDIA_TYPE, kind=KIND_SOURCE)
        self.provenance.register(source.sha256, project_id=project_id, classification=DataClassification.CONFIDENTIAL)
        return source

    def evidence_task(self, document: ArtifactRef, *, task_type: str, query: str, case_id: str,
                      source_id: str = "src.ncr-binder", profile: str = "8d", project_id: str = PROJECT,
                      task_id: str = "evidence-1") -> TaskContract:
        return TaskContract(
            task_id=task_id, task_type=task_type, project_id=project_id, work_contract_id=WORK,
            classification=DataClassification.CONFIDENTIAL, network_policy=NetworkRequirement.LOCAL_ONLY,
            required_outputs=(EVIDENCE_CONTRACT,),
            parameters={"query": query, "case_id": case_id, "source_id": source_id, "profile": profile},
            inputs=(TaskInput(name=DOCUMENT_INPUT, ref=document, project_id=project_id,
                              classification=DataClassification.CONFIDENTIAL),))

    def solve_task(self, evidence: ArtifactRef, *, pinned_digest: str, authorized_sources: str,
                   profile: str = "8d", project_id: str = PROJECT, task_id: str = "solve-1") -> TaskContract:
        return TaskContract(
            task_id=task_id, task_type=SOLVE_TASK_TYPE, project_id=project_id, work_contract_id=WORK,
            classification=DataClassification.CONFIDENTIAL, network_policy=NetworkRequirement.LOCAL_ONLY,
            required_outputs=(ps.OUTPUT_CONTRACT,),
            parameters={"profile": profile, "evidence_pack_digest": pinned_digest,
                        "authorized_sources": authorized_sources},
            inputs=(TaskInput(name=EVIDENCE_INPUT, ref=evidence, project_id=project_id,
                              classification=DataClassification.CONFIDENTIAL),))

    def planner(self) -> CapabilityPlanner:
        return CapabilityPlanner(self.wiring.registry, self.wiring.templates, self.grants,
                                 config=self.wiring.planner_config, inventory=inventory(),
                                 provenance=self.provenance)

    def executor(self) -> CapabilityExecutor:
        return CapabilityExecutor(self.wiring.registry, self.store, self.grants, templates=self.wiring.templates,
                                  verifier=self.wiring.verifier, clock=self.clock or FixedClock(),
                                  run_ids=self.run_ids,
                                  planner_config=self.wiring.planner_config, inventory=inventory(),
                                  provenance=self.provenance)

    def run(self, task: TaskContract) -> tuple[ExecutionPlan, ExecutionResult | None]:
        plan = self.planner().plan(task)
        if plan.status.value != "FEASIBLE":
            return plan, None
        with self.wiring.invocations.bind(task):
            return plan, self.executor().execute(plan, task)

    def outputs(self, result: ExecutionResult, stage_id: str) -> dict[str, bytes]:
        stage = next(stage for stage in result.plan_receipt.stages if stage.stage_id == stage_id)
        return {ref.kind: bytes(self.store.get(ref)) for ref in stage.outputs}

    def output_ref(self, result: ExecutionResult, stage_id: str, kind: str) -> ArtifactRef:
        stage = next(stage for stage in result.plan_receipt.stages if stage.stage_id == stage_id)
        return next(ref for ref in stage.outputs if ref.kind == kind)


def workbench(tmp_path: Path, *, live: bool = False, cross_fails: bool = False, installed: bool = True,
              provider: ps.ClaimProvider | None = None) -> Workbench:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    provenance = AtomicInputProvenance()
    embedder, encoder = engines(tmp_path / "models", live=live, cross_fails=cross_fails, installed=installed)
    wiring = wire_pipeline(provenance, operator=OPERATOR, clock=FixedClock(), include_model_explanation=False,
                           extensions=(document_intelligence_extension(embedder, encoder, operator=OPERATOR),
                                       problem_solving_extension(provider or ExtractiveClaimDrafter(),
                                                                 operator=OPERATOR)))
    grants = GrantStore()
    grants.grant(*stage_grants(*all_evidence_templates(), solve_template()), project_id=PROJECT, work=WORK)
    counter = itertools.count(1)
    return Workbench(store, provenance, wiring, grants, embedder, encoder, lambda: f"run{next(counter):05d}")


__all__ = ["corpus", "SCORED_CONTRACT", "Workbench", "workbench", "live_models_present", "PROJECT"]
