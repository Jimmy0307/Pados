"""Shared fixtures for Outcome Learning tests (content-free metadata only, built at runtime).

``TrustedReceipts`` is the TEST DOUBLE of ``OutcomeAuthenticator``: in production the integrator wires the
authenticator to the fabric executor's receipt store, the planner's recorded plan context and the governed
approval path.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pados.capability.artifacts import ArtifactRef
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Modality,
    VerificationMethod,
)
from pados.capability.receipts import PlanReceipt, StageReceipt, VerificationStatus, chain, parameters_hash
from pados.connectivity.requirements import NetworkRequirement
from pados.connectivity.state import ConnectivityState
from pados.enterprise.classification import DataClassification
from pados.learning.advice_log import TEST_ONLY_ACKNOWLEDGEMENT, AdviceLog
from pados.learning.anchor import FileAnchor
from pados.learning.records import HardwareClass, HumanDecision, IdentityKey, OutcomeRecord, TrustedPlanContext

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SCANNED_ZH = {"document_language": "zh-Hant", "scan_type": "SCANNED", "mime_class": "PDF"}
BORN_DIGITAL_EN = {"document_language": "en", "scan_type": "BORN_DIGITAL", "mime_class": "PDF"}
CONF = DataClassification.CONFIDENTIAL
BINDING = hashlib.sha256(b"plan-binding-1").hexdigest()  # the planner's advice-independent plan binding


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class TrustedReceipts:
    """Test double of the executor receipt store + governed approval path."""

    def __init__(self) -> None:
        self.plans: dict[str, PlanReceipt] = {}
        self.contexts: dict[str, TrustedPlanContext] = {}
        self.decisions: dict[str, HumanDecision] = {}

    def register(self, plan: PlanReceipt, *, task_type: str = "DOCUMENT_EXTRACTION",
                 classification: DataClassification = DataClassification.CONFIDENTIAL,
                 features: Mapping[str, str] | None = None) -> PlanReceipt:
        """Register a plan with its trusted context; ``features=None``: the plan recorded no advice context."""
        for stage in plan.stages:
            self.plans[stage.receipt_hash] = plan
        self.contexts[plan.plan_hash] = TrustedPlanContext(task_type=task_type, classification=classification,
                                                           context_features=features)
        return plan

    def revoke(self, receipt_hash: str) -> None:
        self.plans.pop(receipt_hash, None)

    def decide(self, ref: str, verdict: HumanDecision) -> None:
        self.decisions[ref] = verdict

    def resolve_receipt(self, stage_receipt_hash: str) -> PlanReceipt | None:
        return self.plans.get(stage_receipt_hash)

    def resolve_plan_context(self, plan_hash: str) -> TrustedPlanContext | None:
        return self.contexts.get(plan_hash)

    def resolve_decision(self, decision_ref: str) -> HumanDecision | None:
        return self.decisions.get(decision_ref)


AUTH = TrustedReceipts()

OCR = CapabilityDescriptor(capability_id="DOCUMENT_OCR", version="1", family=CapabilityFamily.PERCEPTION,
                           operation="RECOGNIZE_TEXT", input_contract="ocr.request/v1",
                           output_contract="ocr.document/v1", determinism=Determinism.DETERMINISTIC,
                           modalities=(Modality.PDF, Modality.IMAGE), max_classification=DataClassification.RESTRICTED,
                           network_requirement=NetworkRequirement.LOCAL_ONLY,
                           verification=(VerificationMethod.SCHEMA,))


def adapter(adapter_id: str, version: str = "1", **overrides: Any) -> AdapterDescriptor:
    values: dict[str, Any] = dict(adapter_id=adapter_id, capability_id="DOCUMENT_OCR", version=version,
                                  backend="engine", determinism=Determinism.DETERMINISTIC,
                                  max_classification=DataClassification.RESTRICTED,
                                  network_requirement=NetworkRequirement.LOCAL_ONLY)
    values.update(overrides)
    return AdapterDescriptor(**values)


def stage_receipt(stage_id: str = "s1", *, plan_id: str = "plan-1", adapter_id: str = "ocr.engine-a",
                  adapter_version: str = "1", verification: VerificationStatus = VerificationStatus.PASSED,
                  failure_code: str | None = None, partial: bool = False, duration_ms: int = 1200,
                  resource_id: str = "node-1", finished_at: datetime = NOW) -> StageReceipt:
    outputs = () if failure_code and not partial else (
        ArtifactRef(sha256=sha(f"out-{plan_id}-{stage_id}"), media_type="application/json", size_bytes=10,
                    kind="ocr.doc"),)
    passed = verification is VerificationStatus.PASSED
    return StageReceipt(
        plan_id=plan_id, stage_id=stage_id, attempt_id=f"{stage_id}-a1", capability_id="DOCUMENT_OCR",
        capability_family=CapabilityFamily.PERCEPTION, capability_version="1", capability_digest=sha("cap"),
        adapter_id=adapter_id, adapter_version=adapter_version, adapter_digest=sha(adapter_id + adapter_version),
        resource_id=resource_id, determinism=Determinism.DETERMINISTIC,
        inputs=(ArtifactRef(sha256=sha("in"), media_type="application/pdf", size_bytes=99, kind="source.pdf"),),
        outputs=outputs, parameters_hash=parameters_hash({"dpi": 300}),
        started_at=(finished_at - timedelta(seconds=2)).isoformat(), finished_at=finished_at.isoformat(),
        duration_ms=duration_ms,
        verification_method=VerificationMethod.SCHEMA, verification=verification,
        verification_evidence_hash=sha("evidence") if passed else None, verifier_id="schema-v1" if passed else None,
        policy_decision_hash=sha("decision"), partial=partial, failure_code=failure_code, prev_hash=sha("x"))


def plan_receipt(*stages: StageReceipt, plan_id: str = "plan-1",
                 human_decision_ref: str | None = None) -> PlanReceipt:
    plan_hash = sha(f"plan-{plan_id}")
    linked = chain(plan_id, plan_hash, list(stages))
    return PlanReceipt(plan_id=plan_id, plan_hash=plan_hash, expected_stage_ids=tuple(s.stage_id for s in linked),
                       stages=tuple(linked), human_decision_ref=human_decision_ref)


def outcome(index: int, *, adapter_id: str = "ocr.engine-a", adapter_version: str = "1", success: bool = True,
            verification: VerificationStatus = VerificationStatus.PASSED,
            human: HumanDecision = HumanDecision.NONE, features: dict[str, str] | None = None,
            latency_ms: int = 1000, cost_units: float = 1.0, at: datetime | None = None,
            task_type: str = "DOCUMENT_EXTRACTION", resource_id: str = "node-1",
            quality: float | None = None, retry_count: int = 0, trusted_features: bool = True,
            classification: DataClassification = CONF, auth: TrustedReceipts = AUTH,
            identity_key: IdentityKey | None = None) -> OutcomeRecord:
    """An outcome built the production way: trusted receipts and plan context registered with the
    authenticator double. Executed (and ingested) ``index`` minutes before ``at`` (default NOW)."""
    if not success and verification is VerificationStatus.PASSED:
        verification = VerificationStatus.FAILED
    key = f"{adapter_id}-{adapter_version}-{index}-{task_type}-{resource_id}"
    plan_id = f"plan-{sha(key)[:12]}"
    executed = (at or NOW) - timedelta(minutes=index)
    stage = stage_receipt("s1", plan_id=plan_id, adapter_id=adapter_id, adapter_version=adapter_version,
                          verification=verification, failure_code=None if success else "OCR_ENGINE_FAILED",
                          duration_ms=latency_ms, resource_id=resource_id, finished_at=executed)
    ref = None if human is HumanDecision.NONE else f"decision-{sha(key)[:12]}"
    context = SCANNED_ZH if features is None else features
    plan = auth.register(plan_receipt(stage, plan_id=plan_id, human_decision_ref=ref), task_type=task_type,
                         classification=classification, features=context if trusted_features else None)
    if ref is not None:
        auth.decide(ref, human)
    return OutcomeRecord.from_receipts(
        plan, plan.stages[0], authenticator=auth, hardware_class=HardwareClass.CPU,
        network_state=ConnectivityState.OFFLINE, context_features=None if trusted_features else context,
        cost_units=cost_units, quality_metric=quality, retry_count=retry_count, recorded_at=executed,
        identity_key=identity_key)


def history(adapter_id: str, good: int, bad: int, **kwargs: Any) -> list[OutcomeRecord]:
    """``good`` successful verified outcomes then ``bad`` failed ones for one adapter."""
    offset = kwargs.pop("offset", 0)
    return [outcome(offset + i, adapter_id=adapter_id, success=i < good, **kwargs) for i in range(good + bad)]


class ListSource:
    def __init__(self, records: list[OutcomeRecord]) -> None:
        self.records = list(records)

    def read(self) -> tuple[OutcomeRecord, ...]:
        return tuple(self.records)


def memory_log(**kwargs: Any) -> AdviceLog:
    kwargs.setdefault("clock", lambda: NOW)
    return AdviceLog.ephemeral_for_tests(acknowledgement=TEST_ONLY_ACKNOWLEDGEMENT, **kwargs)


def file_anchor(tmp_path: Path) -> FileAnchor:
    """The external anchor of ``file_log`` (a directory OUTSIDE the advice log's store directory)."""
    return FileAnchor(tmp_path / "anchor")


def file_log(tmp_path: Path, **kwargs: Any) -> AdviceLog:
    """A durable advice log anchored in ``file_anchor(tmp_path)``; pass ``anchor=None`` for an unanchored log."""
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("anchor", file_anchor(tmp_path))
    return AdviceLog(tmp_path / "learning" / "advice.jsonl", **kwargs)
