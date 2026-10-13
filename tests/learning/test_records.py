"""Outcome/evaluation records: facts from trusted receipts, governed verdicts, no content."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from pados.capability.receipts import VerificationStatus
from pados.connectivity.state import ConnectivityState
from pados.learning.records import (
    CONTEXT_FEATURES,
    OUTCOME_KEYS,
    EvaluationRecord,
    FailureCode,
    HardwareClass,
    HumanDecision,
    LearningRecordError,
    OutcomeRecord,
    context_bucket,
    decision_ref_for,
    page_bucket,
    resource_ref_for,
    size_bucket,
    task_ref_for,
    validate_context_features,
)
from pados.lifecycle.minimization import DataMinimizationViolation
from tests.learning.learning_support import (
    NOW,
    SCANNED_ZH,
    TrustedReceipts,
    outcome,
    plan_receipt,
    sha,
    stage_receipt,
)


def derive(plan: Any, stage: Any, auth: TrustedReceipts, **overrides: Any) -> OutcomeRecord:
    values: dict[str, Any] = dict(hardware_class=HardwareClass.CPU, network_state=ConnectivityState.OFFLINE,
                                  context_features=SCANNED_ZH, recorded_at=NOW)
    values.update(overrides)
    return OutcomeRecord.from_receipts(plan, stage, authenticator=auth, **values)


# --- execution facts come from trusted receipts ------------------------------------------------

def test_facts_are_derived_from_the_trusted_stage_receipt() -> None:
    auth = TrustedReceipts()
    plan = auth.register(plan_receipt(stage_receipt(duration_ms=4321)))
    record = derive(plan, plan.stages[0], auth, cost_units=0.5, quality_metric=0.9, retry_count=1)
    stage = plan.stages[0]
    assert (record.adapter_id, record.adapter_version) == ("ocr.engine-a", "1")
    assert record.resource_ref == resource_ref_for("node-1") and record.task_ref == task_ref_for("DOCUMENT_EXTRACTION")
    assert record.latency_ms == 4321 and record.success and record.failure_code is None
    assert record.verification is VerificationStatus.PASSED
    assert record.receipt_hash == stage.receipt_hash and record.plan_hash == plan.plan_hash
    assert record.outcome_id == f"oc-{stage.receipt_hash[:40]}"
    assert record.human_decision is HumanDecision.NONE and record.human_decision_ref is None


def test_failed_and_partial_stages_map_to_closed_codes() -> None:
    auth = TrustedReceipts()
    plan = auth.register(plan_receipt(
        stage_receipt("s1", failure_code="TIMEOUT", verification=VerificationStatus.NOT_VERIFIED),
        stage_receipt("s2", failure_code="engine crashed: see log", partial=True,
                      verification=VerificationStatus.NOT_VERIFIED)))
    first, second = derive(plan, plan.stages[0], auth), derive(plan, plan.stages[1], auth)
    assert not first.success and first.failure_code is FailureCode.TIMEOUT
    assert not second.success and second.failure_code is FailureCode.ADAPTER_FAILURE


def test_untrusted_or_foreign_receipts_are_refused() -> None:
    auth = TrustedReceipts()
    plan = plan_receipt(stage_receipt("s1"))
    with pytest.raises(LearningRecordError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
        derive(plan, plan.stages[0], auth)  # never registered with the executor's receipt store
    auth.register(plan)
    other = plan_receipt(stage_receipt("s1", duration_ms=9))
    with pytest.raises(LearningRecordError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
        derive(plan, other.stages[0], auth)
    forged = plan.model_copy(update={"human_decision_ref": "approval-1"})
    with pytest.raises(LearningRecordError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
        derive(forged, plan.stages[0], auth)  # caller's plan differs from the trusted envelope


# --- human decisions: only the governed verdict, never inferred --------------------------------

def test_human_decision_comes_only_from_the_governed_verdict() -> None:
    auth = TrustedReceipts()
    decided = auth.register(plan_receipt(stage_receipt(), human_decision_ref="approval-42"))
    pending = derive(decided, decided.stages[0], auth)
    assert pending.human_decision is HumanDecision.NONE and pending.human_decision_ref is None
    auth.decide("approval-42", HumanDecision.REJECTED)
    record = derive(decided, decided.stages[0], auth)
    assert record.human_decision is HumanDecision.REJECTED
    assert record.human_decision_ref == decision_ref_for("approval-42") != "approval-42"
    undecided = TrustedReceipts()
    plain = undecided.register(plan_receipt(stage_receipt()))
    assert derive(plain, plain.stages[0], undecided).human_decision is HumanDecision.NONE  # PASSED != accepted


def test_decision_without_reference_is_refused() -> None:
    data = outcome(1).model_dump()
    with pytest.raises(ValidationError, match="HUMAN_DECISION_REQUIRES_RECORDED_REF"):
        OutcomeRecord.model_validate({**data, "human_decision": "ACCEPTED"})
    with pytest.raises(ValidationError, match="HUMAN_DECISION_REQUIRES_RECORDED_REF"):
        OutcomeRecord.model_validate({**data, "human_decision_ref": decision_ref_for("approval-1")})
    with pytest.raises(ValidationError, match="HUMAN_DECISION_REF_NOT_A_DIGEST"):
        OutcomeRecord.model_validate({**data, "human_decision": "ACCEPTED", "human_decision_ref": "approval-1"})


# --- consistency ---------------------------------------------------------------------------

@pytest.mark.parametrize(("overrides", "code"), [
    ({"outcome_id": "oc-other"}, "OUTCOME_ID_NOT_DERIVED_FROM_RECEIPT"),
    ({"success": False}, "OUTCOME_SUCCESS_FAILURE_CODE_INCONSISTENT"),
    ({"failure_code": "TIMEOUT"}, "OUTCOME_SUCCESS_FAILURE_CODE_INCONSISTENT"),
    ({"success": False, "failure_code": "TIMEOUT"}, "FAILED_OUTCOME_CANNOT_PASS_VERIFICATION"),
    ({"recorded_at": "2026-10-01T12:00:00"}, "LEARNING_TIMESTAMP_NOT_AWARE"),
    ({"quality_metric": 1.5}, "less than or equal"),
    ({"cost_units": float("nan")}, "cost_units"),
    ({"latency_ms": -1}, "greater than or equal"),
    ({"receipt_hash": "abc"}, "LEARNING_HASH_INVALID"),
    ({"semantic_authority": "ACCEPTED"}, "Extra inputs"),
])
def test_records_are_internally_consistent(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(ValidationError, match=code):
        OutcomeRecord.model_validate({**outcome(1).model_dump(), **overrides})


def test_timestamps_are_normalized_to_utc() -> None:
    record = OutcomeRecord.model_validate({**outcome(0).model_dump(), "recorded_at": "2026-10-01T20:00:00+08:00"})
    assert record.recorded_at == "2026-10-01T12:00:00+00:00"


# --- privacy: no content, prompts, document text, free text or secrets -------------------------

@pytest.mark.parametrize("field", ["document_text", "prompt", "messages", "file_name", "page_text", "context",
                                   "task_type", "resource_id"])
def test_content_like_and_raw_identifier_fields_are_refused(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        OutcomeRecord.model_validate({**outcome(1).model_dump(), field: "some text from the document"})


@pytest.mark.parametrize("field", ["prompt", "messages", "document_text"])
def test_stored_lines_with_content_fields_are_refused(field: str) -> None:
    line = {**outcome(1).to_record(), field: "x"}
    with pytest.raises(DataMinimizationViolation):
        OutcomeRecord.from_record(line)


@pytest.mark.parametrize("features", [
    {"file_name": "contract.pdf"},
    {"document_language": "Traditional Chinese contract for ACME"},
    {"scan_type": "scanned"},
    {"document_language": "zh-Hant", "scan_type": "SCANNED", "extra_note": "x"},
    {f"k{i}": "UNKNOWN" for i in range(9)},
])
def test_context_features_are_a_closed_vocabulary(features: dict[str, str]) -> None:
    with pytest.raises(LearningRecordError):
        validate_context_features(features)
    with pytest.raises(ValidationError):
        OutcomeRecord.model_validate({**outcome(1).model_dump(), "context_features": features})


@pytest.mark.parametrize(("field", "value"), [
    ("failure_code", "invoice total was 1234 dollars"),
    ("failure_code", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("hardware_class", "s" + "k-" + "abcdef123456"),
    ("hardware_class", "Intel(R) Xeon(R) Gold 6338 CPU"),
    ("resource_ref", "node 1 at the office"),
    ("task_ref", "summarize this contract"),
    ("capability_id", "summarize this contract"),
])
def test_free_text_and_credential_shaped_values_are_refused(field: str, value: str) -> None:
    data = outcome(1, success=False).model_dump()
    with pytest.raises(ValidationError):
        OutcomeRecord.model_validate({**data, field: value})


def test_persisted_form_is_the_explicit_schema_only() -> None:
    record = outcome(3).to_record()
    assert set(record) == set(OUTCOME_KEYS)
    assert OutcomeRecord.from_record(record) == outcome(3)
    assert "semantic_authority" not in record


# --- context helpers ----------------------------------------------------------------------

def test_buckets_and_feature_helpers() -> None:
    features = validate_context_features(SCANNED_ZH)
    assert features == (("document_language", "zh-Hant"), ("mime_class", "PDF"), ("scan_type", "SCANNED"))
    assert context_bucket(features) == "document_language=zh-Hant;mime_class=PDF;scan_type=SCANNED"
    assert context_bucket(features, ("scan_type",)) == "scan_type=SCANNED"
    assert context_bucket(features, ()) == "*"
    assert [page_bucket(n) for n in (0, 1, 2, 10, 11, 200, 201)] == [
        "UNKNOWN", "P1", "P2_10", "P2_10", "P11_50", "P51_200", "P201_PLUS"]
    assert [size_bucket(n) for n in (-1, 0, 65536, 2 * 1024**2, 20 * 1024**2, 300 * 1024**2)] == [
        "UNKNOWN", "XS", "S", "M", "L", "XL"]
    assert all(page_bucket(n) in CONTEXT_FEATURES["page_bucket"] for n in (0, 5, 500))
    with pytest.raises(LearningRecordError, match="CONTEXT_FEATURE_DUPLICATE"):
        validate_context_features([("scan_type", "SCANNED"), ("scan_type", "PHOTO")])


# --- evaluation records ----------------------------------------------------------------------

def evaluation(**overrides: Any) -> EvaluationRecord:
    values: dict[str, Any] = dict(task_type="DOCUMENT_EXTRACTION", capability_id="DOCUMENT_OCR",
                                  adapter_id="ocr.engine-a", adapter_version="1",
                                  dataset_snapshot_hash=sha("dataset-v3"), dataset_size=250,
                                  metric_name="char_accuracy", metric_value=0.962, evaluator="bench-ocr-v1",
                                  evaluated_at=NOW, context_features=SCANNED_ZH, evidence_hash=sha("report"))
    values.update(overrides)
    return EvaluationRecord.create(**values)


def test_evaluation_records_are_bound_to_a_dataset_snapshot() -> None:
    record = evaluation()
    assert record.evaluation_id.startswith("ev-") and record.evaluation_id == evaluation().evaluation_id
    assert record.evaluator_ref.startswith("evr-") and record.task_ref.startswith("task-")
    assert evaluation(dataset_snapshot_hash=sha("dataset-v4")).evaluation_id != record.evaluation_id
    assert EvaluationRecord.from_record(record.to_record()) == record
    with pytest.raises(ValidationError, match="EVALUATION_ID_NOT_DERIVED"):
        EvaluationRecord.model_validate({**record.model_dump(), "metric_name": "word_accuracy"})
    with pytest.raises(LearningRecordError, match="METRIC_NAME_INVALID"):
        evaluation(metric_name="accuracy on the ACME invoices")
    with pytest.raises(ValidationError, match="LEARNING_HASH_INVALID"):
        evaluation(dataset_snapshot_hash="dataset-v3")
    with pytest.raises(ValidationError):
        EvaluationRecord.model_validate({**record.model_dump(), "sample_text": "x"})
    with pytest.raises(ValidationError):
        evaluation(evaluated_at=NOW.replace(tzinfo=None) - timedelta(days=1))
