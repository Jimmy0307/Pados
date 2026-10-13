# SPDX-License-Identifier: AGPL-3.0-only
"""OCR / document-perception capability descriptors (vendor-neutral metadata; never authority).

Each capability is a separate, replaceable stage: preprocess -> recognize -> layout -> tables ->
figures -> structure. A capability's ``determinism`` is the WEAKEST determinism a conforming adapter
may declare (recognition, layout and table engines may be neural), so a probabilistic engine can
implement it while a deterministic one is preferred; fallback between adapters is never implicit.

Vision-language-model OCR is a MODEL_RUNTIME capability that delegates to the existing Worker Router.
Its local and cloud variants are separate capabilities, so a cloud (WAN) model needs its own
project grant and is refused by the shared policy for data above its classification ceiling.
"""
from __future__ import annotations

from types import MappingProxyType

from pados.capability.model import (
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FailureSemantics,
    FallbackPolicy,
    LifecycleState,
    Modality,
    VerificationMethod,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.ocr.contracts import OCR_DOCUMENT_CONTRACT, OCR_PREPROCESSED_CONTRACT, OCR_REQUEST_CONTRACT

DOCUMENT_PREPROCESS = "DOCUMENT_PREPROCESS"
DOCUMENT_OCR = "DOCUMENT_OCR"
DOCUMENT_LAYOUT_ANALYSIS = "DOCUMENT_LAYOUT_ANALYSIS"
TABLE_EXTRACTION = "TABLE_EXTRACTION"
FIGURE_EXTRACTION = "FIGURE_EXTRACTION"
DOCUMENT_STRUCTURE_PARSE = "DOCUMENT_STRUCTURE_PARSE"
VISION_MODEL_OCR = "VISION_MODEL_OCR"
VISION_MODEL_OCR_CLOUD = "VISION_MODEL_OCR_CLOUD"

def _descriptor(capability_id: str, *, family: CapabilityFamily, operation: str, input_contract: str,
                determinism: Determinism, modalities: tuple[Modality, ...],
                verification: tuple[VerificationMethod, ...], description: str,
                failure: FailureSemantics = FailureSemantics.FAIL_CLOSED,
                max_classification: DataClassification = DataClassification.RESTRICTED,
                network_requirement: NetworkRequirement = NetworkRequirement.LOCAL_ONLY,
                lifecycle: LifecycleState = LifecycleState.ACTIVE,
                output_contract: str = OCR_DOCUMENT_CONTRACT) -> CapabilityDescriptor:
    return CapabilityDescriptor(capability_id=capability_id, version="1", family=family, operation=operation,
                                input_contract=input_contract, output_contract=output_contract,
                                determinism=determinism, modalities=modalities,
                                max_classification=max_classification, network_requirement=network_requirement,
                                verification=verification, failure=failure, fallback=FallbackPolicy.NONE,
                                lifecycle=lifecycle, description=description)


_CATALOG = (
    _descriptor(DOCUMENT_PREPROCESS, family=CapabilityFamily.PERCEPTION, operation="NORMALIZE_INPUT",
                input_contract=OCR_REQUEST_CONTRACT, output_contract=OCR_PREPROCESSED_CONTRACT,
                determinism=Determinism.DETERMINISTIC, modalities=(Modality.PDF, Modality.IMAGE),
                verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
                description="Validate the input artifact type, page count, page selection and geometry"),
    _descriptor(DOCUMENT_OCR, family=CapabilityFamily.PERCEPTION, operation="RECOGNIZE_TEXT",
                input_contract=OCR_PREPROCESSED_CONTRACT, determinism=Determinism.PROBABILISTIC,
                modalities=(Modality.PDF, Modality.IMAGE),
                verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY,
                              VerificationMethod.REFERENCE_CHECK),
                failure=FailureSemantics.PARTIAL_WITH_EVIDENCE,
                description="Text lines per page from a text layer or a local recognition engine"),
    _descriptor(DOCUMENT_LAYOUT_ANALYSIS, family=CapabilityFamily.DOCUMENT_INTELLIGENCE,
                operation="ANALYZE_LAYOUT", input_contract=OCR_DOCUMENT_CONTRACT,
                determinism=Determinism.PROBABILISTIC, modalities=(Modality.TEXT, Modality.STRUCTURED),
                verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
                description="Group lines into blocks and fix the reading order"),
    _descriptor(TABLE_EXTRACTION, family=CapabilityFamily.DOCUMENT_INTELLIGENCE, operation="EXTRACT_TABLES",
                input_contract=OCR_DOCUMENT_CONTRACT, determinism=Determinism.PROBABILISTIC,
                modalities=(Modality.TEXT, Modality.TABLE),
                verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
                description="Detect tables and their cells"),
    _descriptor(FIGURE_EXTRACTION, family=CapabilityFamily.DOCUMENT_INTELLIGENCE, operation="EXTRACT_FIGURES",
                input_contract=OCR_DOCUMENT_CONTRACT, determinism=Determinism.PROBABILISTIC,
                modalities=(Modality.PDF, Modality.IMAGE),
                verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
                description="Record figure references per page"),
    _descriptor(DOCUMENT_STRUCTURE_PARSE, family=CapabilityFamily.DOCUMENT_INTELLIGENCE,
                operation="PARSE_STRUCTURE", input_contract=OCR_DOCUMENT_CONTRACT,
                determinism=Determinism.PROBABILISTIC, modalities=(Modality.TEXT, Modality.STRUCTURED),
                verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
                description="Headings, sections and the final normalized document"),
    _descriptor(VISION_MODEL_OCR, family=CapabilityFamily.MODEL_RUNTIME, operation="RECOGNIZE_TEXT",
                input_contract=OCR_PREPROCESSED_CONTRACT, determinism=Determinism.PROBABILISTIC,
                modalities=(Modality.IMAGE, Modality.PDF),
                verification=(VerificationMethod.SCHEMA, VerificationMethod.REFERENCE_CHECK,
                              VerificationMethod.HUMAN_REVIEW),
                failure=FailureSemantics.PARTIAL_WITH_EVIDENCE,
                max_classification=DataClassification.CONFIDENTIAL, lifecycle=LifecycleState.EXPERIMENTAL,
                description="Local vision-language model transcription via the Worker Router"),
    _descriptor(VISION_MODEL_OCR_CLOUD, family=CapabilityFamily.MODEL_RUNTIME, operation="RECOGNIZE_TEXT",
                input_contract=OCR_PREPROCESSED_CONTRACT, determinism=Determinism.PROBABILISTIC,
                modalities=(Modality.IMAGE, Modality.PDF),
                verification=(VerificationMethod.SCHEMA, VerificationMethod.REFERENCE_CHECK,
                              VerificationMethod.HUMAN_REVIEW),
                failure=FailureSemantics.PARTIAL_WITH_EVIDENCE,
                max_classification=DataClassification.INTERNAL,
                network_requirement=NetworkRequirement.WAN_REQUIRED, lifecycle=LifecycleState.EXPERIMENTAL,
                description="Cloud vision-language model transcription via the Worker Router (data leaves host)"),
)

OCR_CAPABILITIES: MappingProxyType[str, CapabilityDescriptor] = MappingProxyType(
    {descriptor.capability_id: descriptor for descriptor in _CATALOG})

PIPELINE_CAPABILITIES = (DOCUMENT_PREPROCESS, DOCUMENT_OCR, DOCUMENT_LAYOUT_ANALYSIS, TABLE_EXTRACTION,
                         FIGURE_EXTRACTION, DOCUMENT_STRUCTURE_PARSE)

_DETERMINISM_RANK = {Determinism.DETERMINISTIC: 0, Determinism.SEEDED: 1, Determinism.PROBABILISTIC: 2}


def determinism_conforms(adapter: Determinism, capability: Determinism) -> bool:
    """An adapter may be more deterministic than its capability allows, never less."""
    return _DETERMINISM_RANK[adapter] <= _DETERMINISM_RANK[capability]


def capability(capability_id: str) -> CapabilityDescriptor:
    try:
        return OCR_CAPABILITIES[capability_id]
    except KeyError as exc:
        raise KeyError("OCR_CAPABILITY_UNKNOWN") from exc
