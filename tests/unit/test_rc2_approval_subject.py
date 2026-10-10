"""RC2 D1: pure tests of the WORK_ROUTE approval subject and its route matcher."""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from pados.control.approval_subject import (
    APPROVAL_ACTOR_MISMATCH,
    APPROVAL_PROJECT_MISMATCH,
    APPROVAL_REQUEST_MISMATCH,
    APPROVAL_SCOPE_MISMATCH,
    APPROVAL_SUBJECT_TAMPERED,
    APPROVAL_SUBJECT_UNBOUND,
    ApprovalOperation,
    WorkRouteSubject,
    load_bound_subject,
    match_route,
    scope_version_is_current,
    subject_hash,
)
from pados.control.schemas import ApprovalCreate
from pados.execution.context import canonical_hash
from pados.worker.schemas import CapabilityId, RiskLevel, RouteRequest

PROJECT = "11111111-1111-4111-8111-111111111111"
REQUEST = "22222222-2222-4222-8222-222222222222"


def _subject(**overrides: Any) -> WorkRouteSubject:
    value: dict[str, Any] = {
        "kind": "WORK_ROUTE",
        "schema_version": "pados.approval-subject.v1",
        "project_id": PROJECT,
        "request_id": REQUEST,
        "actor": {"surfaces": ["TASK_LOOP"], "worker_types": ["DETERMINISTIC_TOOL", "OLLAMA"]},
        "operations": [
            {"step_sequence": 1, "capability_id": "RUN_STATIC_ANALYSIS", "command_id": "python_version",
             "max_risk_level": "LOW"},
            {"step_sequence": 2, "capability_id": "SUMMARIZE", "command_id": None, "max_risk_level": "LOW"},
        ],
        "scope_version_ref": None,
        "task_envelope_hash": None,
    }
    value.update(overrides)
    return WorkRouteSubject.model_validate(value)


def _match(subject: WorkRouteSubject, **overrides: Any) -> str | None:
    route: dict[str, Any] = {
        "project_id": PROJECT,
        "request_id": REQUEST,
        "surface": "TASK_LOOP",
        "step_sequence": 1,
        "capability_id": CapabilityId.RUN_STATIC_ANALYSIS,
        "command_id": "python_version",
        "risk_level": RiskLevel.LOW,
    }
    route.update(overrides)
    return match_route(subject, **route)


def test_subject_hash_is_deterministic_and_content_bound() -> None:
    first = _subject()
    assert subject_hash(first) == subject_hash(_subject())
    assert subject_hash(first) == canonical_hash(first.model_dump(mode="json"))
    assert len(subject_hash(first)) == 64
    assert subject_hash(first) != subject_hash(_subject(request_id="33333333-3333-4333-8333-333333333333"))


def test_models_are_strict_and_frozen() -> None:
    with pytest.raises(ValidationError):
        _subject(extra_field=True)
    with pytest.raises(ValidationError):
        _subject(operations=[])
    with pytest.raises(ValidationError):
        _subject(actor={"surfaces": [], "worker_types": ["OLLAMA"]})
    with pytest.raises(ValidationError):
        _subject(actor={"surfaces": ["ANYONE"], "worker_types": ["OLLAMA"]})
    with pytest.raises(ValidationError):
        _subject(scope_version_ref="scope:latest")
    with pytest.raises(ValidationError):
        _subject(task_envelope_hash="A" * 64)
    with pytest.raises(ValidationError):
        ApprovalOperation(step_sequence=0, capability_id=CapabilityId.SUMMARIZE, max_risk_level=RiskLevel.LOW)
    with pytest.raises(ValidationError):
        _subject(operations=[{"capability_id": "DEPLOY", "max_risk_level": "LOW"}])
    subject = _subject()
    with pytest.raises(ValidationError):
        subject.request_id = "other"  # type: ignore[misc]


def test_matching_operations_are_authorized() -> None:
    subject = _subject()
    assert _match(subject) is None
    assert _match(subject, step_sequence=2, capability_id=CapabilityId.SUMMARIZE, command_id=None) is None


@pytest.mark.parametrize("capability", [CapabilityId.BUILD_RELEASE, CapabilityId.MODIFY_CODE])
def test_rc1_exploit_unapproved_capability_from_control_api_is_scope_mismatch(capability: CapabilityId) -> None:
    subject = _subject(actor={"surfaces": ["TASK_LOOP", "CONTROL_API"], "worker_types": ["DETERMINISTIC_TOOL"]})
    code = _match(subject, surface="CONTROL_API", step_sequence=None, capability_id=capability, command_id=None)
    assert code == APPROVAL_SCOPE_MISMATCH


def test_rc1_exploit_is_refused_even_when_command_is_reused() -> None:
    subject = _subject(actor={"surfaces": ["CONTROL_API"], "worker_types": ["DETERMINISTIC_TOOL"]})
    code = _match(subject, surface="CONTROL_API", step_sequence=None, capability_id=CapabilityId.BUILD_RELEASE)
    assert code == APPROVAL_SCOPE_MISMATCH


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"project_id": "99999999-9999-4999-8999-999999999999"}, APPROVAL_PROJECT_MISMATCH),
        ({"request_id": "99999999-9999-4999-8999-999999999999"}, APPROVAL_REQUEST_MISMATCH),
        ({"surface": "CONTROL_API"}, APPROVAL_ACTOR_MISMATCH),
        ({"step_sequence": None}, APPROVAL_SCOPE_MISMATCH),
        ({"step_sequence": 3}, APPROVAL_SCOPE_MISMATCH),
        ({"capability_id": CapabilityId.RUN_TESTS}, APPROVAL_SCOPE_MISMATCH),
        ({"command_id": "pytest_targeted"}, APPROVAL_SCOPE_MISMATCH),
        ({"command_id": None}, APPROVAL_SCOPE_MISMATCH),
        ({"risk_level": RiskLevel.MEDIUM}, APPROVAL_SCOPE_MISMATCH),
        ({"risk_level": RiskLevel.CRITICAL}, APPROVAL_SCOPE_MISMATCH),
    ],
)
def test_every_refusal_code(overrides: dict[str, Any], code: str) -> None:
    assert _match(_subject(), **overrides) == code


def test_step_two_approval_cannot_be_used_for_step_one() -> None:
    code = _match(_subject(), step_sequence=1, capability_id=CapabilityId.SUMMARIZE, command_id=None)
    assert code == APPROVAL_SCOPE_MISMATCH


def test_risk_at_or_below_the_approved_ceiling_is_allowed() -> None:
    subject = _subject(
        operations=[{"step_sequence": None, "capability_id": "SUMMARIZE", "max_risk_level": "MEDIUM"}],
        actor={"surfaces": ["CONTROL_API"], "worker_types": ["OLLAMA"]},
    )
    common = {"surface": "CONTROL_API", "step_sequence": None, "capability_id": CapabilityId.SUMMARIZE,
              "command_id": None}
    assert _match(subject, risk_level=RiskLevel.LOW, **common) is None
    assert _match(subject, risk_level=RiskLevel.MEDIUM, **common) is None
    assert _match(subject, risk_level=RiskLevel.HIGH, **common) == APPROVAL_SCOPE_MISMATCH


def test_load_bound_subject_fails_closed() -> None:
    subject = _subject()
    stored = subject.model_dump(mode="json")
    digest = subject_hash(subject)
    assert load_bound_subject("WORK_ROUTE", stored, digest) == subject
    for kind, value in ((None, None), (None, stored), ("WORK_ROUTE", None), ("OTHER", stored)):
        with pytest.raises(PermissionError, match=APPROVAL_SUBJECT_UNBOUND):
            load_bound_subject(kind, value, digest)
    tampered = {**stored, "operations": [*stored["operations"],
                                         {"step_sequence": None, "capability_id": "BUILD_RELEASE",
                                          "command_id": None, "max_risk_level": "CRITICAL"}]}
    with pytest.raises(PermissionError, match=APPROVAL_SUBJECT_TAMPERED):
        load_bound_subject("WORK_ROUTE", tampered, digest)
    invalid = {**stored, "unexpected": True}
    with pytest.raises(PermissionError, match=APPROVAL_SUBJECT_TAMPERED):
        load_bound_subject("WORK_ROUTE", invalid, canonical_hash(invalid))
    with pytest.raises(PermissionError, match=APPROVAL_SUBJECT_TAMPERED):
        load_bound_subject("WORK_ROUTE", "not-an-object", digest)


def test_scope_version_reference() -> None:
    assert scope_version_is_current(_subject(), None) is True
    pinned = _subject(scope_version_ref="scope:3")
    assert scope_version_is_current(pinned, 3) is True
    assert scope_version_is_current(pinned, 4) is False
    assert scope_version_is_current(pinned, None) is False


def test_approval_create_accepts_subject_without_hash_and_requires_one_of_them() -> None:
    created = ApprovalCreate(project_id=PROJECT, proposal_summary="s", idempotency_key="k",
                             subject=_subject())
    assert created.proposal_hash is None
    with pytest.raises(ValidationError):
        ApprovalCreate(project_id=PROJECT, proposal_summary="s", idempotency_key="k")
    legacy = ApprovalCreate(project_id=PROJECT, proposal_summary="s", idempotency_key="k", proposal_hash="a" * 64)
    assert legacy.subject is None


def test_route_request_defaults_to_control_api_without_step() -> None:
    route = RouteRequest(approval_id=REQUEST, capability_id=CapabilityId.SUMMARIZE, context_query="q",
                         idempotency_key="k")
    assert route.surface == "CONTROL_API"
    assert route.step_sequence is None
    with pytest.raises(ValidationError):
        RouteRequest(approval_id=REQUEST, capability_id=CapabilityId.SUMMARIZE, context_query="q",
                     idempotency_key="k", surface="ROOT")
