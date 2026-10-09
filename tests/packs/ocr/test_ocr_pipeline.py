"""OCR pipeline: templates as data, the local runner, policy refusals, receipts, determinism, lineage."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from ocr_support import (
    PROJECT,
    fixed_clock,
    fixture_adapter,
    fixture_bytes,
    image_assignments,
    pdf_assignments,
    permitted_context,
    put_source,
    scope,
    task,
)

from pados.capability.adapter import CapabilityAdapter, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ObjectStoreArtifacts
from pados.capability.model import (
    AdapterDescriptor,
    Determinism,
    Distribution,
    LicenseInfo,
    LifecycleState,
)
from pados.capability.policy import AuthorizedScope, PolicyCode
from pados.capability.receipts import VerificationStatus
from pados.capability.seams import InferenceRequest, InferenceSelection
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.enterprise.lineage import LineageGraph, LineageKind
from pados.lifecycle.minimization import assert_minimized
from pados.packs.ocr.contracts import BlockKind, PageRange, PageStatus, QualityRequirement
from pados.packs.ocr.descriptors import DOCUMENT_OCR, VISION_MODEL_OCR, VISION_MODEL_OCR_CLOUD
from pados.packs.ocr.engines import VisionModelOcrAdapter
from pados.packs.ocr.fixture import TEST_FIXTURE_MODE, FixtureOcrAdapter
from pados.packs.ocr.lineage import LINEAGE_KEYS, register_ocr_lineage
from pados.packs.ocr.reference import LineLayoutAdapter
from pados.packs.ocr.pipeline import (
    IMAGE_TEMPLATE,
    IMAGE_VISION_CLOUD_TEMPLATE,
    IMAGE_VISION_TEMPLATE,
    PDF_HYBRID_TEMPLATE,
    PDF_TEXT_LAYER_TEMPLATE,
    TEMPLATES,
    DecompositionTemplate,
    OcrPipelineFailed,
    OcrPipelineRefused,
    PipelineResult,
    StageSpec,
    is_fixture_adapter,
    policy_view,
    run_pipeline,
)

TEXT_WORDS = ("Quarterly", "Region", "INVOICE", "Scanned", "synthetic")


def _run(store: ObjectStoreArtifacts, template: DecompositionTemplate, ocr_task: object,
         assignments: dict[str, CapabilityAdapter], **kwargs: object) -> PipelineResult:
    options: dict[str, object] = {"store": store, "scope": scope(), "revalidate": scope, "project_id": PROJECT,
                                  "plan_id": "plan-ocr-1", "allow_experimental": True, "clock": fixed_clock()}
    if any(is_fixture_adapter(adapter) for adapter in assignments.values()):  # explicit test mode, tests only
        options["test_fixtures"] = TEST_FIXTURE_MODE
    options.update(kwargs)
    return run_pipeline(template, ocr_task, assignments, **options)  # type: ignore[arg-type]


def _pdf(store: ObjectStoreArtifacts) -> object:
    return task(put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf"))


def _png(store: ObjectStoreArtifacts, **overrides: object) -> object:
    return task(put_source(store, fixture_bytes("scan_page.png"), "image/png"), **overrides)


# --- templates ---------------------------------------------------------------------------------

def test_templates_are_plain_data_with_stable_hashes() -> None:
    data = PDF_TEXT_LAYER_TEMPLATE.as_data()
    assert json.loads(json.dumps(data)) == data
    assert [s["capability_id"] for s in data["stages"]] == [
        "DOCUMENT_PREPROCESS", "DOCUMENT_OCR", "DOCUMENT_LAYOUT_ANALYSIS", "TABLE_EXTRACTION", "FIGURE_EXTRACTION",
        "DOCUMENT_STRUCTURE_PARSE"]
    assert "adapter" not in json.dumps(data)  # templates name capabilities, never adapters
    assert len({t.template_hash for t in TEMPLATES.values()}) == len(TEMPLATES)
    for bad in [(StageSpec("ocr", DOCUMENT_OCR, ("manifest",), "document"),),  # input not yet produced
                (StageSpec("a", "UNKNOWN_CAPABILITY", ("source",), "document"),),
                (StageSpec("prep", "DOCUMENT_PREPROCESS", ("source",), "document"),),  # wrong output contract
                (StageSpec("prep", "DOCUMENT_PREPROCESS", ("source",), "manifest"),)]:  # no final document
        with pytest.raises(ValueError):
            DecompositionTemplate("ocr.bad", "1", frozenset({"application/pdf"}), bad)


def test_template_resolution_refuses_unsupported_requests(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    with pytest.raises(OcrPipelineRefused, match="TEMPLATE_MEDIA_TYPE_UNSUPPORTED"):
        IMAGE_TEMPLATE.resolve(_pdf(store))  # type: ignore[arg-type]
    no_tables = DecompositionTemplate("ocr.no-tables", "1", frozenset({"image/png"}), tuple(
        s for s in IMAGE_TEMPLATE.stages if s.stage_id != "tables"))
    with pytest.raises(OcrPipelineRefused, match="TEMPLATE_CANNOT_EXTRACT_TABLES"):
        no_tables.resolve(_png(store))  # type: ignore[arg-type]
    skipped = IMAGE_TEMPLATE.resolve(_png(store, require_tables=False))  # type: ignore[arg-type]
    assert "tables" not in [s.stage_id for s in skipped]


# --- end to end --------------------------------------------------------------------------------

def test_text_layer_pdf_end_to_end(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    result = _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf(store), pdf_assignments())
    first, second = result.document.pages
    assert first.status is PageStatus.TEXT_LAYER and second.status is PageStatus.NEEDS_OCR
    heading = next(b for b in first.blocks if b.kind is BlockKind.HEADING)
    assert (heading.text, heading.level) == ("1. Quarterly Report", 1)
    table = first.tables[0]
    assert table.cells == (("Region", "Units", "Revenue"), ("North", "120", "4500"), ("South", "95", "3100"))
    assert table.header_row and (table.rows, table.columns) == (3, 3)
    assert [(f.name, f.width_px, f.height_px) for f in second.figures] == [("Im1", 2, 2)]
    assert second.blocks == ()  # nothing was invented for the scanned page
    assert result.document.sections[0].heading_block_id == heading.block_id
    assert result.quality.met is False and result.quality.reasons == ("NEEDS_OCR_PAGES_PRESENT",)
    assert result.document.evidence.engines[0].backend == "pypdf" and result.semantic_authority == "NONE"
    receipt = result.plan_receipt
    assert receipt.verify_structure() and receipt.expected_stage_ids == (
        "preprocess", "recognize", "layout", "tables", "figures", "structure")
    assert all(s.verification is VerificationStatus.PASSED and s.verifier_id for s in receipt.stages)
    assert receipt.stages[-1].outputs == (result.document_ref,) and receipt.semantic_authority == "NONE"
    assert all(s.policy_decision_hash == result.decision_hashes[s.stage_id] for s in receipt.stages)
    dumped = json.dumps(receipt.model_dump(mode="json"))
    assert not any(word in dumped for word in TEXT_WORDS)  # receipts carry hashes and counts, not content


def test_hybrid_plan_fills_needs_ocr_pages_with_an_explicit_second_stage(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    result = _run(store, PDF_HYBRID_TEMPLATE, _pdf(store), pdf_assignments(hybrid=True))
    first, second = result.document.pages
    assert first.status is PageStatus.TEXT_LAYER and first.producer == "ocr.pdf-text-layer"
    assert second.status is PageStatus.RECOGNIZED and second.producer == "ocr.fixture-truth"
    summary = result.document.evidence.page_confidence
    assert summary[0].mean is None and summary[1].mean == pytest.approx(0.89) and summary[1].minimum == 0.85
    engines = result.document.evidence.engines  # attempted vs perceived, per the explicit page policy
    assert [(e.attempted_pages, e.pages) for e in engines] == [((1, 2), (1,)), ((2,), (2,))]
    assert {e.operation for e in engines} == {"RECOGNIZE_TEXT"}
    assert result.quality.met


def test_image_pipeline_with_confidence_and_quality_gate(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    result = _run(store, IMAGE_TEMPLATE, _png(store, preserve_layout=True), image_assignments())
    page = result.document.pages[0]
    assert [b.kind for b in page.blocks].count(BlockKind.LINE) == 3  # preserve_layout keeps the lines
    assert page.tables[0].cells == (("Item", "Qty", "Price"), ("Pen", "2", "3.50"))
    assert page.tables[0].confidence == 0.88 and page.tables[0].bbox is not None
    assert result.quality.met
    strict = _png(store, quality=QualityRequirement(min_mean_confidence=0.95))
    flat = _run(store, IMAGE_TEMPLATE, strict, image_assignments())
    assert flat.quality.reasons == ("CONFIDENCE_BELOW_REQUIREMENT",)
    assert not any(b.kind is BlockKind.LINE for b in flat.document.pages[0].blocks)  # layout not preserved
    assert flat.document.evidence.page_confidence[0].scored_blocks == 3  # evidence survives the structure stage


def test_pipeline_is_deterministic_on_replay(tmp_path: Path) -> None:
    runs = []
    for name in ("a", "b"):
        store = ObjectStoreArtifacts(tmp_path / name)
        runs.append(_run(store, PDF_HYBRID_TEMPLATE, _pdf(store), pdf_assignments(hybrid=True)))
    first, second = runs
    assert first.document_ref == second.document_ref and first.plan_hash == second.plan_hash
    assert dict(first.stage_outputs) == dict(second.stage_outputs)
    assert first.plan_receipt.envelope_hash == second.plan_receipt.envelope_hash


# --- refusals ----------------------------------------------------------------------------------

def test_experimental_adapters_need_an_explicit_opt_in(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    with pytest.raises(OcrPipelineRefused, match="EXPERIMENTAL_ADAPTER_NOT_ALLOWED"):
        _run(store, IMAGE_TEMPLATE, _png(store), image_assignments(), allow_experimental=False)


def test_assignment_and_authorization_refusals(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    pdf = _pdf(store)
    missing = pdf_assignments()
    del missing["figures"]
    with pytest.raises(OcrPipelineRefused, match="STAGE_ADAPTER_UNASSIGNED"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, pdf, missing)
    swapped = {**pdf_assignments(), "layout": pdf_assignments()["tables"]}
    with pytest.raises(OcrPipelineRefused, match="CAPABILITY_MISMATCH"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, pdf, swapped)
    narrow = scope(["CAPABILITY:DOCUMENT_PREPROCESS", "CAPABILITY:DOCUMENT_OCR"])
    with pytest.raises(OcrPipelineRefused, match="NOT_AUTHORIZED") as refused:
        _run(store, PDF_TEXT_LAYER_TEMPLATE, pdf, pdf_assignments(), scope=narrow, revalidate=lambda: narrow)
    assert refused.value.stage_id == "layout"
    with pytest.raises(OcrPipelineRefused, match="SCOPE_PROJECT_MISMATCH"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, pdf, pdf_assignments(), project_id="another-project")


def test_revoked_grant_stops_execution_before_the_adapter_runs(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    pdf = _pdf(store)

    def revoked() -> AuthorizedScope:
        raise PermissionError("CAPABILITY_NOT_AUTHORIZED")

    with pytest.raises(OcrPipelineRefused, match="AUTHORIZATION_REVOKED_BEFORE_EXECUTION"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, pdf, pdf_assignments(), revalidate=revoked)
    changed = replace(scope(), grant_hash="0" * 64)
    with pytest.raises(OcrPipelineRefused, match="AUTHORIZATION_CHANGED_BEFORE_EXECUTION"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, pdf, pdf_assignments(), revalidate=lambda: changed)


def _wan_ocr_adapter() -> FixtureOcrAdapter:
    adapter = fixture_adapter()
    adapter._descriptor = AdapterDescriptor(  # noqa: SLF001 - a hypothetical cloud engine for DOCUMENT_OCR
        adapter_id="ocr.hypothetical-cloud", capability_id=DOCUMENT_OCR, version="1", backend="cloud",
        determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.WAN_REQUIRED,
        license=LicenseInfo(spdx="LicenseRef-Provider-Terms", distribution=Distribution.EXTERNAL_SERVICE))
    return adapter


def test_restricted_document_is_never_planned_to_a_wan_adapter(tmp_path: Path) -> None:
    candidates: list[CapabilityAdapter] = [VisionModelOcrAdapter(cloud=True)]
    for network in NetworkRequirement:
        restricted = replace(permitted_context(candidates[0]), classification=DataClassification.RESTRICTED,
                             network_policy=network)
        codes = dict(policy_view(VISION_MODEL_OCR_CLOUD, candidates, restricted))
        assert codes["ocr.vision-model.cloud"] is not PolicyCode.ALLOWED
    restricted_wan = replace(permitted_context(candidates[0]), classification=DataClassification.RESTRICTED)
    assert dict(policy_view(VISION_MODEL_OCR_CLOUD, candidates, restricted_wan))[
        "ocr.vision-model.cloud"] is PolicyCode.CLASSIFICATION_EXCEEDS_CAPABILITY
    local_task = replace(permitted_context(candidates[0]), network_policy=NetworkRequirement.LOCAL_ONLY)
    assert dict(policy_view(VISION_MODEL_OCR_CLOUD, candidates, local_task))[
        "ocr.vision-model.cloud"] is PolicyCode.NETWORK_NOT_PERMITTED
    # A WAN engine can never implement the local-only DOCUMENT_OCR capability, whatever the task allows.
    wan = _wan_ocr_adapter()
    wide = replace(permitted_context(wan), classification=DataClassification.PUBLIC,
                   network_policy=NetworkRequirement.WAN_REQUIRED)
    assert dict(policy_view(DOCUMENT_OCR, [wan], wide))["ocr.hypothetical-cloud"] in {
        PolicyCode.ADAPTER_WIDENS_CAPABILITY_NETWORK, PolicyCode.NETWORK_NOT_PERMITTED}
    # The runner refuses at the recognize stage and never executes the WAN adapter.
    store = ObjectStoreArtifacts(tmp_path)
    restricted_task = _png(store, classification=DataClassification.RESTRICTED,
                           network_policy=NetworkRequirement.WAN_REQUIRED)
    with pytest.raises(OcrPipelineRefused, match="CLASSIFICATION_EXCEEDS") as refused:
        _run(store, IMAGE_VISION_CLOUD_TEMPLATE, restricted_task, image_assignments(StubVision(cloud=True)),
             inference=Router())
    assert refused.value.stage_id == "recognize"
    with pytest.raises(OcrPipelineRefused, match="NETWORK_WIDENS|ADAPTER_WIDENS|NETWORK_NOT_PERMITTED"):
        _run(store, IMAGE_TEMPLATE, _png(store, classification=DataClassification.PUBLIC,
                                         network_policy=NetworkRequirement.WAN_REQUIRED),
             image_assignments(wan))


# --- model-runtime stages go through the Worker Router seam -----------------------------------

class StubVision(FixtureOcrAdapter):
    """Test double for a vision-model stage (replays fixture truth; no model, no engine)."""

    def __init__(self, *, cloud: bool = False) -> None:
        super().__init__(fixture_adapter()._truths)  # noqa: SLF001
        self._descriptor = VisionModelOcrAdapter(cloud=cloud).descriptor


class Router:
    def __init__(self, selected: str | None = "worker-vlm-1") -> None:
        self.selected = selected
        self.requests: list[InferenceRequest] = []

    def select(self, request: InferenceRequest) -> InferenceSelection:
        self.requests.append(request)
        return InferenceSelection(self.selected, "SELECTED" if self.selected else "NO_WORKER", "test", "e" * 64)


def test_model_runtime_stage_requires_worker_router_selection(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    png = _png(store)
    assignments = image_assignments(StubVision())
    with pytest.raises(OcrPipelineRefused, match="INFERENCE_DELEGATE_REQUIRED"):
        _run(store, IMAGE_VISION_TEMPLATE, png, assignments)
    with pytest.raises(OcrPipelineRefused, match="WORKER_ROUTER_NO_SELECTION"):
        _run(store, IMAGE_VISION_TEMPLATE, png, assignments, inference=Router(selected=None))
    router = Router()
    result = _run(store, IMAGE_VISION_TEMPLATE, png, assignments, inference=router)
    recognize = next(s for s in result.plan_receipt.stages if s.stage_id == "recognize")
    assert recognize.router_selection_hash == "e" * 64 and recognize.capability_id == VISION_MODEL_OCR
    assert [r.capability_id for r in router.requests] == [VISION_MODEL_OCR]
    assert router.requests[0].grant_hash == scope().grant_hash
    assert VisionModelOcrAdapter().descriptor.lifecycle is LifecycleState.EXPERIMENTAL


# --- failures fail closed ----------------------------------------------------------------------

def test_adapter_failures_fail_closed_with_codes(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    disguised = task(put_source(store, fixture_bytes("scan_page.png"), "application/pdf"))
    with pytest.raises(OcrPipelineFailed, match="MEDIA_TYPE_MISMATCH") as failed:
        _run(store, PDF_TEXT_LAYER_TEMPLATE, disguised, pdf_assignments())
    assert failed.value.stage_id == "preprocess"
    broken = task(put_source(store, b"%PDF-1.4\nthis is not a pdf", "application/pdf"))
    with pytest.raises(OcrPipelineFailed, match="PDF_UNREADABLE"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, broken, pdf_assignments())
    out_of_range = task(put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf"),
                        page_range=PageRange(first=2, last=5))
    with pytest.raises(OcrPipelineFailed, match="PAGE_RANGE_OUT_OF_BOUNDS"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, out_of_range, pdf_assignments())
    unknown = task(put_source(store, fixture_bytes("scan_page.png")[:-12] + b"\x00" * 12, "image/png"))
    with pytest.raises(OcrPipelineFailed, match="FIXTURE_TRUTH_NOT_FOUND") as failed:
        _run(store, IMAGE_TEMPLATE, unknown, image_assignments())
    assert failed.value.stage_id == "recognize"


def test_encrypted_pdf_is_refused(tmp_path: Path) -> None:
    import io

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(fixture_bytes("text_and_scan.pdf"))).pages:
        writer.add_page(page)
    try:
        writer.encrypt("open-" + "sesame", algorithm="RC4-128")
    except Exception as exc:  # pragma: no cover - depends on pypdf crypto providers
        pytest.skip(f"pypdf cannot encrypt here: {type(exc).__name__}")
    buffer = io.BytesIO()
    writer.write(buffer)
    store = ObjectStoreArtifacts(tmp_path)
    encrypted = task(put_source(store, buffer.getvalue(), "application/pdf"))
    with pytest.raises(OcrPipelineFailed, match="PDF_ENCRYPTED"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, encrypted, pdf_assignments())


def test_selected_page_range_is_honoured(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    second_only = task(put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf"),
                       page_range=PageRange(first=2, last=2), quality=QualityRequirement(allow_needs_ocr_pages=True))
    result = _run(store, PDF_TEXT_LAYER_TEMPLATE, second_only, pdf_assignments())
    assert [p.page_number for p in result.document.pages] == [2]
    assert result.quality.met  # the task explicitly accepts NEEDS_OCR pages


# --- lineage -----------------------------------------------------------------------------------

def test_lineage_reaches_every_ocr_derivative_on_revocation(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    result = _run(store, PDF_HYBRID_TEMPLATE, _pdf(store), pdf_assignments(hybrid=True))
    graph = LineageGraph()
    source_id = "src:" + result.document.source.sha256[:32]
    trusted = {source_id: result.document.source.sha256}
    with pytest.raises(ValueError, match="LINEAGE_SOURCE_NOT_REGISTERED"):
        register_ocr_lineage(graph, source_id=source_id, result=result, trusted_sources=trusted)
    graph.add(source_id, LineageKind.SOURCE)
    lineage = register_ocr_lineage(graph, source_id=source_id, result=result, trusted_sources=trusted)
    assert len(lineage.intermediate_nodes) == len(result.stage_outputs) - 1
    assert lineage.chunk_nodes and graph.nodes[lineage.document_node] is LineageKind.NORMALIZED_ARTIFACT
    assert all(graph.nodes[node] is LineageKind.CHUNK for node in lineage.chunk_nodes)
    assert_minimized(lineage.record, allowed_keys=LINEAGE_KEYS, max_items=4096)
    assert not any(word in json.dumps(lineage.record) for word in TEXT_WORDS)
    again = register_ocr_lineage(graph, source_id=source_id, result=result, trusted_sources=trusted)
    assert again.all_nodes == lineage.all_nodes  # deterministic node ids (idempotent)
    plan = graph.plan_revocation(source_id, reason="source deleted")
    assert set(plan["invalidate_automatically"]) == set(lineage.all_nodes)
    graph.apply_revocation(plan)
    assert all(graph.is_revoked(node) for node in lineage.all_nodes)
    with pytest.raises(ValueError, match="LINEAGE_SOURCE_REVOKED"):
        register_ocr_lineage(graph, source_id=source_id, result=result, trusted_sources=trusted)


class PartialRecognizer(FixtureOcrAdapter):
    """Marks its (valid) output partial, as an engine that failed on some pages would."""

    def __init__(self) -> None:
        super().__init__(fixture_adapter()._truths)  # noqa: SLF001

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        output = super().execute(request, context=context)
        return StageOutput(outputs=output.outputs, metrics=output.metrics, evidence=output.evidence, partial=True)


class PartialLayout(LineLayoutAdapter):
    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        output = super().execute(request, context=context)
        return StageOutput(outputs=output.outputs, evidence=output.evidence, partial=True)


def test_partial_outputs_are_recorded_or_refused_by_failure_semantics(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    result = _run(store, IMAGE_TEMPLATE, _png(store), image_assignments(PartialRecognizer()))
    recognize = next(s for s in result.plan_receipt.stages if s.stage_id == "recognize")
    assert recognize.partial and recognize.failure_code == "STAGE_PARTIAL_OUTPUT"
    assert recognize.verification is VerificationStatus.NOT_VERIFIED and recognize.verifier_id is None
    assert result.plan_receipt.verify_structure()
    with pytest.raises(OcrPipelineFailed, match="STAGE_PARTIAL_NOT_PERMITTED") as failed:
        _run(store, IMAGE_TEMPLATE, _png(store), {**image_assignments(), "layout": PartialLayout()})
    assert failed.value.stage_id == "layout"
