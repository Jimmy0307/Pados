# SPDX-License-Identifier: AGPL-3.0-only
"""Capability Pack V1 governance: the manifest is enforced at runtime and registration is never stale.

* the pack's own classification ceiling and capability identity are enforced on every request, not only when the
  manifest is validated;
* a missing runtime or an unavailable provider makes the pack unplannable -- fail closed, never a crash;
* registration is rejected before the registry is touched when it would be stale (the pack's adapters already
  exist) or would silently redefine an already registered capability;
* a disabled pack is never planned; rollback is a deterministic disable plus re-registration of the previous
  version into a fresh registry, with every version's receipts still attributable;
* a pack implementation cannot emit authority and cannot get malformed output past verification.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fabric_support import OPERATOR, fabric, template

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ScopedArtifacts
from pados.capability.executor import ExecutionStatus
from pados.capability.model import LifecycleState
from pados.capability.pack import (
    GovernedPackAdapter,
    PackIndex,
    PackManifestError,
    disable_pack,
    register_pack,
    validate_pack,
)
from pados.capability.planner import PlanStatus
from pados.capability.receipts import VerificationStatus
from pados.capability.task import TaskInput
from pados.enterprise.classification import DataClassification
from pados.packs.reference import pack as reference


def _variant(**changes: Any) -> Any:
    data = reference.MANIFEST.model_dump(mode="json")
    for dotted, value in changes.items():
        target = data
        *path, last = dotted.split("__")
        for key in path:
            target = target[int(key)] if isinstance(target, list) else target[key]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value
    return validate_pack(data)


class _MemoryStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, payload: bytes, *, media_type: str, kind: str) -> Any:
        import hashlib

        from pados.capability.artifacts import ArtifactRef
        ref = ArtifactRef(sha256=hashlib.sha256(payload).hexdigest(), media_type=media_type,
                          size_bytes=len(payload), kind=kind)
        self.blobs[ref.sha256] = payload
        return ref

    def get(self, ref: Any) -> bytes:
        return self.blobs[ref.sha256]


def _direct(governed: GovernedPackAdapter, *, capability_id: str = reference.CAPABILITY_ID,
            classification: DataClassification = DataClassification.INTERNAL) -> StageOutput:
    store = _MemoryStore()
    source = store.put(b"line one\n", media_type="text/plain", kind="doc.scan")
    context = StageContext(artifacts=ScopedArtifacts(store, readable=(source,)), project_id="proj-a",
                           stage_id="digest")
    request = StageRequest(stage_id="digest", capability_id=capability_id, inputs=(source,), parameters={},
                           classification=classification)
    return governed.execute(request, context=context)


class _Impl:
    """A reference implementation whose availability, output or failure the test controls."""

    def __init__(self, *, available: Any = True, output: Any = None) -> None:
        self._inner = reference.adapter()
        self.descriptor = self._inner.descriptor
        self._available, self._output = available, output

    def available(self) -> bool:
        if isinstance(self._available, BaseException):
            raise self._available
        return bool(self._available)

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        produced = self._inner.execute(request, context=context)
        return produced if self._output is None else self._output(produced, context)


# -- runtime enforcement of the manifest -------------------------------------------------------------------
def test_request_above_the_pack_classification_ceiling_is_refused() -> None:
    narrowed = _variant(security__classification_ceiling="PUBLIC")
    with pytest.raises(CapabilityAdapterError) as excinfo:
        _direct(GovernedPackAdapter(narrowed, _Impl()), classification=DataClassification.INTERNAL)
    assert excinfo.value.code == "PACK_CLASSIFICATION_CEILING_EXCEEDED"
    assert excinfo.value.retryable is False


def test_request_for_a_capability_the_adapter_does_not_implement_is_refused() -> None:
    with pytest.raises(CapabilityAdapterError) as excinfo:
        _direct(GovernedPackAdapter(reference.MANIFEST, _Impl()), capability_id="DOCUMENT_OCR")
    assert excinfo.value.code == "PACK_CAPABILITY_MISMATCH"


@pytest.mark.parametrize("availability", [False, RuntimeError("runtime binary not found"), ConnectionError()])
def test_missing_runtime_or_unavailable_provider_is_unavailable_never_a_crash(availability: Any) -> None:
    assert GovernedPackAdapter(reference.MANIFEST, _Impl(available=availability)).available() is False


def _task(fab: Any) -> Any:
    fab.schemas.register(reference.OUTPUT_CONTRACT, reference.CHECKER_ID, reference.digest_output_valid)
    fab.templates.register(template("reference.digest", [
        {"stage_id": "digest", "capability_id": reference.CAPABILITY_ID,
         "inputs": [{"source": "TASK_INPUT", "name": "scan"}]}]))
    fab.grants.grant("DOCUMENT_OCR", "TEXT_CHUNK", reference.CAPABILITY_ID)
    source = fab.artifact(b"line one\nline two\n", DataClassification.INTERNAL)
    return fab.task(task_type="reference.digest", classification=DataClassification.INTERNAL,
                    required_outputs=(reference.OUTPUT_CONTRACT,),
                    inputs=(TaskInput(name="scan", ref=source, project_id="proj-a",
                                      classification=DataClassification.INTERNAL),))


def test_an_unavailable_provider_makes_the_pack_unplannable(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    register_pack(fab.registry, reference.MANIFEST,
                  {reference.ADAPTER_ID: _Impl(available=RuntimeError("provider down"))}, operator=OPERATOR)
    assert fab.planner().plan(_task(fab)).status is PlanStatus.INFEASIBLE


# -- registration: never stale, never a silent redefinition ------------------------------------------------
def test_registering_the_same_pack_twice_is_a_stale_registration_and_touches_nothing(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()}, operator=OPERATOR)
    before = len(fab.registry.events)
    with pytest.raises(PackManifestError) as excinfo:
        register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()},
                      operator=OPERATOR)
    assert excinfo.value.code == "PACK_STALE_REGISTRATION"
    assert len(fab.registry.events) == before


def test_a_version_that_redefines_a_registered_capability_is_refused_before_any_change(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()}, operator=OPERATOR)
    before = len(fab.registry.events)
    redefined = _variant(version="2", capabilities__0__description="A different capability contract",
                         adapters__0__adapter_id="reference.digest.v2")
    implementation = reference.adapter()
    implementation._descriptor = redefined.adapters[0]
    with pytest.raises(PackManifestError) as excinfo:
        register_pack(fab.registry, redefined, {"reference.digest.v2": implementation}, operator=OPERATOR)
    assert excinfo.value.code == "PACK_CAPABILITY_CONFLICT"
    assert len(fab.registry.events) == before


def test_a_new_version_with_new_adapters_reuses_the_identical_registered_capability(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()}, operator=OPERATOR)
    upgraded = _variant(version="2", adapters__0__adapter_id="reference.digest.v2")
    implementation = reference.adapter()
    implementation._descriptor = upgraded.adapters[0]
    disable_pack(fab.registry, reference.MANIFEST, operator=OPERATOR)
    register_pack(fab.registry, upgraded, {"reference.digest.v2": implementation}, operator=OPERATOR)
    plannable = [a.adapter_id for a in fab.registry.adapters_for(reference.CAPABILITY_ID)
                 if a.descriptor.lifecycle.plannable]
    assert plannable == ["reference.digest.v2"]


# -- disable and rollback -----------------------------------------------------------------------------------
def test_a_disabled_pack_is_never_planned_or_executed(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()}, operator=OPERATOR)
    task = _task(fab)
    assert fab.planner().plan(task).status is PlanStatus.FEASIBLE
    disable_pack(fab.registry, reference.MANIFEST, operator=OPERATOR)
    assert fab.planner().plan(task).status is PlanStatus.INFEASIBLE
    assert fab.registry.adapter(reference.ADAPTER_ID).descriptor.lifecycle is LifecycleState.REVOKED


def test_rollback_is_disable_plus_reregistration_into_a_fresh_registry(tmp_path: Path) -> None:
    upgraded = _variant(version="2", adapters__0__adapter_id="reference.digest.v2")
    v2_impl = reference.adapter()
    v2_impl._descriptor = upgraded.adapters[0]
    index = PackIndex()
    index.add(reference.MANIFEST)
    index.add(upgraded)

    current = fabric(tmp_path / "current")
    register_pack(current.registry, upgraded, {"reference.digest.v2": v2_impl}, operator=OPERATOR)
    disable_pack(current.registry, upgraded, operator=OPERATOR)
    with pytest.raises(PackManifestError):  # a disabled version is terminal in this registry
        register_pack(current.registry, upgraded, {"reference.digest.v2": v2_impl}, operator=OPERATOR)

    rolled_back = fabric(tmp_path / "rolled-back")
    register_pack(rolled_back.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()},
                  operator=OPERATOR)
    task = _task(rolled_back)
    result = rolled_back.executor().execute(rolled_back.planner().plan(task), task)
    assert result.status is ExecutionStatus.COMPLETED, result.failure_code
    (stage,) = result.plan_receipt.stages
    assert {a.version for a in index.attributions(stage.capability_digest, stage.adapter_digest)} == {"1"}


# -- authority and malformed output -------------------------------------------------------------------------
def test_an_implementation_cannot_emit_semantic_authority() -> None:
    def claim_authority(produced: StageOutput, _: StageContext) -> StageOutput:
        return StageOutput(outputs=produced.outputs, evidence=dict(produced.evidence), semantic_authority="ACCEPTED")

    with pytest.raises(CapabilityAdapterError) as excinfo:
        _direct(GovernedPackAdapter(reference.MANIFEST, _Impl(output=claim_authority)))
    assert excinfo.value.code == "PACK_ADAPTER_FAILED"


def test_malformed_output_with_complete_evidence_still_fails_verification(tmp_path: Path) -> None:
    def malformed(produced: StageOutput, context: StageContext) -> StageOutput:
        ref = context.artifacts.put(b'{"schema":"forged"}', media_type="application/json",
                                    kind=reference.OUTPUT_KIND)
        evidence = dict(produced.evidence, artifact_digest=ref.sha256)
        return StageOutput(outputs=(ref,), evidence=evidence)

    fab = fabric(tmp_path)
    register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: _Impl(output=malformed)},
                  operator=OPERATOR)
    task = _task(fab)
    result = fab.executor().execute(fab.planner().plan(task), task)
    assert result.status is ExecutionStatus.FAILED
    (stage,) = result.plan_receipt.stages
    assert stage.verification is VerificationStatus.FAILED
    assert stage.outputs == ()
    assert result.semantic_authority == "NONE"


# -- review hardening: every refusal happens before the registry is touched --------------------------------
@pytest.mark.parametrize(("changes", "code"), [
    ({"adapters__0__determinism": "PROBABILISTIC"}, "PACK_ADAPTER_WEAKENS_DETERMINISM"),
    ({"adapters__0__delegates_to_worker_router": True}, "PACK_ADAPTER_DELEGATION_INVALID"),
    ({"runtime__timeout_seconds": True}, "PACK_RUNTIME_INVALID"),
])
def test_manifests_the_registry_would_refuse_mid_registration_are_refused_at_validation(
        changes: dict[str, Any], code: str) -> None:
    with pytest.raises(PackManifestError) as excinfo:
        _variant(**changes)
    assert excinfo.value.code == code


@pytest.mark.parametrize("document", [None, ["not", "a", "mapping"], "text", 3])
def test_a_non_mapping_manifest_document_is_a_closed_refusal(document: Any) -> None:
    with pytest.raises(PackManifestError) as excinfo:
        validate_pack(document)
    assert excinfo.value.code == "PACK_MANIFEST_INVALID"


def _networked(**changes: Any) -> dict[str, Any]:
    return {"runtime__network": "WAN_REQUIRED", "capabilities__0__network_requirement": "WAN_REQUIRED",
            "adapters__0__network_requirement": "WAN_REQUIRED", "security__egress": "ALLOWLIST", **changes}


def test_an_allowlisted_public_host_is_accepted() -> None:
    assert _variant(**_networked(security__egress_hosts=["api.example.org"])).security.egress_hosts


@pytest.mark.parametrize("host", ["169.254.169.254", "10.0.0.8", "localhost", "metadata.localhost", "printer.local"])
def test_ip_literal_loopback_and_link_local_egress_hosts_are_refused(host: str) -> None:
    with pytest.raises(PackManifestError) as excinfo:
        _variant(**_networked(security__egress_hosts=[host]))
    assert excinfo.value.code == "PACK_EGRESS_HOST_INVALID"


@pytest.mark.parametrize("source", ["THIRD_PARTY", "OPERATOR_SUPPLIED"])
def test_external_code_may_not_declare_in_process_execution(source: str) -> None:
    with pytest.raises(PackManifestError) as excinfo:
        _variant(provenance__source=source, provenance__source_digest="e" * 64)
    assert excinfo.value.code == "PACK_RUNTIME_ISOLATION_REQUIRED"
    assert _variant(provenance__source=source, provenance__source_digest="e" * 64,
                    runtime__runtime_type="CONTAINER").provenance.source.value == source


def test_undeclared_evidence_keys_are_refused() -> None:
    def extra(produced: StageOutput, _: StageContext) -> StageOutput:
        return StageOutput(outputs=produced.outputs, evidence=dict(produced.evidence, side_channel="x"))

    with pytest.raises(CapabilityAdapterError) as excinfo:
        _direct(GovernedPackAdapter(reference.MANIFEST, _Impl(output=extra)))
    assert excinfo.value.code == "PACK_EVIDENCE_UNDECLARED"


class _RefusingRegistry:
    """The real registry, except that it refuses to register adapters (e.g. it is full)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def register_adapter(self, *args: Any, **kwargs: Any) -> Any:
        from pados.capability.registry import RegistryError
        raise RegistryError("ADAPTER_REGISTRY_FULL")


def test_a_registry_refusal_mid_registration_leaves_nothing_plannable(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    with pytest.raises(PackManifestError) as excinfo:
        register_pack(_RefusingRegistry(fab.registry), reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()},
                      operator=OPERATOR)
    assert excinfo.value.code == "PACK_REGISTRATION_INCOMPLETE"
    assert not [a for a in fab.registry.adapters_for(reference.CAPABILITY_ID) if a.descriptor.lifecycle.plannable]
    assert fab.planner().plan(_task(fab)).status is PlanStatus.INFEASIBLE


def test_registration_revalidates_a_manifest_that_bypassed_validation(tmp_path: Path) -> None:
    from pados.capability_loop.contracts import CapabilityState
    forged = reference.MANIFEST.model_copy(update={"initial_state": CapabilityState.VALIDATED})
    fab = fabric(tmp_path)
    with pytest.raises(PackManifestError) as excinfo:
        register_pack(fab.registry, forged, {reference.ADAPTER_ID: reference.adapter()}, operator=OPERATOR)
    assert excinfo.value.code == "PACK_INITIAL_STATE_INVALID"
    assert not fab.registry.is_installed(reference.ADAPTER_ID)


def test_disable_is_all_or_nothing_and_idempotent(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    with pytest.raises(PackManifestError) as excinfo:
        disable_pack(fab.registry, reference.MANIFEST, operator=OPERATOR)
    assert excinfo.value.code == "PACK_NOT_REGISTERED"
    register_pack(fab.registry, reference.MANIFEST, {reference.ADAPTER_ID: reference.adapter()}, operator=OPERATOR)
    disable_pack(fab.registry, reference.MANIFEST, operator=OPERATOR)
    events = len(fab.registry.events)
    disable_pack(fab.registry, reference.MANIFEST, operator=OPERATOR)  # already disabled: no change, no error
    assert len(fab.registry.events) == events
