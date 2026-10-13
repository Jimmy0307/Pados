"""Shared Capability Fabric contract (incl. regressions for Codex CR-09 CF-01..CF-11)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from pados.capability.adapter import (
    StageBindingError,
    StageContext,
    StageOutput,
    StageRequest,
    bind_stage,
    run_bound_stage,
)
from pados.capability.artifacts import (
    ArtifactAccessDenied,
    ArtifactIntegrityError,
    ArtifactRef,
    ObjectStoreArtifacts,
)
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Distribution,
    FallbackPolicy,
    LicenseInfo,
    LifecycleState,
    Modality,
    VerificationMethod,
)
from pados.capability.policy import (
    AuthorizedScope,
    PolicyCode,
    PolicyContext,
    authorize_scope,
    evaluate,
    fallback_permitted,
)
from pados.capability.receipts import PlanReceipt, StageReceipt, VerificationStatus, chain, parameters_hash
from pados.capability.seams import ADVICE_BOUND, Advice, AdviceRejected, apply_advice
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from tests.windows_links import create_link_or_skip

OCR = CapabilityDescriptor(capability_id="DOCUMENT_OCR", version="1", family=CapabilityFamily.PERCEPTION,
                           operation="RECOGNIZE_TEXT", input_contract="ocr.request/v1",
                           output_contract="ocr.document/v1", determinism=Determinism.DETERMINISTIC,
                           modalities=(Modality.PDF, Modality.IMAGE), max_classification=DataClassification.RESTRICTED,
                           network_requirement=NetworkRequirement.LOCAL_ONLY,
                           verification=(VerificationMethod.SCHEMA,))
LLM = CapabilityDescriptor(capability_id="MODEL_INFERENCE", version="1", family=CapabilityFamily.MODEL_RUNTIME,
                           operation="GENERATE_TEXT", input_contract="model.prompt/v1",
                           output_contract="model.text/v1", determinism=Determinism.PROBABILISTIC,
                           modalities=(Modality.TEXT,), network_requirement=NetworkRequirement.WAN_REQUIRED,
                           verification=(VerificationMethod.HUMAN_REVIEW,))
CONTRACT = {"work_contract_id": "wc-1", "project_id": "p", "required_capabilities": ["CAPABILITY:DOCUMENT_OCR"]}
GRANT = {"work_contract_id": "wc-1", "project_id": "p", "capabilities": ["CAPABILITY:DOCUMENT_OCR",
                                                                       "CAPABILITY:MODEL_INFERENCE"]}
ALWAYS = lambda: True  # noqa: E731


def adapter(**overrides: object) -> AdapterDescriptor:
    values: dict[str, object] = dict(adapter_id="ocr.text-layer", capability_id="DOCUMENT_OCR", version="1",
                                     backend="pypdf", determinism=Determinism.DETERMINISTIC,
                                     max_classification=DataClassification.RESTRICTED,
                                     network_requirement=NetworkRequirement.LOCAL_ONLY)
    values.update(overrides)
    return AdapterDescriptor(**values)  # type: ignore[arg-type]


def scope() -> AuthorizedScope:
    return authorize_scope(CONTRACT, GRANT)


def context(**overrides: object) -> PolicyContext:
    values: dict[str, object] = dict(scope=scope(), project_id="p", stage_id="s1",
                                     classification=DataClassification.CONFIDENTIAL,
                                     network_policy=NetworkRequirement.LOCAL_ONLY)
    values.update(overrides)
    return PolicyContext(**values)  # type: ignore[arg-type]


# --- CF-01: authorization comes only from the existing grant validator --------------------------

def test_cf01_scope_is_derived_from_the_existing_grant_validator() -> None:
    with pytest.raises(PermissionError, match="GRANT_SCOPE_MISMATCH"):
        authorize_scope(CONTRACT, {**GRANT, "project_id": "other"})
    with pytest.raises(PermissionError, match="CAPABILITY_NOT_AUTHORIZED"):
        authorize_scope(CONTRACT, {**GRANT, "capabilities": []})
    assert scope().authorization_source == "PADOS_PROJECT_GRANT" and len(scope().grant_hash) == 64
    assert evaluate(OCR, adapter(), context(project_id="q"), adapter_available=ALWAYS).code \
        is PolicyCode.SCOPE_PROJECT_MISMATCH


# --- CF-02: network is the intersection; fallback explicit and never weakens determinism -----

def test_cf02_adapter_cannot_widen_capability_network() -> None:
    wan = adapter(adapter_id="ocr.cloud", network_requirement=NetworkRequirement.WAN_REQUIRED)
    decision = evaluate(OCR, wan, context(network_policy=NetworkRequirement.WAN_REQUIRED), adapter_available=ALWAYS)
    assert decision.code is PolicyCode.ADAPTER_WIDENS_CAPABILITY_NETWORK
    assert evaluate(OCR, adapter(), context(), adapter_available=ALWAYS).allowed


def test_cf02_fallback_requires_opt_in_same_policy_and_no_determinism_downgrade() -> None:
    primary, alt = adapter(), adapter(adapter_id="ocr.other")
    p_dec = evaluate(OCR, primary, context(), adapter_available=ALWAYS)
    assert fallback_permitted(OCR, primary, alt, p_dec, evaluate(OCR, alt, context(), adapter_available=ALWAYS)) is None
    opted = OCR.model_copy(update={"fallback": FallbackPolicy.EXPLICIT_SAME_POLICY})
    p_dec = evaluate(opted, primary, context(), adapter_available=ALWAYS)
    transition = fallback_permitted(opted, primary, alt, p_dec, evaluate(opted, alt, context(), adapter_available=ALWAYS))
    assert transition is not None and len(transition.transition_hash) == 64
    model_alt = adapter(adapter_id="ocr.vlm", determinism=Determinism.PROBABILISTIC)
    assert fallback_permitted(opted, primary, model_alt, p_dec,
                              evaluate(opted, model_alt, context(), adapter_available=ALWAYS)) is None


def test_cf12_fallback_decision_from_another_context_is_refused() -> None:
    opted = OCR.model_copy(update={"fallback": FallbackPolicy.EXPLICIT_SAME_POLICY})
    primary, alt = adapter(), adapter(adapter_id="ocr.other")
    p_dec = evaluate(opted, primary, context(), adapter_available=ALWAYS)
    for other in (context(stage_id="s9"), context(classification=DataClassification.INTERNAL),
                  context(network_policy=NetworkRequirement.PRIVATE_NETWORK)):
        assert fallback_permitted(opted, primary, alt, p_dec, evaluate(opted, alt, other, adapter_available=ALWAYS)) is None
    stale = evaluate(opted, adapter(adapter_id="ocr.other", version="0"), context(), adapter_available=ALWAYS)
    assert fallback_permitted(opted, primary, alt, p_dec, stale) is None  # decision for another descriptor version


def test_cf01_execution_revalidates_the_current_grant(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    ref = store.put(b"x", media_type="text/plain", kind="ocr.input")
    binding = _bound(tmp_path, store, ref)
    def revoked() -> AuthorizedScope:
        return authorize_scope(CONTRACT, {**GRANT, "capabilities": []})
    with pytest.raises(StageBindingError, match="REVOKED"):
        run_bound_stage(binding, EchoAdapter(adapter()), store, revalidate=revoked)
    def changed() -> AuthorizedScope:
        return authorize_scope(CONTRACT, {**GRANT, "approval_ref": "different"})
    with pytest.raises(StageBindingError, match="CHANGED"):
        run_bound_stage(binding, EchoAdapter(adapter()), store, revalidate=changed)


def test_cf05_model_stage_needs_router_evidence_at_execution(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    ref = store.put(b"prompt-ref", media_type="text/plain", kind="model.prompt")
    both = authorize_scope({**CONTRACT, "required_capabilities": ["CAPABILITY:MODEL_INFERENCE"]}, GRANT)
    bridged = adapter(adapter_id="llm.router", capability_id="MODEL_INFERENCE", determinism=Determinism.PROBABILISTIC,
                      network_requirement=NetworkRequirement.WAN_REQUIRED, max_classification=DataClassification.INTERNAL,
                      delegates_to_worker_router=True)
    ctx = context(scope=both, network_policy=NetworkRequirement.WAN_REQUIRED, classification=DataClassification.INTERNAL)
    decision = evaluate(LLM, bridged, ctx, adapter_available=ALWAYS)
    request = StageRequest("s1", "MODEL_INFERENCE", (ref,), {}, DataClassification.INTERNAL)
    binding = bind_stage(LLM, bridged, ctx, decision, request, permitted_inputs=frozenset({ref.sha256}))
    with pytest.raises(StageBindingError, match="WORKER_ROUTER"):
        run_bound_stage(binding, EchoAdapter(bridged), store,
                        revalidate=lambda: authorize_scope({**CONTRACT, "required_capabilities":
                                                            ["CAPABILITY:MODEL_INFERENCE"]}, GRANT))


def test_stage_evidence_must_be_flat() -> None:
    with pytest.raises(ValueError, match="NOT_FLAT"):
        StageOutput(outputs=(), evidence={"nested": {"text": "document body"}})


def test_cf13_relative_store_root_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = ObjectStoreArtifacts(Path("relative-objects"))
    ref = store.put(b"ok", media_type="text/plain", kind="ocr.text")
    assert store.get(ref) == b"ok"


# --- CF-03 / CF-04: scoped artifacts and the trusted stage binding ---------------------------

class EchoAdapter:
    def __init__(self, descriptor: AdapterDescriptor, *, steal: ArtifactRef | None = None) -> None:
        self.descriptor = descriptor
        self.steal = steal

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        source = self.steal or request.inputs[0]
        data = context.artifacts.get(source)
        return StageOutput(outputs=(context.artifacts.put(data.upper(), media_type="text/plain", kind="ocr.text"),))


def _bound(tmp_path: Path, store: ObjectStoreArtifacts, ref: ArtifactRef):  # type: ignore[no-untyped-def]
    ctx = context()
    decision = evaluate(OCR, adapter(), ctx, adapter_available=ALWAYS)
    request = StageRequest("s1", "DOCUMENT_OCR", (ref,), {}, DataClassification.CONFIDENTIAL)
    return bind_stage(OCR, adapter(), ctx, decision, request, permitted_inputs=frozenset({ref.sha256}))


def test_cf03_cf04_adapter_reads_only_authorized_inputs(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    mine = store.put(b"mine", media_type="text/plain", kind="ocr.input")
    other_project = store.put(b"secret of another project", media_type="text/plain", kind="ocr.input")
    binding = _bound(tmp_path, store, mine)
    assert run_bound_stage(binding, EchoAdapter(adapter()), store, revalidate=scope).outputs[0].size_bytes == 4
    with pytest.raises(ArtifactAccessDenied):
        run_bound_stage(binding, EchoAdapter(adapter(), steal=other_project), store, revalidate=scope)


def test_cf04_binding_refuses_mismatched_or_denied_decisions(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    ref = store.put(b"x", media_type="text/plain", kind="ocr.input")
    ctx = context()
    request = StageRequest("s1", "DOCUMENT_OCR", (ref,), {}, DataClassification.CONFIDENTIAL)
    denied = evaluate(OCR, adapter(), context(scope=replace(scope(), capabilities=frozenset())),
                      adapter_available=ALWAYS)
    with pytest.raises(StageBindingError, match="NOT_ALLOWED"):
        bind_stage(OCR, adapter(), ctx, denied, request, permitted_inputs=frozenset({ref.sha256}))
    allowed = evaluate(OCR, adapter(), ctx, adapter_available=ALWAYS)
    with pytest.raises(StageBindingError, match="CONTEXT_MISMATCH"):
        bind_stage(OCR, adapter(), ctx, allowed, replace(request, classification=DataClassification.RESTRICTED),
                   permitted_inputs=frozenset({ref.sha256}))
    with pytest.raises(StageBindingError, match="INPUT_NOT_PERMITTED"):
        bind_stage(OCR, adapter(), ctx, allowed, request, permitted_inputs=frozenset())
    binding = bind_stage(OCR, adapter(), ctx, allowed, request, permitted_inputs=frozenset({ref.sha256}))
    with pytest.raises(StageBindingError, match="DESCRIPTOR_CHANGED"):
        run_bound_stage(binding, EchoAdapter(adapter(version="2")), store, revalidate=scope)


def test_cf04_availability_probe_runs_only_after_policy_passes() -> None:
    probed: list[str] = []
    def probe() -> bool:
        probed.append("x")
        return True
    evaluate(OCR, adapter(), context(scope=replace(scope(), capabilities=frozenset())), adapter_available=probe)
    assert probed == []
    evaluate(OCR, adapter(), context(), adapter_available=probe)
    assert probed == ["x"]


# --- CF-05: model inference must delegate to the Worker Router bridge ------------------------

def test_cf05_model_runtime_adapters_must_delegate() -> None:
    own = adapter(adapter_id="llm.own", capability_id="MODEL_INFERENCE", determinism=Determinism.PROBABILISTIC,
                  network_requirement=NetworkRequirement.WAN_REQUIRED, max_classification=DataClassification.INTERNAL)
    both = authorize_scope({**CONTRACT, "required_capabilities": ["CAPABILITY:MODEL_INFERENCE"]}, GRANT)
    ctx = context(scope=both, network_policy=NetworkRequirement.WAN_REQUIRED,
                  classification=DataClassification.INTERNAL)
    assert evaluate(LLM, own, ctx, adapter_available=ALWAYS).code is PolicyCode.MODEL_INFERENCE_MUST_DELEGATE
    bridged = own.model_copy(update={"adapter_id": "llm.router", "delegates_to_worker_router": True})
    assert evaluate(LLM, bridged, ctx, adapter_available=ALWAYS).allowed


# --- CF-06 / CF-11: receipts are structural evidence with full provenance --------------------

def _receipt(stage_id: str, **overrides: object) -> StageReceipt:
    ref = ArtifactRef(sha256="a" * 64, media_type="text/plain", size_bytes=1, kind="xx")
    values: dict[str, object] = dict(
        plan_id="pl", stage_id=stage_id, attempt_id="a1", capability_id="DOCUMENT_OCR",
        capability_family=CapabilityFamily.PERCEPTION, capability_version="1",
        capability_digest="c" * 64, adapter_id="ocr.text-layer", adapter_version="1", adapter_digest="d" * 64,
        resource_id="local", determinism=Determinism.DETERMINISTIC, inputs=(ref,), outputs=(ref,),
        parameters_hash=parameters_hash({}), started_at="2026-10-03T00:00:00+00:00",
        finished_at="2026-10-03T00:00:01+00:00", duration_ms=1000, verification_method=VerificationMethod.SCHEMA,
        verification=VerificationStatus.PASSED, verification_evidence_hash="e" * 64, verifier_id="schema-validator", policy_decision_hash="b" * 64,
        prev_hash="0" * 64)
    values.update(overrides)
    return StageReceipt(**values)  # type: ignore[arg-type]


def test_cf06_chain_is_bound_to_the_plan_and_cannot_set_acceptance() -> None:
    stages = chain("pl", "f" * 64, [_receipt("s1"), _receipt("s2")])
    plan = PlanReceipt(plan_id="pl", plan_hash="f" * 64, expected_stage_ids=("s1", "s2"), stages=tuple(stages))
    assert plan.verify_structure()
    assert not PlanReceipt(plan_id="pl", plan_hash="9" * 64, expected_stage_ids=("s1", "s2"),
                           stages=tuple(stages)).verify_structure()  # re-hung under another plan
    assert not PlanReceipt(plan_id="pl", plan_hash="f" * 64, expected_stage_ids=("s1", "s2", "s3"),
                           stages=tuple(stages)).verify_structure()  # a planned stage is missing
    with pytest.raises(ValidationError):
        PlanReceipt(plan_id="pl", plan_hash="f" * 64, expected_stage_ids=(), stages=())
    with pytest.raises(ValidationError):
        PlanReceipt.model_validate({**plan.model_dump(), "human_acceptance": "ACCEPTED"})


@pytest.mark.parametrize("overrides", [
    {"finished_at": "2026-10-02T00:00:00+00:00"},  # time order
    {"started_at": "2026-10-03T00:00:00"},  # naive timestamp
    {"determinism": Determinism.SEEDED},  # seed missing
    {"verification_evidence_hash": None},  # PASSED without evidence
    {"failure_code": "ADAPTER_FAILED"},  # failed but reports PASSED + full output
    {"partial": True},  # partial without failure code
    {"capability_digest": "nothex"},
    {"metrics": {"x": float("nan")}},
    {"semantic_authority": "ACCEPTED"},
    {"verifier_id": None},  # PASSED needs a verifier identity
    {"capability_family": CapabilityFamily.MODEL_RUNTIME},  # model stage without router evidence
])
def test_cf11_receipt_provenance_is_validated(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _receipt("s1", **overrides)


# --- CF-07: decision hashes are bound to their context ---------------------------------------

def test_cf07_decision_hash_differs_across_contexts() -> None:
    base = evaluate(OCR, adapter(), context(), adapter_available=ALWAYS).decision_hash
    assert base != evaluate(OCR, adapter(), context(stage_id="s2"), adapter_available=ALWAYS).decision_hash
    assert base != evaluate(OCR, adapter(), context(classification=DataClassification.INTERNAL),
                            adapter_available=ALWAYS).decision_hash
    assert base != evaluate(OCR, adapter(version="2"), context(), adapter_available=ALWAYS).decision_hash


# --- CF-08: metadata cannot carry URLs, paths or shell fragments -----------------------------

@pytest.mark.parametrize("field,value", [
    ("capability_id", "ocr; rm -rf /"), ("input_contract", "http://evil/x"), ("operation", "run `id`"),
    ("operation", "fetch http://x"), ("description", "fetch http://169.254.169.254/latest/meta-data"),
    ("description", "read /etc/passwd"), ("description", "see ../../secrets"), ("description", "$(curl evil)"),
    ("version", "../../etc"),
])
def test_cf08_descriptor_metadata_cannot_smuggle_commands_or_urls(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        CapabilityDescriptor.model_validate({**OCR.model_dump(), field: value})


def test_cf08_extra_fields_and_engine_versions() -> None:
    with pytest.raises(ValidationError):
        CapabilityDescriptor.model_validate({**OCR.model_dump(), "command": "tesseract"})
    with pytest.raises(ValidationError):
        AdapterDescriptor.model_validate({**adapter().model_dump(), "endpoint": "http://169.254.169.254/"})
    with pytest.raises(ValidationError):
        LicenseInfo(engine_version="5.3; curl evil")
    with pytest.raises(ValidationError, match="EXTERNAL_SERVICE_MUST_DECLARE_NETWORK"):
        adapter(license=LicenseInfo(spdx="LicenseRef-Proprietary", distribution=Distribution.EXTERNAL_SERVICE))


# --- CF-09: artifact I/O validation ----------------------------------------------------------

def test_cf09_artifact_store_validates_before_write_and_refuses_links(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    with pytest.raises(ValidationError):
        store.put(b"x", media_type="not a media type", kind="ocr.text")
    assert not any((tmp_path / "objects").rglob("*")) or all(p.is_dir() for p in (tmp_path / "objects").rglob("*"))
    ref = store.put(b"hello", media_type="text/plain", kind="ocr.text")
    target = tmp_path / "objects" / ref.sha256[:2] / ref.sha256[2:4] / ref.sha256
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"hello")
    target.unlink()
    create_link_or_skip(elsewhere, target, directory=False)
    with pytest.raises(ArtifactIntegrityError):
        store.get(ref)
    with pytest.raises(ValidationError):
        ArtifactRef(sha256="../../etc/passwd", media_type="text/plain", size_bytes=1, kind="xx")


# --- CF-10: advice is validated, intersected and bounded once per candidate ------------------

def test_cf10_advice_contract() -> None:
    def advice(adapter_id: str, score: float, ctx: str = "ctx") -> Advice:
        return Advice(adapter_id, score, 10, "historically better on scanned PDFs", "p" * 64, ctx)
    with pytest.raises(ValueError):
        advice("a", float("nan"))
    with pytest.raises(ValueError):
        Advice("a", 0.1, -1, "x", "p" * 64, "ctx")
    with pytest.raises(AdviceRejected):
        apply_advice({"a"}, [advice("a", 0.1), advice("a", 0.2)], context_hash="ctx")
    adjusted = apply_advice({"a", "b"}, [advice("a", 9.0), advice("b", -9.0), advice("zz-unauthorized", 1.0),
                                         advice("other-context", 1.0, ctx="other")], context_hash="ctx")
    assert adjusted == {"a": ADVICE_BOUND, "b": -ADVICE_BOUND}


def test_lifecycle_and_classification_still_fail_closed() -> None:
    assert evaluate(OCR, adapter(lifecycle=LifecycleState.REVOKED), context(),
                    adapter_available=ALWAYS).code is PolicyCode.LIFECYCLE_NOT_PLANNABLE
    small = adapter(max_classification=DataClassification.INTERNAL)
    assert evaluate(OCR, small, context(), adapter_available=ALWAYS).code is PolicyCode.CLASSIFICATION_EXCEEDS_ADAPTER
    assert evaluate(OCR, adapter(), context(), adapter_available=lambda: False).code is PolicyCode.ADAPTER_UNAVAILABLE


def test_cf14_router_evidence_must_be_a_hash(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    ref = store.put(b"prompt-ref", media_type="text/plain", kind="model.prompt")
    both = authorize_scope({**CONTRACT, "required_capabilities": ["CAPABILITY:MODEL_INFERENCE"]}, GRANT)
    bridged = adapter(adapter_id="llm.router", capability_id="MODEL_INFERENCE", determinism=Determinism.PROBABILISTIC,
                      network_requirement=NetworkRequirement.WAN_REQUIRED, max_classification=DataClassification.INTERNAL,
                      delegates_to_worker_router=True)
    ctx = context(scope=both, network_policy=NetworkRequirement.WAN_REQUIRED, classification=DataClassification.INTERNAL)
    decision = evaluate(LLM, bridged, ctx, adapter_available=ALWAYS)
    for bogus in ("", "router-said-ok"):
        request = StageRequest("s1", "MODEL_INFERENCE", (ref,), {}, DataClassification.INTERNAL, router_selection_hash=bogus)
        binding = bind_stage(LLM, bridged, ctx, decision, request, permitted_inputs=frozenset({ref.sha256}))
        with pytest.raises(StageBindingError, match="WORKER_ROUTER"):
            run_bound_stage(binding, EchoAdapter(bridged), store, revalidate=lambda: both)


def test_cf15_decisions_bind_the_policy_version() -> None:
    assert dict(evaluate(OCR, adapter(), context(), adapter_available=ALWAYS).binding)["policy_version"]
