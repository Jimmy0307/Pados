"""Storage Flexibility V1+: optional, additive model artifact location in the ModelCatalog (provenance only)."""
from __future__ import annotations

import json

import pytest
from capability_profile_support import LICENSE, make_profile
from pydantic import ValidationError
from resource_fixtures import ALL_MODELS, TEXT_7B

from pados.resources.models import (
    ArtifactManager,
    ModelArtifactLocation,
    ModelCatalog,
    profile_from_capability,
)

# Digests computed with the catalog code of the base commit (before locations existed). An absent location must
# keep every existing document, catalog digest and profile digest byte-for-byte identical.
GOLDEN_FIXTURE_CATALOG_DIGEST = "4ee0740d8184a46610239070bb711689d7162be35bfd0576e543dc686f7cd5d4"
GOLDEN_MEASURED_CATALOG_DIGEST = "4b9cfff0bf75b26a122df6343664550cedce3311fcf7321d92bbef3dc870da46"
GOLDEN_PROFILE_DIGESTS = ["3319176c39681732147d48ed54abea88ea1e75238a59e324d2386c4d85774dfb",
                          "cbc9fb0259747b20f31326a13961b1718d9bdf0b4a388365516a0ca3e3944b09",
                          "eb5212244ccae1ea4d75d12e99ad87a0767d88e9eb800e3da9a7f9aec5d41be3",
                          "b8e5e52eb701bb2b7d93c38b4e599c4719a3e589f9cccc63adc3a29908304e17"]


def measured_catalog() -> ModelCatalog:
    profile = make_profile()
    return ModelCatalog([profile_from_capability(profile, license=LICENSE)]).register_capability_profile(profile)


def location(model_id: str = TEXT_7B.model_id, **values: object) -> ModelArtifactLocation:
    return ModelArtifactLocation(model_id=model_id, backend_id="runtime-store", **values)  # type: ignore[arg-type]


def test_existing_catalog_documents_and_digests_are_unchanged() -> None:
    fixture = ModelCatalog(ALL_MODELS)
    assert fixture.catalog_digest == GOLDEN_FIXTURE_CATALOG_DIGEST
    assert [m.profile_digest for m in fixture] == GOLDEN_PROFILE_DIGESTS
    measured = measured_catalog()
    assert measured.catalog_digest == GOLDEN_MEASURED_CATALOG_DIGEST
    for catalog in (fixture, measured):
        document = json.loads(json.dumps(catalog.to_document()))
        assert "artifact_locations" not in document
        restored = ModelCatalog.from_document(document)
        assert restored.catalog_digest == catalog.catalog_digest and restored.to_document() == document
        assert [p.profile_id for p in restored.capability_profiles()] == \
            [p.profile_id for p in catalog.capability_profiles()]


def test_location_is_additive_provenance_that_round_trips() -> None:
    base = ModelCatalog(ALL_MODELS)
    located = base.locate(location(label="blobs/model-x", content_digest="b" * 64))
    assert located.artifact_location(TEXT_7B.model_id) == location(label="blobs/model-x", content_digest="b" * 64)
    assert located.artifact_location(ALL_MODELS[1].model_id) is None
    assert located.get(TEXT_7B.model_id).profile_digest == TEXT_7B.profile_digest  # description identity unchanged
    assert located.catalog_digest != base.catalog_digest  # the document now carries the location
    restored = ModelCatalog.from_document(json.loads(json.dumps(located.to_document())))
    assert restored.catalog_digest == located.catalog_digest
    assert restored.artifact_locations() == located.artifact_locations()
    moved = located.locate(location(label="other-disk/model-x"))  # backend replaced: identity unchanged
    assert len(moved.artifact_locations()) == 1 and moved.get(TEXT_7B.model_id) == TEXT_7B
    assert moved.register(ALL_MODELS[0].model_copy(update={"model_id": "llama-cpp/example-text-9b@v1"})) \
        .artifact_location(TEXT_7B.model_id) == moved.artifact_location(TEXT_7B.model_id)  # carried through
    assert ModelCatalog(ALL_MODELS[1:], (), ()).register(TEXT_7B).locate(location()).artifact_location(
        TEXT_7B.model_id) is not None
    assert location().managed_by is ArtifactManager.RUNTIME


@pytest.mark.parametrize("label", ["https-like://host/x", "bucket/key?X-Sig=abc", "user:pw@host/x", "../escape",
                                   "/absolute/path", "C:/models/x", "a//b", "trailing/", "with space", "x" * 200])
def test_location_is_never_a_url_path_or_credential(label: str) -> None:
    with pytest.raises(ValidationError, match="MODEL_ARTIFACT_LOCATION_NOT_A_SAFE_LABEL"):
        location(label=label)


_DOTTED = ".".join  # synthetic hosts and addresses are assembled, so no literal sits in the source


@pytest.mark.parametrize("label", [
    _DOTTED(["models", "corp", "example", "com"]) + "/blobs/model-x",  # internal FQDN + path
    _DOTTED(["models", "corp", "example", "com"]),  # a bare FQDN
    _DOTTED(["registry", "internal"]) + "/model-x",  # host + path
    "blobs/" + _DOTTED(["10", "0", "0", "5"]),  # an IPv4 address anywhere
    _DOTTED(["192", "168", "1", "20"]) + "/models/x",  # private IPv4 + path
    "localhost/models/x",
    "home/alice/models/x",  # home-directory-like relative paths
    "Users/alice/models/x",
    "blobs/" + "ghp_" + "A1b2" * 9,  # token-shaped
    "keys/" + "AK" + "IA" + "ABCDEFGHIJ234567",  # cloud-access-key-shaped
    "blobs/" + "sk-" + "a1B2c3D4e5F6g7H8",
    "blobs/" + "xox" + "b-123456",
    "blobs/" + "eyJ" + "hbGciOiJIUzI1NiJ9",  # a JWT header
    "blobs/" + "Zx9" * 9,  # high-entropy mixed-case-and-digit token
])
def test_location_never_names_a_host_address_home_or_credential(label: str) -> None:
    """Red-team F-6: these were accepted before and could put infrastructure identifiers or secrets into a
    digest-pinned, shareable catalog document."""
    with pytest.raises(ValidationError, match="MODEL_ARTIFACT_LOCATION_NOT_A_SAFE_LABEL"):
        location(label=label)


@pytest.mark.parametrize("label", ["blobs/model-x", "other-disk/model-x", "b", "model-x.gguf",
                                   "blobs/model.Q4_K_M.gguf", "store/sha256-" + "a" * 64, "v1/text-7b+q4"])
def test_simple_opaque_labels_stay_valid(label: str) -> None:
    assert location(label=label).label == label


def test_location_fields_fail_closed() -> None:
    with pytest.raises(ValidationError, match="MODEL_ARTIFACT_BACKEND_INVALID"):
        ModelArtifactLocation(model_id=TEXT_7B.model_id, backend_id="s3://bucket")
    with pytest.raises(ValidationError, match="MODEL_ARTIFACT_DIGEST_INVALID"):
        location(content_digest="sha256:abc")
    with pytest.raises(ValidationError, match="MODEL_ID_NOT_CANONICAL"):
        location(model_id="not canonical")
    with pytest.raises(ValidationError):
        location(managed_by="PADOS")  # PADOS never manages model weights
    with pytest.raises(ValidationError):
        ModelArtifactLocation(model_id=TEXT_7B.model_id, backend_id="x", path="/tmp/w")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="MODEL_ARTIFACT_LOCATION_MODEL_NOT_REGISTERED"):
        ModelCatalog(ALL_MODELS).locate(location(model_id="llama-cpp/unknown@v1"))
    with pytest.raises(ValueError, match="MODEL_ARTIFACT_LOCATION_DUPLICATE"):
        ModelCatalog(ALL_MODELS, (), (location(), location(label="b")))
    with pytest.raises(TypeError, match="MODEL_ARTIFACT_LOCATION_TYPE_INVALID"):
        ModelCatalog(ALL_MODELS, (), ({"model_id": TEXT_7B.model_id},))  # type: ignore[arg-type]
    document = ModelCatalog(ALL_MODELS).to_document()
    with pytest.raises(ValueError, match="MODEL_CATALOG_ARTIFACT_LOCATIONS_INVALID"):
        ModelCatalog.from_document({**document, "artifact_locations": {"x": 1}})
    forged = {**document, "artifact_locations": [{**location().model_dump(mode="json"), "label": "a?b=c"}]}
    with pytest.raises(ValueError):
        ModelCatalog.from_document(forged)
