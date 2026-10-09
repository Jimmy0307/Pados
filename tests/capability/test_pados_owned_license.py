"""Red-team F-7: descriptors of PADOS-owned code declare the Core license (OD-1), never Apache-2.0.

The value is written into durable receipts (``license_spdx``, ``backend_license_spdx``), so it must not suggest an
Apache grant for Core modules. Third-party engines keep the license of the engine they describe.
"""
from __future__ import annotations

from pados.capability.model import Distribution, LicenseInfo
from pados.packs.algorithms.registry import ALGORITHMS, adapter_descriptor
from pados.packs.extraction.table_dataset import extraction_descriptor
from pados.packs.intelligence.report import report_descriptor
from pados.packs.ocr.engines import paddle_descriptor, tesseract_descriptor
from pados.packs.ocr.fixture import fixture_descriptor
from pados.packs.ocr.reference import preprocess_descriptor
from pados.packs.ocr.text_layer import text_layer_descriptor
from pados.packs.retrieval.lexical import lexical_descriptor
from pados.training.backends import CONTRACT_ONLY_SPECS, AlgorithmFitBackend, ContractOnlyBackend
from pados.training.contracts import TrainingKind

CORE = "AGPL-3.0-only"


def test_the_default_license_is_the_core_license() -> None:
    assert LicenseInfo().spdx == CORE and LicenseInfo().distribution is Distribution.NONE


def test_every_pados_owned_descriptor_declares_the_core_license() -> None:
    owned = [adapter_descriptor(spec) for spec in ALGORITHMS.values()]
    owned += [extraction_descriptor(), report_descriptor(), lexical_descriptor(), fixture_descriptor(),
              preprocess_descriptor(), AlgorithmFitBackend().descriptor,
              ContractOnlyBackend(TrainingKind.EVALUATION_ONLY, CONTRACT_ONLY_SPECS[TrainingKind.EVALUATION_ONLY])
              .descriptor]
    for descriptor in owned:
        assert descriptor.license.spdx == CORE, descriptor.adapter_id


def test_third_party_engine_licenses_are_unchanged() -> None:
    assert tesseract_descriptor().license.spdx == "Apache-2.0"
    assert paddle_descriptor().license.spdx == "Apache-2.0"
    assert text_layer_descriptor().license.spdx == "BSD-3-Clause"
    for kind in (TrainingKind.LORA_FINETUNE, TrainingKind.EMBEDDING, TrainingKind.RERANKER):
        assert ContractOnlyBackend(kind, CONTRACT_ONLY_SPECS[kind]).descriptor.license.spdx == "Apache-2.0"
    vision = ContractOnlyBackend(TrainingKind.VISION_DETECTION, CONTRACT_ONLY_SPECS[TrainingKind.VISION_DETECTION])
    assert vision.descriptor.license.spdx == "AGPL-3.0-only"
