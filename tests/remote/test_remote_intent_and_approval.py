from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest

from pados.remote.approval import (
    PendingDecision,
    RemoteApprovalOutcome,
    apply_remote_decision,
    bind_remote_decision,
)
from pados.remote.context import RemoteContextReconstructor, duplicated_sources
from pados.remote.intent import (
    FORBIDDEN_CONTEXT_KEYS,
    RemoteIntentError,
    RemoteIntentType,
    parse_client_intent,
)
from pados.remote import live as remote_live
from pados.control.schemas import MAX_WORK_REQUEST_BODY_CHARS, WorkRequestCreate
from pados.enterprise.classification import DataClassification

ACTOR = "actor-remote-1"
SESSION = "session-remote-1"


def _start(**extra: Any) -> dict[str, Any]:
    return {
        "intent_id": "intent-1",
        "project_id": "proj-alpha",
        "intent_type": "START_TASK",
        "human_message": "summarise the latest accepted result",
        **extra,
    }


def test_start_task_envelope_is_intent_only() -> None:
    env = parse_client_intent(_start(), actor_id=ACTOR, session_id=SESSION)
    assert env.intent_type is RemoteIntentType.START_TASK
    assert env.to_dict()["protocol_version"] == "1.0"
    assert env.to_dict()["schema_version"] == "pados.remote-intent.v1"
    assert env.actor_id == ACTOR
    assert not set(env.to_dict()) & FORBIDDEN_CONTEXT_KEYS
    assert env.size_bytes < 1024


@pytest.mark.parametrize("key", sorted(FORBIDDEN_CONTEXT_KEYS))
def test_client_context_is_rejected(key: str) -> None:
    with pytest.raises(RemoteIntentError) as info:
        parse_client_intent(_start(**{key: "x"}), actor_id=ACTOR, session_id=SESSION)
    assert info.value.code == "CLIENT_CONTEXT_FORBIDDEN"


def test_client_cannot_assert_identity() -> None:
    with pytest.raises(RemoteIntentError) as info:
        parse_client_intent(_start(actor_id="someone"), actor_id=ACTOR, session_id=SESSION)
    assert info.value.code == "CLIENT_IDENTITY_FORBIDDEN"


def test_unknown_fields_and_oversized_messages_fail_closed() -> None:
    with pytest.raises(RemoteIntentError, match="UNKNOWN_FIELD"):
        parse_client_intent(_start(extra=1), actor_id=ACTOR, session_id=SESSION)
    with pytest.raises(RemoteIntentError, match="HUMAN_MESSAGE_TOO_LONG"):
        parse_client_intent(_start(human_message="x" * 2001), actor_id=ACTOR, session_id=SESSION)


def test_deadline_requires_timezone_and_roundtrips() -> None:
    with pytest.raises(RemoteIntentError, match="DEADLINE_TIMEZONE_REQUIRED"):
        parse_client_intent(
            _start(deadline="2026-10-06T08:00:00"), actor_id=ACTOR, session_id=SESSION
        )
    envelope = parse_client_intent(
        _start(deadline="2026-10-06T08:00:00+08:00"), actor_id=ACTOR, session_id=SESSION
    )
    assert envelope.to_dict()["deadline"] == "2026-10-06T08:00:00+08:00"


def test_remote_task_intake_persists_canonical_envelope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _Session:
        def __init__(self, _engine: object) -> None:
            pass

        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def begin(self) -> _Session:
            return self

    def create_work_request(_session: object, payload: dict[str, Any]) -> dict[str, str]:
        captured.update(payload)
        return {"id": "work-request-1"}

    monkeypatch.setattr(remote_live, "Session", _Session)
    monkeypatch.setattr(remote_live.control_service, "create_work_request", create_work_request)

    class _Ports(remote_live.LiveRemotePorts):
        def _project_classification(self, _project_id: str) -> DataClassification:
            return DataClassification.CONFIDENTIAL

    ports = _Ports(
        gateway=SimpleNamespace(),  # type: ignore[arg-type]
        operational_engine=object(),  # type: ignore[arg-type]
        presentation_dir=tmp_path,
    )
    intent = parse_client_intent(
        _start(
            requested_capability="ocr_text",
            priority="HIGH",
            deadline="2026-10-06T08:00:00+08:00",
        ),
        actor_id=ACTOR,
        session_id=SESSION,
    )
    context = SimpleNamespace(
        context=SimpleNamespace(scope_version=17), context_fingerprint="server-context-hash"
    )

    result = asyncio.run(ports.submit_task(intent, context))  # type: ignore[arg-type]
    body = json.loads(captured["body"])
    task = body["task_envelope"]
    assert result == "work-request-1"
    assert task["original_intent"] == intent.human_message
    assert task["classification"] == "CONFIDENTIAL"
    assert task["scope_version_ref"] == "scope:17"
    assert task["priority"] == "HIGH"
    assert task["deadline"] == "2026-10-06T08:00:00+08:00"
    assert task["normalized_goals"][0]["required_capabilities"] == ["OCR_TEXT"]
    assert body["task_envelope_hash"] == remote_live.TaskEnvelope.model_validate(task).content_hash
    assert body["remote_intent"]["actor_id"] == ACTOR
    assert body["pados_intent"]["protocol_version"] == "1.0"
    assert body["pados_intent"]["schema_version"] == "pados.remote-intent.v1"
    assert body["pados_intent"]["target_scope_version_ref"] == "scope:17"
    assert captured["idempotency_key"] == f"remote:{intent.intent_id}"


def test_attachment_only_task_preserves_structured_intent_without_empty_envelope() -> None:
    intent = parse_client_intent(
        {
            "intent_id": "intent-upload-1",
            "project_id": "proj-alpha",
            "intent_type": "UPLOAD_AND_INGEST",
            "attachment_refs": ["staged:upload-1"],
        },
        actor_id=ACTOR,
        session_id=SESSION,
    )
    context = SimpleNamespace(context=SimpleNamespace(scope_version=3))

    task = remote_live.task_envelope_for_intent(intent, context, DataClassification.INTERNAL)  # type: ignore[arg-type]

    assert task.original_intent == "UPLOAD_AND_INGEST"


def test_multilingual_task_intent_is_stored_without_truncation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _Session:
        def __init__(self, _engine: object) -> None:
            pass

        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def begin(self) -> _Session:
            return self

    def create_work_request(_session: object, payload: dict[str, Any]) -> dict[str, str]:
        captured.update(payload)
        return {"id": "work-request-unicode"}

    monkeypatch.setattr(remote_live, "Session", _Session)
    monkeypatch.setattr(remote_live.control_service, "create_work_request", create_work_request)

    class _Ports(remote_live.LiveRemotePorts):
        def _project_classification(self, _project_id: str) -> DataClassification:
            return DataClassification.INTERNAL

    ports = _Ports(
        gateway=SimpleNamespace(),  # type: ignore[arg-type]
        operational_engine=object(),  # type: ignore[arg-type]
        presentation_dir=tmp_path,
    )
    original_intent = "\u7814\u7a76\u6458\u8981" * 500
    intent = parse_client_intent(
        _start(human_message=original_intent), actor_id=ACTOR, session_id=SESSION
    )
    context = SimpleNamespace(
        context=SimpleNamespace(scope_version=17), context_fingerprint="bounded-context-hash"
    )

    asyncio.run(ports.submit_task(intent, context))  # type: ignore[arg-type]

    stored = json.loads(captured["body"])
    assert stored["remote_intent"]["human_message"] == original_intent
    assert stored["task_envelope"]["original_intent"] == original_intent
    assert stored["task_envelope"]["normalized_goals"][0]["objective"] == original_intent
    assert len(captured["body"]) > 8000
    assert len(captured["body"]) <= remote_live._MAX_CAPTURED_TASK_BODY_CHARS
    WorkRequestCreate.model_validate({
        "project_id": "proj-alpha",
        "title": "multilingual task",
        "body": captured["body"],
        "idempotency_key": "remote:intent-1",
    })
    assert MAX_WORK_REQUEST_BODY_CHARS == remote_live._MAX_CAPTURED_TASK_BODY_CHARS


def test_control_intents_require_target_and_attachments_must_be_staged() -> None:
    with pytest.raises(RemoteIntentError, match="TARGET_RUN_REQUIRED"):
        parse_client_intent(
            {"intent_id": "i2", "project_id": "p", "intent_type": "CANCEL"},
            actor_id=ACTOR,
            session_id=SESSION,
        )
    with pytest.raises(RemoteIntentError, match="ATTACHMENT_REF_NOT_STAGED"):
        parse_client_intent(
            _start(attachment_refs=["/etc/hosts"]), actor_id=ACTOR, session_id=SESSION
        )


def _semantic(version: int) -> dict[str, Any]:
    return {
        "active_goal": "ship remote ops",
        "open_blockers": [],
        "pending_decisions": [],
        "recent_accepted_results": [],
        "promoted_memory": [],
        "evidence": [],
        "scope_version": version,
    }


def test_context_is_reconstructed_server_side(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("project readme", encoding="utf-8")
    reconstructor = RemoteContextReconstructor(
        semantic_source=lambda pid: _semantic(3) if pid == "proj-alpha" else None,
        workspace_resolver=lambda pid: tmp_path,
    )
    env = parse_client_intent(_start(), actor_id=ACTOR, session_id=SESSION)
    rebuilt = reconstructor.reconstruct(env)
    summary = rebuilt.summary()
    assert summary["scope_version"] == 3
    assert summary["context_source"] == "PADOS_AUTHORITATIVE_STORE"
    assert summary["client_history_included"] is False
    assert summary["provider_history_included"] is False
    assert duplicated_sources(rebuilt.context) == []
    assert env.human_message not in "".join(s.text for s in rebuilt.context.sources)


def test_unregistered_project_fails_closed(tmp_path: Path) -> None:
    reconstructor = RemoteContextReconstructor(
        semantic_source=lambda pid: None, workspace_resolver=lambda pid: tmp_path
    )
    env = parse_client_intent(_start(), actor_id=ACTOR, session_id=SESSION)
    with pytest.raises(ValueError, match="PROJECT_NOT_REGISTERED"):
        reconstructor.reconstruct(env)


def _pending(version: int = 5, state: str = "ACCEPTED_PENDING_HUMAN_DECISION") -> PendingDecision:
    return PendingDecision(
        approval_ref="appr-1",
        run_id="run-1",
        candidate_receipt_id="rcpt-1",
        scope_id="proj-alpha",
        scope_version=version,
        manifest_hash="a" * 64,
        semantic_state=state,
    )


PROOF_REF = "step-up-proof:00000000-0000-4000-8000-000000000001"


def _decision(kind: str = "APPROVE") -> Any:
    env = parse_client_intent(
        {
            "intent_id": "i3",
            "project_id": "proj-alpha",
            "intent_type": kind,
            "target_run_id": "run-1",
            "approval_ref": "appr-1",
            "step_up_proof": PROOF_REF,
        },
        actor_id=ACTOR,
        session_id=SESSION,
    )
    return bind_remote_decision(
        env, _pending(), candidate_receipt_id="rcpt-1", proof_ref=PROOF_REF, step_up_binding_digest="b" * 64
    )


@pytest.mark.parametrize("kind", ["APPROVE", "REJECT"])
def test_decision_intent_requires_step_up_proof_reference(kind: str) -> None:
    payload = {"intent_id": "i9", "project_id": "proj-alpha", "intent_type": kind,
               "target_run_id": "run-1", "approval_ref": "appr-1"}
    with pytest.raises(RemoteIntentError, match="APPROVAL_STEP_UP_PROOF_REQUIRED"):
        parse_client_intent(payload, actor_id=ACTOR, session_id=SESSION)
    with pytest.raises(RemoteIntentError, match="STEP_UP_PROOF_NOT_APPLICABLE"):
        parse_client_intent(_start(step_up_proof=PROOF_REF), actor_id=ACTOR, session_id=SESSION)


class _Commit:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"status": "COMMITTED", "new_version": kwargs["expected_version"] + 1}


def _apply(binding: Any, current: PendingDecision, version: int, commit: _Commit) -> Any:
    return asyncio.run(
        apply_remote_decision(
            binding,
            current=current,
            current_scope_version=version,
            actor_may_approve=lambda actor, scope: actor == ACTOR,
            commit=commit,
        )
    )


def test_approval_commits_through_cas_with_bound_version() -> None:
    commit = _Commit()
    result = _apply(_decision(), _pending(), 5, commit)
    assert result.outcome is RemoteApprovalOutcome.COMMITTED
    bound = commit.calls[0].pop("binding")
    assert bound.proof_ref == PROOF_REF and bound.step_up_binding_digest == "b" * 64
    assert commit.calls == [
        {
            "scope_id": "proj-alpha",
            "expected_version": 5,
            "candidate_ref": "rcpt-1",
            "manifest_hash": "a" * 64,
            "approval_ref": "appr-1",
            "actor": ACTOR,
        }
    ]


def test_scope_version_change_fails_closed() -> None:
    commit = _Commit()
    result = _apply(_decision(), _pending(version=6), 6, commit)
    assert result.outcome is RemoteApprovalOutcome.SCOPE_VERSION_CHANGED
    assert commit.calls == []


def test_reject_never_commits_and_non_pending_is_refused() -> None:
    commit = _Commit()
    assert _apply(_decision("REJECT"), _pending(), 5, commit).outcome is RemoteApprovalOutcome.REJECTED
    assert (
        _apply(_decision(), _pending(state="COMMITTED"), 5, commit).outcome
        is RemoteApprovalOutcome.NOT_PENDING_HUMAN_DECISION
    )
    assert commit.calls == []


def test_commit_conflict_is_not_reported_as_success() -> None:
    class _Conflict(_Commit):
        async def __call__(self, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("RESULT_SCOPE_VERSION_MISMATCH")

    result = _apply(_decision(), _pending(), 5, _Conflict())
    assert result.outcome is RemoteApprovalOutcome.COMMIT_CONFLICT


def test_stale_candidate_from_cas_is_not_success() -> None:
    class _Stale(_Commit):
        async def __call__(self, **kwargs: Any) -> dict[str, Any]:
            return {"status": "STALE_CANDIDATE", "new_version": None}

    result = _apply(_decision(), _pending(), 5, _Stale())
    assert result.outcome is RemoteApprovalOutcome.COMMIT_CONFLICT
