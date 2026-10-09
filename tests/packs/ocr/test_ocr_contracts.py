"""OCR pack contracts: task contract, ocr.document/v1, engine-result normalization, descriptors, licenses."""
from __future__ import annotations

from pathlib import Path

import pytest
from ocr_support import FIXTURES, every_adapter, load_builder
from pydantic import ValidationError

from pados.capability.adapter import CapabilityAdapterError
from pados.capability.artifacts import ArtifactRef
from pados.capability.model import CapabilityFamily, Distribution, LifecycleState
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.ocr.contracts import (
    OCR_DOCUMENT_CONTRACT,
    BBox,
    Block,
    BlockKind,
    EngineRecord,
    OcrContractError,
    OcrDocument,
    OcrEvidence,
    OcrPage,
    OcrTaskContract,
    PageConfidence,
    PageRange,
    PageStatus,
    StageParameters,
    Table,
)
from pados.packs.ocr.descriptors import (
    OCR_CAPABILITIES,
    PIPELINE_CAPABILITIES,
    VISION_MODEL_OCR,
    VISION_MODEL_OCR_CLOUD,
    determinism_conforms,
)
from pados.packs.ocr.engines import (
    LICENSE_REVIEW_REQUIRED,
    MINERU_ADAPTER_ID,
    TesseractCliAdapter,
    TesseractConfig,
    parse_tesseract_tsv,
    tesseract_argv,
    tesseract_environment,
    tesseract_languages,
)
from pados.packs.ocr.fixture import FixtureOcrAdapter, FixtureTruth, load_truths
from pados.packs.ocr.normalize import recognized_pages

PDF_REF = ArtifactRef(sha256="a" * 64, media_type="application/pdf", size_bytes=10, kind="ocr.source")
PNG_REF = ArtifactRef(sha256="b" * 64, media_type="image/png", size_bytes=10, kind="ocr.source")


def _task(**overrides: object) -> OcrTaskContract:
    values: dict[str, object] = {"artifact_ref": PDF_REF, "media_type": "application/pdf",
                                 "classification": DataClassification.INTERNAL}
    values.update(overrides)
    return OcrTaskContract(**values)  # type: ignore[arg-type]


# --- task contract -----------------------------------------------------------------------------

def test_task_contract_defaults_are_local_only_and_bounded() -> None:
    task = _task(language_hints=("en", "zh-Hant", "pt-BR", "es-419"), page_range=PageRange(first=1, last=3))
    assert task.network_policy is NetworkRequirement.LOCAL_ONLY
    params = StageParameters.parse(task.stage_parameters())
    assert params.select_pages(5) == (1, 2, 3)
    assert len(task.task_hash) == 64 and task.task_hash == _task(language_hints=task.language_hints,
                                                                  page_range=task.page_range).task_hash


@pytest.mark.parametrize("overrides", [
    {"language_hints": ("english",)}, {"language_hints": ("en", "en")}, {"language_hints": ("en_US",)},
    {"language_hints": tuple(f"e{c}" for c in "abcdefghi")},  # more than 8 hints
    {"media_type": "image/png"},  # does not match the artifact claim
    {"artifact_ref": PNG_REF.model_copy(update={"media_type": "text/html"}), "media_type": "text/html"},
    {"artifact_ref": PNG_REF, "media_type": "image/png", "page_range": PageRange(first=1, last=2)},
    {"artifact_ref": PDF_REF.model_copy(update={"kind": "ocr.document"})},
    {"network_policy": "ANYWHERE"}, {"unexpected": True},
])
def test_task_contract_fails_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _task(**overrides)


def test_page_ranges_are_ordered_bounded_and_never_clipped() -> None:
    with pytest.raises(ValidationError):
        PageRange(first=3, last=2)
    with pytest.raises(ValidationError):
        PageRange(first=1, last=5000)
    params = StageParameters.parse(_task(page_range=PageRange(first=2, last=4)).stage_parameters())
    with pytest.raises(OcrContractError, match="PAGE_RANGE_OUT_OF_BOUNDS"):
        params.select_pages(3)
    with pytest.raises(OcrContractError, match="STAGE_PARAMETERS_INVALID"):
        StageParameters.parse({**_task().stage_parameters(), "command": "rm"})


# --- ocr.document/v1 ---------------------------------------------------------------------------

def _evidence(**overrides: object) -> OcrEvidence:
    values: dict[str, object] = {"input_sha256": "a" * 64, "input_media_type": "application/pdf", "pages_total": 1}
    values.update(overrides)
    return OcrEvidence(**values)  # type: ignore[arg-type]


def _line(block_id: str = "p1.l1", **overrides: object) -> Block:
    values: dict[str, object] = {"block_id": block_id, "kind": BlockKind.LINE, "text": "hello"}
    values.update(overrides)
    return Block(**values)  # type: ignore[arg-type]


def test_document_round_trips_canonically() -> None:
    page = OcrPage(page_number=1, status=PageStatus.RECOGNIZED, blocks=(_line(confidence=0.912345),),
                   reading_order=("p1.l1",))
    summary = PageConfidence(page_number=1, status=PageStatus.RECOGNIZED, mean=0.9123, minimum=0.9123,
                             scored_blocks=1)
    document = OcrDocument(source=PDF_REF, pages=(page,), evidence=_evidence(page_confidence=(summary,)))
    data = document.to_bytes()
    assert OcrDocument.from_bytes(data).to_bytes() == data
    assert document.schema_version == OCR_DOCUMENT_CONTRACT and document.semantic_authority == "NONE"
    assert document.pages[0].blocks[0].confidence == 0.9123
    with pytest.raises(OcrContractError, match="OCR_DOCUMENT_INVALID"):
        OcrDocument.from_bytes(data.replace(b'"NONE"', b'"ACCEPTED"'))


@pytest.mark.parametrize("page", [
    {"status": PageStatus.TEXT_LAYER, "blocks": (_line(confidence=0.5),)},  # text layer is not a recognition score
    {"status": PageStatus.TEXT_LAYER, "blocks": ()},  # an empty text layer is NEEDS_OCR
    {"status": PageStatus.NEEDS_OCR, "blocks": (_line(),)},  # NEEDS_OCR never carries invented text
    {"status": PageStatus.RECOGNIZED, "blocks": (_line(),), "reading_order": ("p1.l9",)},
    {"status": PageStatus.RECOGNIZED, "blocks": (_line(), _line())},  # duplicate ids
    {"status": PageStatus.RECOGNIZED, "blocks": ({"block_id": "p1.l1", "kind": "LINE", "text": "a\x00b"},)},
    {"status": PageStatus.RECOGNIZED, "width": 10.0},  # incomplete geometry
])
def test_page_invariants(page: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OcrPage(page_number=1, **page)  # type: ignore[arg-type]


def test_bbox_and_tables_are_validated() -> None:
    with pytest.raises(ValidationError):
        BBox(x0=0.5, y0=0, x1=0.4, y1=1)
    with pytest.raises(ValidationError):
        BBox(x0=0, y0=0, x1=1.2, y1=1)
    with pytest.raises(ValidationError, match="TABLE_NOT_RECTANGULAR"):
        Table(table_id="p1.t1", rows=2, columns=2, cells=(("a", "b"), ("c",)), method="test")
    with pytest.raises(ValidationError, match="TABLE_TOO_LARGE"):
        Table(table_id="p1.t1", rows=100, columns=100, cells=(), method="test")


def test_evidence_is_minimized_and_bound_to_the_input() -> None:
    leaked = EngineRecord(adapter_id="x", adapter_version="1", backend="b", engine_version="sk-" + "a" * 24,
                          license_spdx="MIT", distribution="NONE", operation="RECOGNIZE_TEXT", attempted_pages=(1,))
    with pytest.raises(ValidationError):
        _evidence(engines=(leaked,))
    page = OcrPage(page_number=1, status=PageStatus.NEEDS_OCR)
    with pytest.raises(ValidationError, match="EVIDENCE_INPUT_MISMATCH"):
        OcrDocument(source=PDF_REF, pages=(page,), evidence=_evidence(input_sha256="c" * 64))


# --- engine-result normalization ---------------------------------------------------------------

def _raw(lines: list[dict[str, object]], page_number: int = 1, **page: object) -> dict[str, object]:
    return {"pages": [{"page_number": page_number, "width": 100, "height": 50, "unit": "px", "lines": lines,
                       **page}]}


def test_engine_results_are_normalized_and_bounded() -> None:
    pages = recognized_pages(_raw([{"text": " total\t 5 ", "bbox": [10, 5, 60, 15], "confidence": 0.8},
                                   {"text": "   "}]), selected=(1,), producer="ocr.x")
    page = pages[1]
    assert page.status is PageStatus.RECOGNIZED and len(page.blocks) == 1
    assert page.blocks[0].text == "total  5" and page.blocks[0].bbox == BBox(x0=0.1, y0=0.1, x1=0.6, y1=0.3)
    for raw, code in [(_raw([{"text": "x", "bbox": [0, 0, 101, 5]}]), "BBOX_OUTSIDE_PAGE"),
                      (_raw([], page_number=2), "PAGE_UNEXPECTED"),
                      (_raw([{"text": "x", "bbox": [0, 0, 1, 1]}], width=None, height=None, unit=None),
                       "BBOX_WITHOUT_GEOMETRY"),
                      (_raw([{"text": "x", "confidence": 7}]), "ENGINE_RESULT_INVALID"),
                      ({"pages": [], "extra": 1}, "ENGINE_RESULT_INVALID")]:
        with pytest.raises(OcrContractError, match=code):
            recognized_pages(raw, selected=(1,), producer="ocr.x")


def test_tesseract_tsv_parsing_and_fixed_argv(tmp_path: Path) -> None:
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [header, "1\t1\t0\t0\t0\t0\t0\t0\t200\t100\t-1\t",
            "5\t1\t1\t1\t1\t1\t10\t10\t40\t12\t96.5\tHello", "5\t1\t1\t1\t1\t2\t60\t11\t50\t12\t91.5\tworld",
            "5\t1\t1\t1\t2\t1\t10\t40\t30\t12\t-1\t?"]
    result = parse_tesseract_tsv("\n".join(rows))
    page = result.pages[0]
    assert (page.width, page.height, page.unit) == (200, 100, "px")
    assert page.lines[0].text == "Hello world" and page.lines[0].bbox == (10, 10, 110, 23)
    assert page.lines[0].confidence == pytest.approx(0.94) and page.lines[1].confidence is None
    with pytest.raises(OcrContractError):
        parse_tesseract_tsv("not a tsv")
    executable = tmp_path / "tesseract.exe"
    config = TesseractConfig(executable=executable, engine_version="5.4.1")
    argv = tesseract_argv(config, tesseract_languages(("en", "zh-Hant-TW", "pt-BR")))
    assert argv == [str(executable), "stdin", "stdout", "-l", "eng+chi_tra+por", "--psm", "3", "tsv"]
    assert set(tesseract_environment()) <= {"OMP_THREAD_LIMIT", "LC_ALL", "SYSTEMROOT"}
    with pytest.raises(CapabilityAdapterError, match="LANGUAGE_UNSUPPORTED"):
        tesseract_languages(("tlh",))
    with pytest.raises(ValueError):
        TesseractConfig(executable=Path("tesseract"), engine_version="5")
    with pytest.raises(ValueError):
        TesseractConfig(executable=executable, engine_version="5; curl x")
    # A configured but absent executable is still unavailable (never executed here).
    assert not TesseractCliAdapter(TesseractConfig(executable=tmp_path / "absent-tesseract.exe",
                                                   engine_version="5.4.1")).available()


# --- descriptors and licenses ------------------------------------------------------------------

def test_each_capability_is_a_separate_replaceable_stage() -> None:
    assert PIPELINE_CAPABILITIES == ("DOCUMENT_PREPROCESS", "DOCUMENT_OCR", "DOCUMENT_LAYOUT_ANALYSIS",
                                     "TABLE_EXTRACTION", "FIGURE_EXTRACTION", "DOCUMENT_STRUCTURE_PARSE")
    for capability_id in PIPELINE_CAPABILITIES:
        descriptor = OCR_CAPABILITIES[capability_id]
        assert descriptor.network_requirement is NetworkRequirement.LOCAL_ONLY
        assert descriptor.family is not CapabilityFamily.MODEL_RUNTIME
    cloud = OCR_CAPABILITIES[VISION_MODEL_OCR_CLOUD]
    assert cloud.family is CapabilityFamily.MODEL_RUNTIME and cloud.network_requirement is NetworkRequirement.WAN_REQUIRED
    assert cloud.max_classification is DataClassification.INTERNAL
    assert OCR_CAPABILITIES[VISION_MODEL_OCR].network_requirement is NetworkRequirement.LOCAL_ONLY


def test_every_adapter_conforms_and_records_its_license() -> None:
    adapters = {adapter.descriptor.adapter_id: adapter.descriptor for adapter in every_adapter()}
    assert len(adapters) == 12
    for descriptor in adapters.values():
        capability = OCR_CAPABILITIES[descriptor.capability_id]
        assert determinism_conforms(descriptor.determinism, capability.determinism)
        assert descriptor.delegates_to_worker_router is (capability.family is CapabilityFamily.MODEL_RUNTIME)
    expected = {"ocr.pdf-text-layer": ("BSD-3-Clause", Distribution.RUNTIME_DEPENDENCY),
                "ocr.tesseract-cli": ("Apache-2.0", Distribution.RUNTIME_OPTIONAL),
                "ocr.paddleocr": ("Apache-2.0", Distribution.RUNTIME_OPTIONAL),
                "ocr.mineru-style": ("AGPL-3.0-only", Distribution.RUNTIME_OPTIONAL),
                "ocr.vision-model.local": ("LicenseRef-Model-Specific", Distribution.RUNTIME_OPTIONAL),
                "ocr.vision-model.cloud": ("LicenseRef-Provider-Terms", Distribution.EXTERNAL_SERVICE)}
    for adapter_id, (spdx, distribution) in expected.items():
        assert (adapters[adapter_id].license.spdx, adapters[adapter_id].license.distribution) == (spdx, distribution)
    assert adapters["ocr.pdf-text-layer"].license.engine_version != "n/a"  # the installed pypdf version
    assert MINERU_ADAPTER_ID in LICENSE_REVIEW_REQUIRED and adapters[MINERU_ADAPTER_ID].lifecycle is LifecycleState.EXPERIMENTAL
    fixture = adapters["ocr.fixture-truth"]
    assert fixture.lifecycle is LifecycleState.EXPERIMENTAL and fixture.backend == "fixture"
    assert FixtureOcrAdapter.production_eligible is False


# --- fixtures ----------------------------------------------------------------------------------

def test_committed_fixtures_are_reproducible_from_the_builder() -> None:
    built = load_builder().build_all()
    for name, data in built.items():
        assert (FIXTURES / name).read_bytes() == data, name
    truths = load_truths(FIXTURES)
    assert len(truths) == 2


def test_fixture_truth_files_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "bad.ocr-truth.json").write_text('{"label": "TEST_FIXTURE_ONLY"}', encoding="utf-8")
    with pytest.raises(OcrContractError, match="FIXTURE_TRUTH_INVALID"):
        load_truths(tmp_path)
    with pytest.raises(ValidationError):
        FixtureTruth.model_validate({"label": "PRODUCTION", "input_sha256": "a" * 64, "result": {"pages": []}})
    assert not FixtureOcrAdapter({}).available()


def test_header_sniffing_and_image_geometry() -> None:
    from pados.packs.ocr.reference import jpeg_size, png_size, sniff_media_type

    png = (FIXTURES / "scan_page.png").read_bytes()
    assert sniff_media_type(png) == "image/png" and png_size(png) == (64, 16)
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + (30).to_bytes(2, "big") + (40).to_bytes(2, "big")
    jpeg = b"\xff\xd8" + app0 + sof0 + b"\x03" + b"\x00" * 9 + b"\xff\xd9"
    assert sniff_media_type(jpeg) == "image/jpeg" and jpeg_size(jpeg) == (40, 30)
    assert jpeg_size(b"\xff\xd8" + b"\x00" * 20) is None and png_size(b"\x89PNG") is None
    assert sniff_media_type(b"II*\x00rest") == "image/tiff" and sniff_media_type(b"<html>") is None
