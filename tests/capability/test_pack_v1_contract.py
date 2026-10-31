# SPDX-License-Identifier: AGPL-3.0-only
"""Capability Pack V1: a versioned extension contract, never an authority.

installed != registered != authorized != selected != accepted -- a pack changes none of that. It declares
existing descriptors plus identity, provenance, runtime, security, evidence, verification, lifecycle, failure and
removal metadata; the manifest is validated fail-closed, registered only through the existing operator registry,
executed only through the existing executor, and its outputs stay unaccepted candidates.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fabric_support import OPERATOR, fabric, template

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ScopedArtifacts
from pados.capability.executor import ExecutionStatus
from pados.capability.planner import PlanStatus
from pados.capability.task import TaskInput
from pados.capability.model import LifecycleState, descriptor_digest
from pados.capability.pack import (
    PACK_SCHEMA,
    GovernedPackAdapter,
    PackIndex,
    PackManifestError,
    disable_pack,
    outputs_digest,
    propose_pack_transition,
    register_pack,
    validate_pack,
)
from pados.capability.receipts import VerificationStatus
from pados.capability_loop.contracts import CapabilityState
from pados.capability_loop.lifecycle import LifecycleError
from pados.enterprise.classification import DataClassification
from pados.packs.reference import pack as reference

DIGEST = "d" * 64


def _manifest_data(**overrides: Any) -> dict[str, Any]:
    data = reference.MANIFEST.model_dump(mode="json")
    data.update(overrides)
    return data


# -- contract ----------------------------------------------------------------------------------------------
def test_reference_manifest_is_valid_and_content_addressed() -> None:
    manifest = validate_pack(reference.MANIFEST.model_dump(mode="json"))
    assert manifest == reference.MANIFEST
    assert manifest.schema_version == PACK_SCHEMA
    assert manifest.manifest_digest == reference.MANIFEST.manifest_digest
    assert manifest.capability_ids == (reference.CAPABILITY_ID,)
    assert manifest.required_grants == (f"CAPABILITY:{reference.CAPABILITY_ID}",)
    assert manifest.semantic_authority == "NONE"


def test_manifest_digest_changes_with_any_field() -> None:
    changed = validate_pack(_manifest_data(version="2"))
    assert changed.manifest_digest != reference.MANIFEST.manifest_digest


def _mutate(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    data = _manifest_data()
    target = data
    for key in path[:-1]:
        target = target[key] if not isinstance(target, list) else target[int(key)]
    target[path[-1] if not isinstance(target, list) else int(path[-1])] = value
    return data


INVALID: dict[str, tuple[dict[str, Any], str]] = {
    "schema": (_manifest_data(schema_version="pados.capability.pack.v9"), "PACK_SCHEMA_UNSUPPORTED"),
    "pack_id": (_manifest_data(pack_id="Bad Pack!"), "PACK_ID_INVALID"),
    "undeclared_adapter_capability": (_mutate(("adapters", "0", "capability_id"), "OTHER_CAPABILITY"),
                                      "PACK_ADAPTER_CAPABILITY_UNDECLARED"),
    "duplicate_capability": (_manifest_data(capabilities=[reference.MANIFEST.capabilities[0].model_dump(
        mode="json")] * 2), "PACK_DUPLICATE_ID"),
    "no_capabilities": (_manifest_data(capabilities=[], adapters=[]), "PACK_EMPTY"),
    "capability_ids_mismatch": (_manifest_data(capability_ids=["SOMETHING_ELSE"]), "PACK_CAPABILITY_IDS_MISMATCH"),
    "classification_ceiling_above_descriptor": (_mutate(("security", "classification_ceiling"), "RESTRICTED"),
                                                "PACK_CLASSIFICATION_CEILING_EXCEEDS_DESCRIPTOR"),
    "egress_contradicts_network": (_mutate(("security", "egress"), "ALLOWLIST"), "PACK_EGRESS_INCONSISTENT"),
    "secret_value_instead_of_ref": (_mutate(("security", "secret_refs"), ["plain-value-not-a-secretref"]),
                                    "PACK_SECRET_REF_INVALID"),
    "missing_core_evidence": (_manifest_data(evidence_requirements=["runtime_version"]),
                              "PACK_EVIDENCE_REQUIREMENTS_INCOMPLETE"),
    "self_declared_validated": (_manifest_data(initial_state="VALIDATED"), "PACK_INITIAL_STATE_INVALID"),
    "active_descriptor_in_unvalidated_pack": (_mutate(("capabilities", "0", "lifecycle"), "ACTIVE"),
                                              "PACK_LIFECYCLE_CLAIMS_ACTIVE"),
    "third_party_without_source_digest": (_mutate(("provenance", "source"), "THIRD_PARTY"),
                                          "PACK_PROVENANCE_DIGEST_REQUIRED"),
    "failure_code_invalid": (_manifest_data(failure_codes=[{"code": "bad code", "retryable": False}]),
                             "PACK_FAILURE_CODE_INVALID"),
    "timeout_unbounded": (_mutate(("runtime", "timeout_seconds"), 100_000), "PACK_RUNTIME_INVALID"),
    "config_digest_invalid": (_mutate(("runtime", "execution_config_digest"), "not-a-digest"),
                              "PACK_RUNTIME_INVALID"),
    "verification_hook_for_unknown_contract": (_manifest_data(verification_hooks=[
        {"output_contract": "unknown.contract/v1", "checker_id": "x.checker"}]), "PACK_VERIFICATION_HOOK_UNKNOWN"),
    "extra_field": (_manifest_data(grants_itself=True), "PACK_MANIFEST_INVALID"),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_manifests_fail_closed_with_a_closed_code(name: str) -> None:
    data, code = INVALID[name]
    with pytest.raises(PackManifestError) as excinfo:
        validate_pack(data)
    assert excinfo.value.code == code


def test_manifest_cannot_carry_authority() -> None:
    with pytest.raises(PackManifestError):
        validate_pack(_manifest_data(semantic_authority="GRANTED"))


# -- registration: through the existing operator registry; installed/registered != authorized ---------------
def test_register_pack_uses_the_existing_registry_and_grants_nothing(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    registration = register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()},
                                 operator=OPERATOR)
    assert registration.manifest_digest == reference.MANIFEST.manifest_digest
    assert [a.adapter_id for a in fab.registry.adapters_for(reference.CAPABILITY_ID)] == [reference.ADAPTER_ID]
    assert fab.registry.capability(reference.CAPABILITY_ID).lifecycle is LifecycleState.EXPERIMENTAL
    assert ("proj-a", "wc-1") in fab.grants.grants
    assert f"CAPABILITY:{reference.CAPABILITY_ID}" not in str(fab.grants.grants)  # registration granted nothing


def test_register_pack_refuses_an_implementation_that_is_not_the_declared_adapter(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    other = reference.adapter()
    other._descriptor = other.descriptor.model_copy(update={"version": "99"})
    with pytest.raises(PackManifestError, match="PACK_IMPLEMENTATION_MISMATCH"):
        register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: other}, operator=OPERATOR)
    with pytest.raises(PackManifestError, match="PACK_IMPLEMENTATION_MISSING"):
        register_pack(fab.registry, reference.MANIFEST, {}, operator=OPERATOR)
    assert fab.registry.adapters_for("DOCUMENT_OCR")  # nothing else disturbed


def test_a_discovered_pack_is_not_registrable(tmp_path: Path) -> None:
    discovered = validate_pack(_manifest_data(initial_state="DISCOVERED"))
    with pytest.raises(PackManifestError, match="PACK_DISCOVERED_NOT_REGISTRABLE"):
        register_pack(fabric(tmp_path).registry, discovered, {reference.ADAPTER_ID: reference.adapter()},
                      operator=OPERATOR)


# -- execution: only through the existing executor; outputs stay unaccepted ---------------------------------
def _run_reference(tmp_path: Path, *, granted: bool = True) -> Any:
    fab = fabric(tmp_path)
    register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()}, operator=OPERATOR)
    fab.schemas.register(reference.OUTPUT_CONTRACT, reference.CHECKER_ID, reference.digest_output_valid)
    fab.templates.register(template("reference.digest", [
        {"stage_id": "digest", "capability_id": reference.CAPABILITY_ID,
         "inputs": [{"source": "TASK_INPUT", "name": "scan"}]}]))
    if granted:
        fab.grants.grant("DOCUMENT_OCR", "TEXT_CHUNK", reference.CAPABILITY_ID)
    source = fab.artifact(b"line one\nline two\n", DataClassification.INTERNAL)
    task = fab.task(task_type="reference.digest", classification=DataClassification.INTERNAL,
                    required_outputs=(reference.OUTPUT_CONTRACT,),
                    inputs=(TaskInput(name="scan", ref=source, project_id="proj-a",
                                      classification=DataClassification.INTERNAL),))
    return fab, task


def test_reference_pack_runs_only_through_the_existing_executor(tmp_path: Path) -> None:
    fab, task = _run_reference(tmp_path)
    plan = fab.planner().plan(task)
    result = fab.executor().execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED, result.failure_code
    (stage,) = result.plan_receipt.stages
    assert stage.verification is VerificationStatus.PASSED
    assert result.semantic_authority == "NONE" and result.plan_receipt.semantic_authority == "NONE"
    index = PackIndex()
    index.add(reference.MANIFEST)
    (attribution,) = index.attributions(stage.capability_digest, stage.adapter_digest)
    assert (attribution.pack_id, attribution.version) == (reference.PACK_ID, reference.MANIFEST.version)
    assert attribution.manifest_digest == reference.MANIFEST.manifest_digest


def test_without_a_grant_the_registered_pack_is_not_planned(tmp_path: Path) -> None:
    fab, task = _run_reference(tmp_path, granted=False)
    plan = fab.planner().plan(task)
    assert plan.status is PlanStatus.INFEASIBLE  # registered != authorized


# -- the governed adapter boundary: evidence + closed failure vocabulary -------------------------------------
class _Store:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, payload: bytes, *, media_type: str, kind: str) -> Any:
        from pados.capability.artifacts import ArtifactRef
        import hashlib
        ref = ArtifactRef(sha256=hashlib.sha256(payload).hexdigest(), media_type=media_type,
                          size_bytes=len(payload), kind=kind)
        self.blobs[ref.sha256] = payload
        return ref

    def get(self, ref: Any) -> bytes:
        return self.blobs[ref.sha256]


def _call(inner: Any) -> StageOutput:
    store = _Store()
    source = store.put(b"line one\nline two\n", media_type="text/plain", kind="doc.scan")
    context = StageContext(artifacts=ScopedArtifacts(store, readable=(source,)), project_id="proj-a",
                           stage_id="digest")
    request = StageRequest(stage_id="digest", capability_id=reference.CAPABILITY_ID, inputs=(source,),
                           parameters={}, classification=DataClassification.INTERNAL)
    return GovernedPackAdapter(reference.MANIFEST, inner).execute(request, context=context)


class _Wrapped:
    def __init__(self, transform: Any = None, raises: BaseException | None = None) -> None:
        self._inner = reference.adapter()
        self.descriptor = self._inner.descriptor
        self._transform, self._raises = transform, raises

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        if self._raises is not None:
            raise self._raises
        output = self._inner.execute(request, context=context)
        return output if self._transform is None else self._transform(output)


def test_governed_adapter_passes_a_complete_output_through() -> None:
    output = _call(_Wrapped())
    assert output.evidence["runtime_version"] == reference.MANIFEST.provider.runtime_version
    assert output.evidence["artifact_digest"] == outputs_digest(output.outputs)


@pytest.mark.parametrize(("change", "code"), [
    ({"runtime_version": None}, "PACK_EVIDENCE_MISSING"),
    ({"config_digest": DIGEST}, "PACK_EVIDENCE_MISMATCH"),
    ({"runtime_version": "9.9.9"}, "PACK_EVIDENCE_MISMATCH"),
    ({"provider_id": "someone.else"}, "PACK_EVIDENCE_MISMATCH"),
    ({"artifact_digest": DIGEST}, "PACK_EVIDENCE_MISMATCH"),
])
def test_governed_adapter_refuses_missing_or_contradicting_evidence(change: dict[str, Any], code: str) -> None:
    def tamper(output: StageOutput) -> StageOutput:
        evidence = {k: v for k, v in output.evidence.items() if not (k in change and change[k] is None)}
        evidence.update({k: v for k, v in change.items() if v is not None})
        return StageOutput(outputs=output.outputs, metrics=output.metrics, evidence=evidence)

    with pytest.raises(CapabilityAdapterError) as excinfo:
        _call(_Wrapped(tamper))
    assert excinfo.value.code == code


def test_declared_failure_codes_keep_their_retryability_and_others_are_closed() -> None:
    with pytest.raises(CapabilityAdapterError) as declared:
        _call(_Wrapped(raises=CapabilityAdapterError("REFERENCE_INPUT_EMPTY")))
    assert declared.value.code == "REFERENCE_INPUT_EMPTY" and declared.value.retryable is False
    with pytest.raises(CapabilityAdapterError) as undeclared:
        _call(_Wrapped(raises=CapabilityAdapterError("SECRET_TOKEN_abc123")))
    assert undeclared.value.code == "PACK_ADAPTER_FAILED"
    with pytest.raises(CapabilityAdapterError) as unexpected:
        _call(_Wrapped(raises=RuntimeError("customer document text leaks here")))
    assert unexpected.value.code == "PACK_ADAPTER_FAILED"
    assert unexpected.value.__cause__ is None and unexpected.value.__suppress_context__


# -- lifecycle: existing capability-loop transitions; nothing auto-promotes ----------------------------------
def test_pack_transitions_are_inert_proposals_and_validated_needs_an_outcome() -> None:
    proposals = propose_pack_transition(reference.MANIFEST, current=CapabilityState.EXPERIMENTAL,
                                        target=CapabilityState.VALIDATION_REQUIRED, reason_code="PACK_TRIAL_DONE")
    assert [p.capability_id for p in proposals] == [reference.CAPABILITY_ID]
    assert all(p.auto_apply is False and p.semantic_authority == "NONE" for p in proposals)
    with pytest.raises(LifecycleError, match="VALIDATION_OUTCOME_REQUIRED"):
        propose_pack_transition(reference.MANIFEST, current=CapabilityState.VALIDATION_REQUIRED,
                                target=CapabilityState.VALIDATED, reason_code="PACK_VALIDATED")
    with pytest.raises(LifecycleError, match="TRANSITION_NOT_ALLOWED"):
        propose_pack_transition(reference.MANIFEST, current=CapabilityState.EXPERIMENTAL,
                                target=CapabilityState.VALIDATED, reason_code="SKIP", validation_outcome_ref=DIGEST)


def test_disable_revokes_the_packs_adapters_and_keeps_history(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()}, operator=OPERATOR)
    disable_pack(fab.registry, reference.MANIFEST, operator=OPERATOR, reason_code="PACK_DISABLED")
    assert fab.registry.adapter(reference.ADAPTER_ID).descriptor.lifecycle is LifecycleState.REVOKED
    assert any(event.reason_code == "PACK_DISABLED" for event in fab.registry.events)


# -- rollback: every version stays attributable --------------------------------------------------------------
def test_index_keeps_versions_and_refuses_a_conflicting_redefinition() -> None:
    index = PackIndex()
    index.add(reference.MANIFEST)
    v2 = validate_pack(_manifest_data(version="2"))
    index.add(v2)
    index.add(reference.MANIFEST)  # idempotent
    assert index.versions(reference.PACK_ID) == ("1", "2")
    conflicting = validate_pack(_mutate(("runtime", "timeout_seconds"), 5))
    with pytest.raises(PackManifestError, match="PACK_VERSION_CONFLICT"):
        index.add(conflicting)
    capability = reference.MANIFEST.capabilities[0]
    adapter = reference.MANIFEST.adapters[0]
    # identical descriptors in two versions: attribution names every candidate, it never guesses one
    found = index.attributions(descriptor_digest(capability), descriptor_digest(adapter))
    assert {item.version for item in found} == {"1", "2"}
    assert index.attributions(DIGEST, DIGEST) == ()
