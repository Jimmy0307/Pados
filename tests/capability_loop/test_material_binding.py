# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 10: learning materials must be bound to the request's project before reaching the provider.

A ``material_ref`` is only syntactically valid on its own; knowing another project's material id must not let
it be used. A PADOS-owned resolver verifies, per material, that it is registered to ``request.project_id``
with the same ``content_digest`` before the OpenMAIC adapter is ever called. The adapter never judges
project ownership.
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
    binding_authority,
    contract,
    generate_ok,
    grant,
    learning_request,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.learning import LearningService, MaterialResolutionCode
from pados.capability_loop.learning_contracts import MaterialRef
from pados.capability_loop.materials import MaterialRegistration, resolve_materials
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _material(ref: str = "material:cad-primer", digest: str = DIGEST_A) -> MaterialRef:
    return MaterialRef(material_ref=ref, content_digest=digest, media_kind="pdf")


def _registration(ref: str = "material:cad-primer", project_id: str = PROJECT,
                  digest: str = DIGEST_A) -> MaterialRegistration:
    return MaterialRegistration(material_ref=ref, project_id=project_id, content_digest=digest)


def _service(store: LoopStore, http: FakeHttp) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), store,
                           capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER, authority=binding_authority())


# -- pure resolver ---------------------------------------------------------
def test_resolver_accepts_same_project_matching_digest() -> None:
    request = learning_request(materials=(_material(),))
    result = resolve_materials(request, {_registration().material_ref: _registration()})
    assert result.code is MaterialResolutionCode.RESOLVED
    assert result.material_refs == ("material:cad-primer",)


def test_resolver_rejects_cross_project() -> None:
    request = learning_request(materials=(_material(),))
    result = resolve_materials(request, {"material:cad-primer": _registration(project_id="proj-other")})
    assert result.code is MaterialResolutionCode.CROSS_PROJECT


def test_resolver_rejects_digest_mismatch() -> None:
    request = learning_request(materials=(_material(digest=DIGEST_A),))
    result = resolve_materials(request, {"material:cad-primer": _registration(digest=DIGEST_B)})
    assert result.code is MaterialResolutionCode.DIGEST_MISMATCH


def test_resolver_rejects_unknown_material() -> None:
    request = learning_request(materials=(_material(),))
    result = resolve_materials(request, {})
    assert result.code is MaterialResolutionCode.UNKNOWN_MATERIAL


# -- service integration: provider never called on a material failure ------
def test_cross_project_material_refused_before_provider(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok()})
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, http).create_session(
        learning_request(materials=(_material(),)), contract=contract(), grant=grant(),
        classification=DataClassification.INTERNAL,
        material_registrations={"material:cad-primer": _registration(project_id="proj-other")})
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == "MATERIAL_CROSS_PROJECT"
    assert ("POST", "/api/generate-classroom") not in http.calls


def test_same_project_material_is_allowed(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok()})
    store = LoopStore(tmp_path / "loop.json")
    receipt = _service(store, http).create_session(
        learning_request(materials=(_material(),)), contract=contract(), grant=grant(),
        classification=DataClassification.INTERNAL,
        material_registrations={"material:cad-primer": _registration()})
    assert receipt.result is OperationResult.RECORDED
    assert ("POST", "/api/generate-classroom") in http.calls
