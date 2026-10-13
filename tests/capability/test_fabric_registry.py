"""Capability registry: installed != registered != authorized; explicit, logged operator lifecycle."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fabric_support import OCR, OPERATOR, TransformAdapter, adapter_descriptor, capability, fabric

from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    LifecycleState,
)
from pados.capability.registry import AdapterIntegrityError, CapabilityRegistry, RegistryAction, RegistryError


def test_installed_is_not_registered_and_registered_is_not_authorized(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    ghost = fab.add(TransformAdapter(adapter_descriptor("ocr.ghost", "DOCUMENT_OCR")), register=False)
    assert fab.registry.is_installed("ocr.ghost") and not fab.registry.is_registered("ocr.ghost")
    assert "ocr.ghost" not in [a.adapter_id for a in fab.registry.adapters_for("DOCUMENT_OCR")]
    with pytest.raises(RegistryError, match="ADAPTER_NOT_REGISTERED"):
        fab.registry.adapter("ocr.ghost")
    assert ghost.calls == []
    # The registry has no notion of authorization at all.
    for name in ("authorize", "grant", "grants", "authorized", "is_authorized"):
        assert not hasattr(fab.registry, name)
    assert not any("grant" in key or "authori" in key for key in vars(fab.registry))


def test_duplicate_and_forged_descriptors_are_refused(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    with pytest.raises(RegistryError, match="CAPABILITY_ALREADY_REGISTERED"):
        fab.registry.register_capability(OCR, operator=OPERATOR)
    forged = CapabilityDescriptor.model_construct(**{**dict(OCR), "capability_id": "ocr; rm -rf"})
    with pytest.raises(RegistryError, match="CAPABILITY_DESCRIPTOR_INVALID"):
        fab.registry.register_capability(forged, operator=OPERATOR)
    with pytest.raises(RegistryError, match="ADAPTER_ALREADY_INSTALLED"):
        fab.registry.install(TransformAdapter(adapter_descriptor("ocr.alpha", "DOCUMENT_OCR")), operator=OPERATOR)
    forged_adapter = TransformAdapter(AdapterDescriptor.model_construct(
        **{**dict(adapter_descriptor("ocr.x1", "DOCUMENT_OCR")), "adapter_id": "../evil"}))
    with pytest.raises(RegistryError, match="ADAPTER_DESCRIPTOR_INVALID"):
        fab.registry.install(forged_adapter, operator=OPERATOR)
    with pytest.raises(RegistryError, match="ADAPTER_IMPLEMENTATION_INVALID"):
        fab.registry.install(object(), operator=OPERATOR)  # type: ignore[arg-type]
    with pytest.raises(RegistryError, match="OPERATOR_IDENTITY_INVALID"):
        fab.registry.register_capability(capability("NEW_CAP"), operator="")


def test_malicious_adapter_cannot_register_under_another_capability_or_self_authorize(tmp_path: Path) -> None:
    fab = fabric(tmp_path)

    class SelfAuthorizing(TransformAdapter):
        authorized = True
        grants = ("CAPABILITY:DOCUMENT_OCR",)

        def register(self, registry: CapabilityRegistry) -> None:  # never called by the registry
            raise AssertionError("adapter code must never receive the registry")

    rogue = SelfAuthorizing(adapter_descriptor("chunk.rogue", "TEXT_CHUNK"))
    fab.registry.install(rogue, operator=OPERATOR)
    with pytest.raises(RegistryError, match="ADAPTER_CAPABILITY_MISMATCH"):
        fab.registry.register_adapter("chunk.rogue", capability_id="DOCUMENT_OCR", operator=OPERATOR)
    unknown = TransformAdapter(adapter_descriptor("x.unknown", "UNREGISTERED_CAP"))
    fab.registry.install(unknown, operator=OPERATOR)
    with pytest.raises(RegistryError, match="CAPABILITY_NOT_REGISTERED"):
        fab.registry.register_adapter("x.unknown", capability_id="UNREGISTERED_CAP", operator=OPERATOR)
    # Extra "authorization" fields cannot even exist on a descriptor.
    with pytest.raises(ValueError):
        AdapterDescriptor.model_validate({**adapter_descriptor("a.b1", "DOCUMENT_OCR").model_dump(),
                                          "authorized": True})


def test_adapter_cannot_weaken_determinism_or_claim_router_delegation(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    vlm = TransformAdapter(adapter_descriptor("ocr.vlm", "DOCUMENT_OCR", determinism=Determinism.PROBABILISTIC))
    fab.registry.install(vlm, operator=OPERATOR)
    with pytest.raises(RegistryError, match="WEAKENS_CAPABILITY_DETERMINISM"):
        fab.registry.register_adapter("ocr.vlm", capability_id="DOCUMENT_OCR", operator=OPERATOR)
    fake = TransformAdapter(adapter_descriptor("ocr.router", "DOCUMENT_OCR", delegates_to_worker_router=True))
    fab.registry.install(fake, operator=OPERATOR)
    with pytest.raises(RegistryError, match="ADAPTER_DELEGATION_INVALID"):
        fab.registry.register_adapter("ocr.router", capability_id="DOCUMENT_OCR", operator=OPERATOR)


def test_descriptor_mutation_after_install_is_detected(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    impl = fab.adapters["ocr.alpha"]
    facade = fab.registry.adapter("ocr.alpha")
    impl.descriptor = adapter_descriptor("ocr.alpha", "DOCUMENT_OCR", max_classification="PUBLIC")
    with pytest.raises(AdapterIntegrityError, match="CHANGED_SINCE_INSTALL"):
        _ = facade.descriptor
    assert facade.available() is False


def test_lifecycle_transitions_are_explicit_operator_calls_and_logged(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    before = len(fab.registry.events)
    deprecated = fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.DEPRECATED, operator=OPERATOR,
                                                    reason_code="SUPERSEDED")
    assert deprecated.lifecycle is LifecycleState.DEPRECATED
    assert fab.registry.adapter("ocr.alpha").descriptor.lifecycle is LifecycleState.DEPRECATED
    fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.REVOKED, operator=OPERATOR,
                                       reason_code="SECURITY_ADVISORY")
    with pytest.raises(RegistryError, match="LIFECYCLE_TRANSITION_NOT_ALLOWED"):
        fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.ACTIVE, operator=OPERATOR,
                                           reason_code="UNDO")
    with pytest.raises(RegistryError, match="REASON_CODE_INVALID"):
        fab.registry.set_capability_lifecycle("TEXT_CHUNK", LifecycleState.DEPRECATED, operator=OPERATOR,
                                              reason_code="free text reason")
    fab.registry.set_capability_lifecycle("TEXT_CHUNK", LifecycleState.DEPRECATED, operator=OPERATOR,
                                          reason_code="REPLACED")
    assert fab.registry.capability("TEXT_CHUNK").lifecycle is LifecycleState.DEPRECATED
    events = fab.registry.events[before:]
    assert [(e.action, e.subject_id, e.from_state, e.to_state) for e in events] == [
        (RegistryAction.LIFECYCLE, "ocr.alpha", "ACTIVE", "DEPRECATED"),
        (RegistryAction.LIFECYCLE, "ocr.alpha", "DEPRECATED", "REVOKED"),
        (RegistryAction.LIFECYCLE, "TEXT_CHUNK", "ACTIVE", "DEPRECATED"),
    ]
    assert all(e.operator == OPERATOR and len(e.digest) == 64 for e in events)
    assert [e.sequence for e in fab.registry.events] == list(range(1, len(fab.registry.events) + 1))


def test_registry_never_calls_adapter_code_during_registration(tmp_path: Path) -> None:
    fab = fabric(tmp_path)

    class Exploding(TransformAdapter):
        def available(self) -> bool:
            raise AssertionError("registration must not probe adapters")

        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("registration must not execute adapters")

    fab.add(Exploding(adapter_descriptor("ocr.boom", "DOCUMENT_OCR")))
    assert fab.registry.is_registered("ocr.boom")
    assert fab.registry.adapter("ocr.boom").available() is False  # a crashing probe is "unavailable"


def test_model_runtime_capability_is_just_another_capability(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    assert fab.registry.capability("MODEL_SUMMARY").family is CapabilityFamily.MODEL_RUNTIME
