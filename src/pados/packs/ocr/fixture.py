# SPDX-License-Identifier: AGPL-3.0-only
"""Fixture OCR adapter: TEST / CONTRACT ADAPTER ONLY, never a production engine.

It "recognizes" nothing: it returns ground truth from a sidecar fixture keyed by the SHA-256 of the
input artifact, so the pipeline, policy and receipts are testable end-to-end without installing an
OCR engine. Its descriptor is ``EXPERIMENTAL`` with backend ``fixture`` and ``production_eligible``
is False. The pack runner refuses it unless the caller passes the explicit test-mode token
``TEST_FIXTURE_MODE`` (``run_pipeline(..., test_fixtures=TEST_FIXTURE_MODE)``), whatever
``allow_experimental`` says; production code paths never pass it. Every document it produces is
marked ``evidence.test_provenance = "FIXTURE"`` (inherited by every later stage), the run result has
``provenance = "FIXTURE"`` and every stage receipt of a test-mode run carries the metric
``test_fixture_mode = 1``, so a fixture result can never look like production output.
An input without a sidecar fails closed (``FIXTURE_TRUTH_NOT_FOUND``).

Given a prior ``ocr.document`` input, it fills only ``NEEDS_OCR`` pages (an explicit, planned
second recognition stage, never an implicit fallback).
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.model import AdapterDescriptor, Determinism, LicenseInfo, LifecycleState
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.ocr._stage import OcrStageAdapter, read_manifest, recognition_output, source_input
from pados.packs.ocr.contracts import OcrContractError, StageParameters
from pados.packs.ocr.descriptors import DOCUMENT_OCR
from pados.packs.ocr.normalize import EngineResult

FIXTURE_ADAPTER_ID = "ocr.fixture-truth"
FIXTURE_LABEL = "TEST_FIXTURE_ONLY"
TRUTH_SUFFIX = ".ocr-truth.json"
MAX_TRUTH_FILES = 64
MAX_TRUTH_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FixtureTruth(BaseModel):
    """Sidecar ground truth ``ocr.fixture-truth/v1`` (committed test data, never user content)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["ocr.fixture-truth/v1"] = "ocr.fixture-truth/v1"
    label: Literal["TEST_FIXTURE_ONLY"]
    input_sha256: str
    result: EngineResult

    @field_validator("input_sha256")
    @classmethod
    def sha(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("FIXTURE_INPUT_HASH_INVALID")
        return value


@dataclass(frozen=True)
class FixtureMode:
    """The explicit test-mode token, the ONLY way the runner admits fixture (non-production) adapters."""

    label: Literal["TEST_FIXTURE_ONLY"] = "TEST_FIXTURE_ONLY"


TEST_FIXTURE_MODE = FixtureMode()


def fixture_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=FIXTURE_ADAPTER_ID, capability_id=DOCUMENT_OCR, version="1", backend="fixture",
        determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, lifecycle=LifecycleState.EXPERIMENTAL,
        license=LicenseInfo(spdx="AGPL-3.0-only"))


def _truth(path: Path) -> FixtureTruth | None:
    try:
        return FixtureTruth.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValidationError, ValueError):
        return None


def load_truths(directory: Path) -> dict[str, EngineResult]:
    """Load ``*.ocr-truth.json`` sidecars (bounded count and size; any invalid file fails closed)."""
    files = sorted(Path(directory).glob(f"*{TRUTH_SUFFIX}"))
    if len(files) > MAX_TRUTH_FILES:
        raise OcrContractError("FIXTURE_TRUTH_TOO_MANY")
    truths: dict[str, EngineResult] = {}
    for path in files:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_TRUTH_BYTES:
            raise OcrContractError("FIXTURE_TRUTH_FILE_INVALID")
        truth = _truth(path)
        if truth is None:
            raise OcrContractError("FIXTURE_TRUTH_INVALID")
        if truth.input_sha256 in truths:
            raise OcrContractError("FIXTURE_TRUTH_DUPLICATE")
        truths[truth.input_sha256] = truth.result
    return truths


class FixtureOcrAdapter(OcrStageAdapter):
    """Deterministic ground-truth replay for tests; ``production_eligible`` is always False."""

    production_eligible = False

    def __init__(self, truths: Mapping[str, EngineResult]) -> None:
        super().__init__(fixture_descriptor())
        self._truths = dict(truths)

    @classmethod
    def from_directory(cls, directory: Path) -> FixtureOcrAdapter:
        return cls(load_truths(directory))

    def _ready(self) -> bool:
        return bool(self._truths)

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        source = source_input(request, params)
        manifest = read_manifest(request, context, source)
        truth = self._truths.get(source.sha256)
        if truth is None:
            raise CapabilityAdapterError("FIXTURE_TRUTH_NOT_FOUND")
        return recognition_output(self.descriptor, request, context, source, manifest, truth,
                                  extra={"fixture_label": FIXTURE_LABEL}, fixture=True)
