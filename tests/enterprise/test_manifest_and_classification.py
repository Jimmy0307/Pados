import pytest
from enterprise_support import manifest
from pydantic import ValidationError

from pados.enterprise.classification import DataClassification, lowest, parse_classification
from pados.enterprise.manifest import ArtifactContract, CapabilityType
from pados.enterprise.registry import CapabilityRegistry


def test_every_required_capability_type_has_machine_readable_manifest():
    assert {t.value for t in CapabilityType} == {
        "KNOWLEDGE_RETRIEVAL", "OCR", "EMBEDDING", "RERANK", "DOCUMENT_EXTRACTION",
        "CLASSIFICATION", "LOCAL_INFERENCE", "ENTERPRISE_TOOL", "MCP_TOOL"}
    for capability_type in CapabilityType:
        item = manifest(f"cap.{capability_type.value.lower()}", capability_type)
        dumped = item.model_dump(mode="json")
        for key in ("capability_id", "capability_type", "version", "supported_inputs", "supported_outputs",
                    "privacy_classes", "required_permissions", "execution_mode", "health",
                    "provenance_support", "artifact_contract"):
            assert key in dumped
        assert item.artifact_contract.semantic_authority == "NONE"


def test_manifest_declares_maximum_classification_and_rejects_semantic_authority():
    item = manifest(privacy_classes=(DataClassification.PUBLIC, DataClassification.INTERNAL))
    assert item.max_classification is DataClassification.INTERNAL
    with pytest.raises(ValidationError, match="SEMANTIC_TRUTH"):
        ArtifactContract(output_kind="x", schema_ref="y", semantic_authority="PADOS_SEMANTIC_STATE")
    with pytest.raises(ValidationError, match="REQUIRES_PROVENANCE"):
        manifest(provenance_support=False)
    with pytest.raises(ValidationError):
        manifest(capability_id="Bad Id")


def test_manifest_rejects_inline_secrets_and_accepts_only_secret_refs():
    assert manifest(secret_refs=("secretref://env/RERANK_TOKEN",)).secret_refs
    with pytest.raises(ValidationError, match="SECRET_REFERENCE_REQUIRED"):
        manifest(secret_refs=("plain-token-value",))
    with pytest.raises(ValidationError, match="SECRET_MATERIAL"):
        manifest(version="sk" + "-" + "abcdefghijklmnop1234")


def test_classification_vocabulary_is_ordered_and_compatible_with_existing_pados_value():
    assert parse_classification("internal") is DataClassification.INTERNAL  # PADOS default value
    assert DataClassification.CONFIDENTIAL.permits(DataClassification.INTERNAL)
    assert not DataClassification.INTERNAL.permits(DataClassification.RESTRICTED)
    assert lowest(DataClassification.RESTRICTED, DataClassification.PUBLIC) is DataClassification.PUBLIC
    for bad in ("", None, "SECRET", 3):
        with pytest.raises(ValueError):
            parse_classification(bad)


def test_registry_reports_installed_capabilities_without_authorizing_them():
    registry = CapabilityRegistry()
    registry.register(manifest())
    assert registry.health("knowledge.local").value == "UNAVAILABLE"  # no implementation
    with pytest.raises(ValueError, match="ALREADY_REGISTERED"):
        registry.register(manifest())
    with pytest.raises(PermissionError, match="NOT_INSTALLED"):
        registry.get("missing")
