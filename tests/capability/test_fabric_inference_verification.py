"""Worker Router bridge (the only router contact) and stage verification (never acceptance)."""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
from fabric_support import CHUNK, OCR, MODEL, fabric

from pados.capability.adapter import StageOutput
from pados.capability.inference import WorkerRouterBridge, selection_routed
from pados.capability.model import Determinism, VerificationMethod
from pados.capability.receipts import VerificationStatus
from pados.capability.seams import InferenceRequest
from pados.capability.verification import (
    ValidatorRegistry,
    VerificationEngine,
    VerificationError,
    VerificationResult,
)
from pados.enterprise.classification import DataClassification
from pados.worker.router import select_worker
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType


def worker(worker_id: str, worker_type: WorkerType, *, available: bool = True) -> WorkerSpec:
    return WorkerSpec(worker_id=worker_id, worker_type=worker_type, display_name=worker_id,
                      availability=WorkerAvailability.AVAILABLE if available else WorkerAvailability.UNAVAILABLE,
                      capabilities=[CapabilityId.SUMMARIZE], risk_ceiling=RiskLevel.MEDIUM,
                      supports_structured_output=True, supports_file_changes=False, supports_tool_calls=False,
                      supports_streaming=False, max_context_tokens=8000, default_timeout_seconds=60)


WORKERS = {"local-model": worker("local-model", WorkerType.OLLAMA),
           "remote-model": worker("remote-model", WorkerType.CLAUDE_CODE)}


def request(**overrides: object) -> InferenceRequest:
    values: dict[str, object] = dict(project_id="p", stage_id="summary", capability_id="MODEL_SUMMARY",
                                     risk_level="LOW", classification=DataClassification.INTERNAL,
                                     network_policy="WAN_REQUIRED", grant_hash="g" * 64)
    values.update(overrides)
    return InferenceRequest(**values)  # type: ignore[arg-type]


def bridge(workers: dict[str, WorkerSpec] = WORKERS) -> WorkerRouterBridge:
    return WorkerRouterBridge(lambda: workers, {"MODEL_SUMMARY": CapabilityId.SUMMARIZE})


# --- Worker Router bridge ----------------------------------------------------------------------

def test_bridge_returns_the_router_decision_unchanged() -> None:
    selection = bridge().select(request())
    direct = select_worker(dict(WORKERS), capability_id=CapabilityId.SUMMARIZE, risk_level=RiskLevel.LOW)
    assert selection.selected_worker_id == direct.selected_worker_id == "local-model"
    assert selection.status == "ROUTED" and selection_routed(selection) and len(selection.selection_hash) == 64
    assert bridge().select(request()).selection_hash == selection.selection_hash  # canonical, deterministic
    assert bridge().select(request(stage_id="other")).selection_hash != selection.selection_hash


def test_bridge_narrows_by_classification_and_network_without_reranking() -> None:
    local_down = {**WORKERS, "local-model": worker("local-model", WorkerType.OLLAMA, available=False)}
    wan = bridge(local_down).select(request())
    assert wan.selected_worker_id == "remote-model"  # the router's own ranking picked the next worker
    local_only = bridge(local_down).select(request(network_policy="LOCAL_ONLY"))
    assert local_only.selected_worker_id is None and not selection_routed(local_only)  # no local -> cloud
    confidential = bridge(local_down).select(request(classification=DataClassification.CONFIDENTIAL))
    assert confidential.selected_worker_id is None  # remote workers are capped at INTERNAL


def test_bridge_fails_closed_on_unmapped_or_invalid_requests() -> None:
    assert bridge().select(request(capability_id="UNKNOWN_CAP")).status == "BLOCKED"
    assert bridge().select(request(risk_level="EXTREME")).explanation == "INFERENCE_REQUEST_INVALID"
    assert bridge().select(request(network_policy="ANYWHERE")).selected_worker_id is None


# --- verification --------------------------------------------------------------------------------

def _output(fab_store: object, payload: bytes) -> StageOutput:
    ref = fab_store.put(payload, media_type="text/plain", kind="ocr.text")  # type: ignore[attr-defined]
    return StageOutput(outputs=(ref,))


def test_schema_verification_pass_fail_and_missing_validator(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    engine = VerificationEngine(schemas=fab.schemas)
    passed = engine.verify(OCR, _output(fab.store, b"UPPER"), fab.store, seed=None, replay=None)
    assert passed.status is VerificationStatus.PASSED and passed.method is VerificationMethod.SCHEMA
    assert passed.verifier_id and passed.evidence_hash
    failed = engine.verify(OCR, _output(fab.store, b"lower"), fab.store, seed=None, replay=None)
    assert failed.status is VerificationStatus.FAILED and failed.evidence_hash != passed.evidence_hash
    missing = VerificationEngine().verify(OCR, _output(fab.store, b"UPPER"), fab.store, seed=None, replay=None)
    assert missing.status is VerificationStatus.NOT_VERIFIED  # cannot verify -> never PASSED
    assert missing.results[0].code == "SCHEMA_CHECKER_MISSING"


def test_crashing_validator_fails_and_registry_refuses_override(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    schemas = ValidatorRegistry()

    def crash(outputs: object) -> bool:
        raise RuntimeError("validator bug")

    schemas.register("document.ocr/v1", "crashy", crash)
    with pytest.raises(VerificationError, match="VALIDATOR_ALREADY_REGISTERED"):
        schemas.register("document.ocr/v1", "other", lambda outputs: True)
    with pytest.raises(VerificationError, match="VALIDATOR_IDENTITY_INVALID"):
        schemas.register("http://x", "id", lambda outputs: True)
    result = VerificationEngine(schemas=schemas).verify(OCR, _output(fab.store, b"UPPER"), fab.store, seed=None,
                                                        replay=None)
    assert result.status is VerificationStatus.FAILED


def test_deterministic_replay_and_reference_check(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    out = _output(fab.store, b"A")
    engine = VerificationEngine()
    assert engine.verify(CHUNK, out, fab.store, seed=None, replay=lambda: out).status is VerificationStatus.PASSED
    other = _output(fab.store, b"B")
    assert engine.verify(CHUNK, out, fab.store, seed=None, replay=lambda: other).status is VerificationStatus.FAILED

    def boom() -> StageOutput:
        raise RuntimeError("replay crashed")

    assert engine.verify(CHUNK, out, fab.store, seed=None, replay=boom).status is VerificationStatus.FAILED
    probabilistic = CHUNK.model_copy(update={"determinism": Determinism.PROBABILISTIC})
    assert engine.verify(probabilistic, out, fab.store, seed=None,
                         replay=lambda: out).status is VerificationStatus.NOT_VERIFIED
    # The selected adapter's own (digest-bound) DETERMINISTIC declaration makes replay applicable even under a
    # PROBABILISTIC capability; a probabilistic adapter there is still not replayed, and a false claim fails.
    assert engine.verify(probabilistic, out, fab.store, seed=None, replay=lambda: out,
                         adapter_determinism=Determinism.DETERMINISTIC).status is VerificationStatus.PASSED
    assert engine.verify(probabilistic, out, fab.store, seed=None, replay=lambda: other,
                         adapter_determinism=Determinism.DETERMINISTIC).status is VerificationStatus.FAILED
    assert engine.verify(probabilistic, out, fab.store, seed=None, replay=lambda: out,
                         adapter_determinism=Determinism.PROBABILISTIC).status is VerificationStatus.NOT_VERIFIED
    assert engine.verify(CHUNK, out, fab.store, seed=None, replay=lambda: other,
                         adapter_determinism=Determinism.PROBABILISTIC).status is VerificationStatus.FAILED
    seeded = CHUNK.model_copy(update={"determinism": Determinism.SEEDED})
    assert engine.verify(seeded, out, fab.store, seed=None, replay=lambda: out).status is VerificationStatus.NOT_VERIFIED
    assert engine.verify(seeded, out, fab.store, seed=7, replay=lambda: out).status is VerificationStatus.PASSED
    refs = ValidatorRegistry()
    refs.register("text.chunks/v1", "golden-fixture", lambda outputs: [d for _, d in outputs] == [b"A"])
    referenced = CHUNK.model_copy(update={"verification": (VerificationMethod.REFERENCE_CHECK,)})
    result = VerificationEngine(references=refs).verify(referenced, out, fab.store, seed=None, replay=None)
    assert result.status is VerificationStatus.PASSED and result.results[0].checker_id == "golden-fixture"


def test_none_and_human_review_never_pass_and_nothing_sets_acceptance(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    out = _output(fab.store, b"A")
    none = OCR.model_copy(update={"verification": (VerificationMethod.NONE,)})
    result = VerificationEngine().verify(none, out, fab.store, seed=None, replay=None)
    assert result.status is VerificationStatus.NOT_VERIFIED and result.evidence_hash is None
    human = VerificationEngine().verify(MODEL, out, fab.store, seed=None, replay=None)
    assert human.status is VerificationStatus.NOT_VERIFIED and human.results[0].code == "EXTERNAL_METHOD"
    partial = StageOutput(outputs=out.outputs, partial=True)
    assert VerificationEngine(schemas=fab.schemas).verify(OCR, partial, fab.store, seed=None,
                                                          replay=None).status is VerificationStatus.NOT_VERIFIED
    assert not {"accepted", "acceptance", "human_acceptance"} & {f.name for f in fields(VerificationResult)}


def test_end_to_end_model_stage_through_the_real_worker_router_bridge(tmp_path: Path) -> None:
    from fabric_support import with_model

    from pados.capability.executor import ExecutionRefused, ExecutionStatus

    fab = fabric(tmp_path)
    task = with_model(fab)
    live = dict(WORKERS)
    real = WorkerRouterBridge(lambda: live, {"MODEL_SUMMARY": CapabilityId.SUMMARIZE})
    plan = fab.planner(inference=real).plan(task)
    result = fab.executor(inference=real).execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED
    summary = result.plan_receipt.stages[2]
    assert result.router_evidence[str(summary.router_selection_hash)].selected_worker_id == "local-model"
    # The router decision changes during the run (after planning, before the model stage).
    fab.adapters["chunk.fixed"].hook = lambda: live.update(
        {"local-model": worker("local-model", WorkerType.OLLAMA, available=False)})
    refused = fab.executor(inference=real).execute(plan, task)
    assert refused.failure_code == "summary:WORKER_ROUTER_SELECTION_CHANGED"
    assert len(fab.adapters["llm.bridge"].calls) == 1  # only the first, unchanged run reached the adapter
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):  # and the plan is now irreproducible
        fab.executor(inference=real).execute(plan, task)
