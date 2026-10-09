from __future__ import annotations

import pytest

from pados.work_protocol.compatibility import ProtocolCompatibilityError, parse_protocol
from pados.work_protocol.models import ExecutionReceiptRef, ResultReceiptRef, TaskEnvelope
from tests.work_protocol.test_contracts import envelope_data


def test_future_incompatible_major_is_rejected() -> None:
    raw = envelope_data()
    raw["protocol_version"] = "2.0"
    with pytest.raises(ProtocolCompatibilityError, match="PROTOCOL_MAJOR_UNSUPPORTED"):
        parse_protocol(raw, TaskEnvelope)


def test_compatible_minor_and_unknown_additive_field_are_preserved() -> None:
    raw = {**envelope_data(), "protocol_version": "1.1", "future_display": "panorama"}

    parsed = parse_protocol(raw, TaskEnvelope)

    assert parsed.model_dump(mode="json")["future_display"] == "panorama"
    assert parsed.classification.value == "INTERNAL"
    assert parsed.project_ref == "project-1"
    assert parsed.scope_version_ref == "scope:7"


def test_missing_protocol_or_schema_version_is_rejected() -> None:
    raw = envelope_data()
    del raw["protocol_version"]
    with pytest.raises(ValueError):
        parse_protocol(raw, TaskEnvelope)

    raw = envelope_data()
    del raw["schema_version"]
    with pytest.raises(ValueError):
        parse_protocol(raw, TaskEnvelope)


def test_authorization_sensitive_additive_field_is_rejected() -> None:
    raw = envelope_data()
    raw["api" + "_key"] = "sk-" + ("x" * 32)
    with pytest.raises(ValueError):
        parse_protocol(raw, TaskEnvelope)


def test_execution_and_result_receipt_refs_are_independently_versioned() -> None:
    execution = parse_protocol(
        {
            "protocol_version": "1.0",
            "schema_version": "pados.execution-receipt-ref.v1",
            "receipt_id": "execution-1",
            "digest": "a" * 64,
        },
        ExecutionReceiptRef,
    )
    result = parse_protocol(
        {
            "protocol_version": "1.0",
            "schema_version": "pados.result-receipt-ref.v1",
            "receipt_id": "result-1",
            "candidate_id": "candidate-1",
            "task_id": "task-1",
            "project_ref": "project-1",
            "classification": "INTERNAL",
            "digest": "b" * 64,
            "scope_version_ref": "scope:7",
        },
        ResultReceiptRef,
    )

    assert execution.schema_version == "pados.execution-receipt-ref.v1"
    assert result.schema_version == "pados.result-receipt-ref.v1"
    with pytest.raises(ValueError):
        parse_protocol(
            {"schema_version": "pados.result-receipt-ref.v1", "receipt_id": "result-1"},
            ResultReceiptRef,
        )
