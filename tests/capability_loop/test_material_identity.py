# SPDX-License-Identifier: AGPL-3.0-only
"""Round-2 Finding 3: a material registration's own ref must match the key and the requested ref.

Looking a registration up by key is not enough: a mis-keyed registration (key material:A holding a
registration whose material_ref is material:B) must fail closed, so the mapping key, the registration's
declared ref and the requested material ref are all the same identity.
"""
from __future__ import annotations

from pathlib import Path

from loop_learning_support import (
    ENDPOINT,
    HEALTH_OK,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    PROJECT,
    FakeHttp,
    contract,
    generate_ok,
    grant,
    learning_request,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.learning import LearningService
from pados.capability_loop.learning_contracts import MaterialRef
from pados.capability_loop.materials import MaterialRegistration, MaterialResolutionCode, resolve_materials
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

DIGEST = "a" * 64


def _material(ref: str = "material:a") -> MaterialRef:
    return MaterialRef(material_ref=ref, content_digest=DIGEST, media_kind="pdf")


def test_resolver_accepts_consistent_identity() -> None:
    request = learning_request(materials=(_material("material:a"),))
    reg = MaterialRegistration(material_ref="material:a", project_id=PROJECT, content_digest=DIGEST)
    result = resolve_materials(request, {"material:a": reg})
    assert result.code is MaterialResolutionCode.RESOLVED


def test_resolver_rejects_miskeyed_registration() -> None:
    request = learning_request(materials=(_material("material:a"),))
    # Keyed under material:a, but the registration declares a different material_ref.
    reg = MaterialRegistration(material_ref="material:b", project_id=PROJECT, content_digest=DIGEST)
    result = resolve_materials(request, {"material:a": reg})
    assert result.code is MaterialResolutionCode.REGISTRATION_IDENTITY_MISMATCH


def test_miskeyed_registration_refused_before_provider(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok()})
    store = LoopStore(tmp_path / "loop.json")
    service = LearningService(OpenMaicLearningProvider(ENDPOINT, http), store,
                              capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER)
    reg = MaterialRegistration(material_ref="material:b", project_id=PROJECT, content_digest=DIGEST)
    receipt = service.create_session(
        learning_request(materials=(_material("material:a"),)), contract=contract(), grant=grant(),
        classification=DataClassification.INTERNAL, material_registrations={"material:a": reg})
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == "MATERIAL_IDENTITY_MISMATCH"
    assert ("POST", "/api/generate-classroom") not in http.calls
