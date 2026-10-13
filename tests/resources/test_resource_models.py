"""Model runtime profiles: canonical ids, explicit memory per runtime path, catalog and manifest adapter."""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from resource_fixtures import (
    ALL_MODELS,
    EMBED_SMALL,
    OPEN_LICENSE,
    OVERLAY_ENDPOINT,
    RT,
    TEXT_7B,
    manifest,
    model,
    node,
)

from pados.capability.model import Distribution, LicenseInfo, Modality
from pados.compute_node import manifest as compute_manifest
from pados.compute_node.manifest import ModelAvailability, NodeCapability
from pados.connectivity.requirements import NetworkRequirement
from pados.lifecycle.minimization import DataMinimizationViolation
from pados.resources.models import (
    MODEL_CATALOG_SCHEMA,
    MODEL_ID_PATTERN,
    MemoryRequirement,
    ModelCatalog,
    ModelRole,
    profile_from_manifest,
    profiles_from_node,
)


def test_model_id_rule_matches_compute_node_manifest() -> None:
    assert MODEL_ID_PATTERN == compute_manifest.MODEL_ID_PATTERN


@pytest.mark.parametrize("model_id", [
    "https://models.example/x@v1", "llama-cpp/../../weights@v1", "llama-cpp//abs@v1", "llama-cpp/example",
    "Llama-cpp/example@v1", "llama-cpp/example@", "llama-cpp/ex ample@v1", "llama-cpp/a:/b@v1",
    "llama-cpp/" + "x" * 161 + "@v1",
])
def test_non_canonical_or_path_like_model_ids_refused(model_id: str) -> None:
    with pytest.raises(ValidationError, match="MODEL_ID_NOT_CANONICAL"):
        model(model_id=model_id)


def test_ollama_style_ids_are_canonical() -> None:
    assert model(model_id="ollama/library/example:7b@sha256-0123456789ab", backend="ollama").backend == "ollama"


@pytest.mark.parametrize(("overrides", "code"), [
    (dict(backend="ollama"), "MODEL_ID_BACKEND_MISMATCH"),
    (dict(ram_mb=0), "MODEL_RAM_REQUIREMENT_REQUIRED"),
    (dict(unified_memory_mb=0), "MODEL_UNIFIED_MEMORY_REQUIREMENT_REQUIRED"),
    (dict(vram_mb=0), "MODEL_VRAM_REQUIREMENT_REQUIRED"),
    (dict(runtimes=(RT.NPU,), ram_mb=0), "MODEL_RAM_REQUIREMENT_REQUIRED"),
    (dict(runtimes=(RT.CPU, RT.CPU)), "MODEL_RUNTIME_DUPLICATE"),
    (dict(roles=(ModelRole.TEXT, ModelRole.TEXT)), "MODEL_ROLE_DUPLICATE"),
    (dict(modalities=(Modality.TEXT, Modality.TEXT)), "MODEL_MODALITY_DUPLICATE"),
    (dict(family="see http://x"), "MODEL_FAMILY_INVALID"),
    (dict(family="a..b"), "MODEL_FAMILY_INVALID"),
    (dict(quantization="q4; rm"), "MODEL_QUANTIZATION_INVALID"),
    (dict(quantization="../q4"), "MODEL_QUANTIZATION_INVALID"),
    (dict(backend="Bad Backend", model_id="llama-cpp/x@v1"), "MODEL_BACKEND_INVALID"),
    (dict(license=LicenseInfo(spdx="LicenseRef-X", distribution=Distribution.EXTERNAL_SERVICE)),
     "EXTERNAL_SERVICE_MUST_DECLARE_NETWORK"),
    (dict(schema_version="pados.resources.model-runtime.v0"), "MODEL_PROFILE_SCHEMA_UNSUPPORTED"),
])
def test_invalid_profiles_fail_closed(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(ValidationError, match=code):
        model(**overrides)


@pytest.mark.parametrize("field", ["endpoint", "download_url", "weights_path", "command", "authorized"])
def test_profiles_carry_no_locations_commands_or_authority(field: str) -> None:
    with pytest.raises(ValidationError, match="extra"):
        model(**{field: "x"})


def test_license_is_required_and_cpu_gpu_flags_derive_from_runtimes() -> None:
    values = TEXT_7B.model_dump()
    values.pop("license")
    with pytest.raises(ValidationError, match="license"):
        type(TEXT_7B)(**values)
    assert TEXT_7B.cpu_supported and TEXT_7B.gpu_supported
    assert EMBED_SMALL.cpu_supported and not EMBED_SMALL.gpu_supported
    assert model(runtimes=(RT.CUDA,)).cpu_supported is False


def test_catalog_is_immutable_sorted_and_rejects_duplicates() -> None:
    catalog = ModelCatalog(reversed(ALL_MODELS))
    assert [item.model_id for item in catalog] == sorted(item.model_id for item in ALL_MODELS)
    assert len(catalog) == len(ALL_MODELS)
    with pytest.raises(ValueError, match="MODEL_PROFILE_DUPLICATE"):
        catalog.register(TEXT_7B)
    empty = ModelCatalog()
    bigger = empty.register(TEXT_7B)
    assert len(empty) == 0 and bigger.get(TEXT_7B.model_id) == TEXT_7B
    with pytest.raises(KeyError, match="MODEL_PROFILE_NOT_REGISTERED"):
        empty.get(TEXT_7B.model_id)
    with pytest.raises(TypeError):
        ModelCatalog([TEXT_7B.model_dump()])  # type: ignore[list-item]


def test_catalog_document_round_trip_and_fail_closed() -> None:
    document = {"schema_version": MODEL_CATALOG_SCHEMA, "models": [TEXT_7B.model_dump(mode="json")]}
    assert ModelCatalog.from_document(document).get(TEXT_7B.model_id) == TEXT_7B
    with pytest.raises(ValueError, match="MODEL_CATALOG_SCHEMA_UNSUPPORTED"):
        ModelCatalog.from_document({"schema_version": "x", "models": []})
    with pytest.raises(DataMinimizationViolation, match="FIELD_NOT_IN_SCHEMA"):
        ModelCatalog.from_document({**document, "endpoint": "x"})
    leaked = dict(TEXT_7B.model_dump(mode="json"), family="pass" + "word=hunter2")
    with pytest.raises((DataMinimizationViolation, ValidationError)):
        ModelCatalog.from_document({"schema_version": MODEL_CATALOG_SCHEMA, "models": [leaked]})
    with pytest.raises(ValueError, match="MODEL_CATALOG_MODELS_INVALID"):
        ModelCatalog.from_document({"schema_version": MODEL_CATALOG_SCHEMA, "models": {}})


SIZED = MemoryRequirement(runtimes=(RT.CUDA, RT.CPU), ram_mb=8192, vram_mb=6144, license=OPEN_LICENSE)


def test_profile_from_manifest_translates_without_guessing() -> None:
    item = manifest()
    profile = profile_from_manifest(item, SIZED, network_requirement=NetworkRequirement.LOCAL_ONLY)
    assert profile.model_id == item.model_id and profile.backend == "ollama"
    assert profile.roles == (ModelRole.TEXT,) and profile.modalities == (Modality.TEXT,)
    assert profile.runtimes == (RT.CPU, RT.CUDA)  # canonical runtime order
    assert profile.context_length == 8192 and profile.max_classification is item.privacy_class
    embed = manifest(tasks=(NodeCapability.EMBEDDING, NodeCapability.RERANK))
    roles = profile_from_manifest(embed, SIZED, network_requirement=NetworkRequirement.LOCAL_ONLY).roles
    assert roles == (ModelRole.EMBEDDING, ModelRole.RERANKER)


def test_profiles_from_node_explains_every_skip() -> None:
    models = (manifest(), manifest(model_id="ollama/missing:1b@v1", backend_model="missing:1b",
                                   availability=ModelAvailability.MISSING),
              manifest(model_id="ollama/unsized:3b@v1", backend_model="unsized:3b"),
              manifest(model_id="ollama/bad:3b@v1", backend_model="bad:3b"))
    bad = MemoryRequirement(runtimes=(RT.CUDA,), license=OPEN_LICENSE)  # no VRAM figure -> invalid profile
    result = profiles_from_node(node(models=models), {"ollama/example-text:7b@v1": SIZED, "ollama/bad:3b@v1": bad,
                                                      "ollama/missing:1b@v1": SIZED})
    assert [item.model_id for item in result.profiles] == ["ollama/example-text:7b@v1"]
    assert result.profiles[0].network_requirement is NetworkRequirement.LOCAL_ONLY
    assert dict(result.skipped) == {"ollama/bad:3b@v1": "MODEL_PROFILE_INVALID",
                                    "ollama/missing:1b@v1": "MODEL_NOT_AVAILABLE:MISSING",
                                    "ollama/unsized:3b@v1": "MODEL_MEMORY_REQUIREMENT_UNDECLARED"}


def test_profiles_from_private_node_require_private_network() -> None:
    result = profiles_from_node(node(endpoint=OVERLAY_ENDPOINT), {"ollama/example-text:7b@v1": SIZED})
    assert result.profiles[0].network_requirement is NetworkRequirement.PRIVATE_NETWORK
