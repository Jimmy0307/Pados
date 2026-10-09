"""Shared helpers for the OCR pack tests (hermetic: tmp stores, explicit grants, fixed clock, no network)."""
from __future__ import annotations

import importlib.util
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from pados.capability.adapter import CapabilityAdapter, StageOutput, StageRequest, bind_stage, run_bound_stage
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.policy import AuthorizedScope, PolicyContext, authorize_scope, evaluate
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.ocr.contracts import KIND_SOURCE, OcrTaskContract
from pados.packs.ocr.descriptors import OCR_CAPABILITIES
from pados.packs.ocr.engines import MinerUStyleAdapter, PaddleOcrAdapter, TesseractCliAdapter, VisionModelOcrAdapter
from pados.packs.ocr.fixture import FixtureOcrAdapter
from pados.packs.ocr.reference import (
    DocumentPreprocessAdapter,
    HeuristicStructureAdapter,
    LineLayoutAdapter,
    WhitespaceTableAdapter,
    XObjectFigureAdapter,
)
from pados.packs.ocr.text_layer import PdfTextLayerAdapter

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ocr"
ROOT_DOCS = Path(__file__).resolve().parents[3] / "docs" / "packs" / "ocr" / "OCR_CAPABILITY_PACK_V1.md"
PROJECT = "proj-ocr"
ALL_GRANTS = sorted(f"CAPABILITY:{capability_id}" for capability_id in OCR_CAPABILITIES)
CONTRACT = {"work_contract_id": "wc-ocr", "project_id": PROJECT, "required_capabilities": ALL_GRANTS}
GRANT = {"work_contract_id": "wc-ocr", "project_id": PROJECT, "capabilities": ALL_GRANTS}


def load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ocr_fixture_builder", FIXTURES / "build_fixtures.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def scope(capabilities: Sequence[str] | None = None) -> AuthorizedScope:
    if capabilities is None:
        return authorize_scope(CONTRACT, GRANT)
    return authorize_scope({**CONTRACT, "required_capabilities": list(capabilities)}, GRANT)


def put_source(store: ObjectStoreArtifacts, data: bytes, media_type: str) -> ArtifactRef:
    return store.put(data, media_type=media_type, kind=KIND_SOURCE)


def task(ref: ArtifactRef, **overrides: Any) -> OcrTaskContract:
    values: dict[str, Any] = {"artifact_ref": ref, "media_type": ref.media_type,
                              "classification": DataClassification.CONFIDENTIAL, "require_tables": True}
    values.update(overrides)
    return OcrTaskContract(**values)


def fixed_clock() -> Callable[[], datetime]:
    start = datetime(2026, 10, 3, tzinfo=UTC)

    def ticks() -> Iterator[datetime]:
        step = 0
        while True:
            yield start + timedelta(milliseconds=step)
            step += 5

    sequence = ticks()
    return lambda: next(sequence)


def fixture_adapter() -> FixtureOcrAdapter:
    return FixtureOcrAdapter.from_directory(FIXTURES)


def pdf_assignments(*, hybrid: bool = False) -> dict[str, CapabilityAdapter]:
    assignments: dict[str, CapabilityAdapter] = {
        "preprocess": DocumentPreprocessAdapter(), "recognize": PdfTextLayerAdapter(), "layout": LineLayoutAdapter(),
        "tables": WhitespaceTableAdapter(), "figures": XObjectFigureAdapter(), "structure": HeuristicStructureAdapter()}
    if hybrid:
        assignments["recognize_scanned"] = fixture_adapter()
    return assignments


def image_assignments(recognizer: CapabilityAdapter | None = None) -> dict[str, CapabilityAdapter]:
    return {"preprocess": DocumentPreprocessAdapter(), "recognize": recognizer or fixture_adapter(),
            "layout": LineLayoutAdapter(), "tables": WhitespaceTableAdapter(),
            "structure": HeuristicStructureAdapter()}


def every_adapter() -> list[CapabilityAdapter]:
    """Every adapter the pack ships, in their default (operator-unconfigured) state."""
    return [DocumentPreprocessAdapter(), PdfTextLayerAdapter(), fixture_adapter(), LineLayoutAdapter(),
            WhitespaceTableAdapter(), XObjectFigureAdapter(), HeuristicStructureAdapter(), TesseractCliAdapter(),
            PaddleOcrAdapter(), MinerUStyleAdapter(), VisionModelOcrAdapter(), VisionModelOcrAdapter(cloud=True)]


def permitted_context(adapter: CapabilityAdapter, *, stage_id: str = "stage") -> PolicyContext:
    """The most permissive context the shared policy allows for this adapter (everything but availability)."""
    descriptor = adapter.descriptor
    capability = OCR_CAPABILITIES[descriptor.capability_id]
    ceiling = min((capability.max_classification, descriptor.max_classification), key=lambda c: c.rank)
    network = max((capability.network_requirement, descriptor.network_requirement),
                  key=lambda n: [NetworkRequirement.LOCAL_ONLY, NetworkRequirement.PRIVATE_NETWORK,
                                 NetworkRequirement.NETWORK_OPTIONAL, NetworkRequirement.WAN_REQUIRED].index(n))
    return PolicyContext(scope=scope(), project_id=PROJECT, stage_id=stage_id, classification=ceiling,
                         network_policy=network)


def run_stage(adapter: CapabilityAdapter, store: ObjectStoreArtifacts, inputs: tuple[ArtifactRef, ...],
              ocr_task: OcrTaskContract, *, stage_id: str = "stage") -> StageOutput:
    """Execute one adapter strictly through bind_stage/run_bound_stage."""
    capability = OCR_CAPABILITIES[adapter.descriptor.capability_id]
    context = PolicyContext(scope=scope(), project_id=PROJECT, stage_id=stage_id,
                            classification=ocr_task.classification, network_policy=ocr_task.network_policy)
    decision = evaluate(capability, adapter.descriptor, context, adapter_available=adapter.available)
    request = StageRequest(stage_id, capability.capability_id, inputs, ocr_task.stage_parameters(),
                           ocr_task.classification)
    binding = bind_stage(capability, adapter.descriptor, context, decision, request,
                         permitted_inputs=frozenset(ref.sha256 for ref in inputs))
    return run_bound_stage(binding, adapter, store, revalidate=scope)
