# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.core.task_service import create_project
from pados.memory import repository
from pados.memory.security import assert_no_secret, validate_payload, validate_summary
from pados.models.context_memory import (
    FreshnessStatus,
    MemoryType,
    SourceTrustLevel,
    VerificationStatus,
)

VALID_EVENT_PAYLOAD_KEYS: dict[str, set[str]] = {
    "RUN_CREATED": {"summary"},
    "CONTEXT_REQUESTED": {"purpose", "token_budget"},
    "CONTEXT_ASSEMBLED": {"packet_id", "estimated_tokens"},
    "WORKER_STARTED": {"worker_type"},
    "OBSERVATION_RECORDED": {"title", "summary"},
    "FILE_CHANGED": {"path", "sha256"},
    "TEST_EXECUTED": {"command", "log_path"},
    "TEST_PASSED": {"command", "exit_code", "log_path"},
    "TEST_FAILED": {"command", "exit_code", "log_path"},
    "ARTIFACT_CREATED": {"artifact_id", "path", "sha256"},
    "DECISION_REQUIRED": {"question"},
    "DECISION_RECORDED": {"decision_id", "status"},
    "STATE_UPDATED": {"from", "to"},
    "RUN_COMPLETED": {"summary"},
    "RUN_FAILED": {"error"},
}

ORIGIN_TYPES = {"HUMAN", "MODEL", "AGENT", "SYSTEM", "IMPORT", "LEGACY", "UNKNOWN"}
ACTOR_TYPES = ORIGIN_TYPES
AUTHORITY_STATES = {
    "CANDIDATE",
    "PROMOTED",
    "HUMAN_CONFIRMED",
    "CONFLICT_CANDIDATE",
    "SUPERSEDED",
    "REJECTED",
}
EVIDENCE_STATUSES = {"PROVIDED", "EVIDENCE_UNAVAILABLE", "UNKNOWN"}


def _enum_value(value: str, allowed: type[Any]) -> str:
    members = {member.value for member in allowed}
    if value not in members:
        raise ValueError(f"invalid enum value: {value}")
    return value


def _assert_registered_path(session: Session, *, project_id: str, path: Path) -> Path:
    project_root = repository.get_project_root(session, project_id)
    resolved = path.resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise PermissionError("path outside registered project boundary")
    return resolved


def register_project(
    session: Session,
    *,
    project_id: str,
    canonical_root: Path,
    classification: str = "internal",
) -> None:
    resolved = canonical_root.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError("canonical_root must exist and be a directory")
    repository.register_context_project(
        session,
        project_id=project_id,
        canonical_root=resolved,
        classification=classification,
    )
    repository.register_source(
        session,
        project_id=project_id,
        source_type="project",
        source_uri=f"project:{project_id}",
        canonical_path=str(resolved),
        source_hash=None,
        verification_status=VerificationStatus.VERIFIED.value,
        freshness_status=FreshnessStatus.CURRENT.value,
        trust_level=SourceTrustLevel.CANONICAL.value,
        metadata={"classification": classification},
    )


def bootstrap_project(
    session: Session,
    *,
    name: str,
    root_path: Path,
    classification: str = "internal",
    default_branch: str = "main",
) -> str:
    """Create a project and its context registry in one transaction."""

    if not name.strip():
        raise ValueError("project name must not be empty")
    if not default_branch.strip():
        raise ValueError("default branch must not be empty")
    resolved = root_path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError("root_path must exist and be a directory")
    existing_rows = session.execute(
        text("SELECT id FROM projects WHERE lower(name) = lower(:name)"),
        {"name": name},
    ).mappings().all()
    if existing_rows:
        raise ValueError("duplicate project identity rejected")
    existing_root = session.execute(
        text("SELECT project_id FROM context_project_registry WHERE canonical_root = :root_path"),
        {"root_path": str(resolved)},
    ).scalar()
    if existing_root is not None:
        raise ValueError("duplicate canonical root rejected")
    project_id = create_project(
        session,
        name=name,
        root_path=str(resolved),
        default_branch=default_branch,
        classification=classification,
        actor="pados-cli",
    )
    register_project(
        session,
        project_id=project_id,
        canonical_root=resolved,
        classification=classification,
    )
    return project_id


def register_markdown_source(
    session: Session,
    *,
    project_id: str,
    path: Path,
    verification_status: str = VerificationStatus.VERIFIED.value,
    trust_level: str = SourceTrustLevel.CANONICAL.value,
) -> str:
    resolved = _assert_registered_path(session, project_id=project_id, path=path)
    if resolved.suffix.lower() != ".md":
        raise ValueError("only selected Markdown sources are supported")
    payload = resolved.read_text(encoding="utf-8")
    assert_no_secret(payload, field_name="markdown_source")
    return repository.register_source(
        session,
        project_id=project_id,
        source_type="markdown",
        source_uri=resolved.as_posix(),
        canonical_path=str(resolved),
        source_hash=repository.source_hash_for_text(payload),
        verification_status=verification_status,
        freshness_status=FreshnessStatus.CURRENT.value,
        trust_level=trust_level,
    )


def add_memory_record(
    session: Session,
    *,
    project_id: str,
    memory_type: str,
    title: str,
    content_summary: str,
    source_type: str,
    source_uri: str,
    source_hash: str | None = None,
    source_trust_level: str = SourceTrustLevel.OBSERVED.value,
    verification_status: str = VerificationStatus.UNVERIFIED.value,
    freshness_status: str = FreshnessStatus.CURRENT.value,
    importance: int = 50,
    retrieval_priority: int = 50,
    supersedes_memory_id: str | None = None,
    origin_type: str = "UNKNOWN",
    actor_type: str | None = None,
    actor_id: str | None = None,
    run_id: str | None = None,
    result_ref: str | None = None,
    evidence_ref: str | None = None,
    evidence_status: str = "EVIDENCE_UNAVAILABLE",
    provider: str | None = None,
    model: str | None = None,
    runtime: str | None = None,
    contract_id: str | None = None,
    operation_key: str | None = None,
) -> str:
    _enum_value(memory_type, MemoryType)
    _enum_value(source_trust_level, SourceTrustLevel)
    _enum_value(verification_status, VerificationStatus)
    _enum_value(freshness_status, FreshnessStatus)
    if origin_type not in ORIGIN_TYPES:
        raise ValueError(f"invalid origin_type: {origin_type}")
    if actor_type is not None and actor_type not in ACTOR_TYPES:
        raise ValueError(f"invalid actor_type: {actor_type}")
    if evidence_status not in EVIDENCE_STATUSES:
        raise ValueError(f"invalid evidence_status: {evidence_status}")
    if verification_status != VerificationStatus.UNVERIFIED.value:
        raise ValueError("memory writes create candidates; use explicit promotion for authority")
    if source_trust_level in {
        SourceTrustLevel.CANONICAL.value,
        SourceTrustLevel.VERIFIED_GENERATED.value,
        SourceTrustLevel.USER_CONFIRMED.value,
    }:
        raise ValueError("promoted-looking trust requires explicit promotion")
    if origin_type == "HUMAN" and not actor_id:
        raise ValueError("human-origin candidates require actor_id")
    if origin_type in {"MODEL", "AGENT"} and (not provider or not model or not (run_id or result_ref)):
        raise ValueError("machine-origin candidates require provider, model, and run_id or result_ref")
    validate_summary(content_summary)
    if not 0 <= importance <= 100 or not 0 <= retrieval_priority <= 100:
        raise ValueError("importance and retrieval_priority must be between 0 and 100")
    if operation_key is not None:
        existing = repository.get_authority_event_by_operation(session, operation_key=operation_key)
        if existing is not None:
            if str(existing["project_id"]) != project_id:
                raise ValueError("operation_key reuse across projects blocked")
            return str(existing["memory_id"])
    effective_trust = (
        SourceTrustLevel.MODEL_INFERRED.value
        if origin_type in {"MODEL", "AGENT"}
        else source_trust_level
    )
    memory_id = repository.insert_memory(
        session,
        project_id=project_id,
        memory_type=memory_type,
        title=title,
        content_summary=content_summary,
        source_type=source_type,
        source_uri=source_uri,
        source_hash=source_hash,
        source_trust_level=effective_trust,
        verification_status=VerificationStatus.UNVERIFIED.value,
        freshness_status=freshness_status,
        importance=importance,
        retrieval_priority=retrieval_priority,
        supersedes_memory_id=supersedes_memory_id,
    )
    repository.insert_authority_event(
        session,
        memory_id=memory_id,
        project_id=project_id,
        version=1,
        authority_state="CANDIDATE",
        origin_type=origin_type,
        origin_actor_type=actor_type or origin_type,
        origin_actor_id=actor_id,
        actor_type=actor_type or origin_type,
        actor_id=actor_id,
        source_ref=source_uri,
        evidence_ref=evidence_ref,
        evidence_status=evidence_status,
        result_ref=result_ref,
        run_id=run_id,
        provider=provider,
        model=model,
        runtime=runtime,
        contract_id=contract_id,
        promotion_gate=None,
        promotion_reason=None,
        supersedes_memory_id=supersedes_memory_id,
        superseded_by_memory_id=None,
        previous_event_id=None,
        operation_key=operation_key or f"candidate:{memory_id}",
    )
    return memory_id


def _authority_provenance(current: dict[str, Any]) -> dict[str, Any]:
    return {
        "origin_type": str(current["origin_type"]),
        "origin_actor_type": str(current["origin_actor_type"])
        if current["origin_actor_type"] is not None
        else None,
        "origin_actor_id": str(current["origin_actor_id"])
        if current["origin_actor_id"] is not None
        else None,
        "source_ref": str(current["source_ref"]) if current["source_ref"] is not None else None,
        "evidence_ref": str(current["evidence_ref"]) if current["evidence_ref"] is not None else None,
        "evidence_status": str(current["evidence_status"]),
        "result_ref": str(current["result_ref"]) if current["result_ref"] is not None else None,
        "run_id": str(current["run_id"]) if current["run_id"] is not None else None,
        "provider": str(current["provider"]) if current["provider"] is not None else None,
        "model": str(current["model"]) if current["model"] is not None else None,
        "runtime": str(current["runtime"]) if current["runtime"] is not None else None,
        "contract_id": str(current["contract_id"]) if current["contract_id"] is not None else None,
        "supersedes_memory_id": str(current["supersedes_memory_id"])
        if current["supersedes_memory_id"] is not None
        else None,
    }


def _validate_promotion_request(
    *,
    authority_state: str,
    actor_type: str,
    actor_id: str | None,
    promotion_gate: str | None,
    evidence_ref: str | None,
    evidence_status: str,
    source_ref: str | None,
    result_ref: str | None,
) -> None:
    if authority_state not in {"PROMOTED", "HUMAN_CONFIRMED"}:
        raise ValueError("promotion authority_state must be PROMOTED or HUMAN_CONFIRMED")
    if actor_type not in ACTOR_TYPES or actor_type in {"MODEL", "AGENT", "LEGACY", "UNKNOWN"}:
        raise ValueError("promotion actor_type must be an explicit non-model actor")
    if not actor_id:
        raise ValueError("promotion requires actor_id")
    if not promotion_gate:
        raise ValueError("promotion requires promotion_gate")
    if evidence_status not in EVIDENCE_STATUSES:
        raise ValueError(f"invalid evidence_status: {evidence_status}")
    if not (evidence_ref or source_ref or result_ref or evidence_status == "EVIDENCE_UNAVAILABLE"):
        raise ValueError("promotion requires source/evidence/result reference or EVIDENCE_UNAVAILABLE")
    if authority_state == "HUMAN_CONFIRMED" and actor_type != "HUMAN":
        raise ValueError("HUMAN_CONFIRMED promotion requires HUMAN actor_type")


def promote_memory(
    session: Session,
    *,
    memory_id: str,
    project_id: str,
    expected_authority_version: int,
    authority_state: str = "PROMOTED",
    actor_type: str,
    actor_id: str,
    promotion_gate: str,
    evidence_ref: str | None = None,
    evidence_status: str = "UNKNOWN",
    result_ref: str | None = None,
    promotion_reason: str | None = None,
    operation_key: str | None = None,
    expected_superseded_version: int | None = None,
) -> dict[str, Any]:
    if operation_key is not None:
        existing = repository.get_authority_event_by_operation(session, operation_key=operation_key)
        if existing is not None:
            if str(existing["project_id"]) != project_id or str(existing["memory_id"]) != memory_id:
                raise ValueError("operation_key reuse for another memory blocked")
            return {
                "memory_id": str(existing["memory_id"]),
                "authority_event_id": str(existing["id"]),
                "authority_state": str(existing["authority_state"]),
                "authority_version": int(existing["version"]),
                "context_eligible": str(existing["authority_state"])
                in {"PROMOTED", "HUMAN_CONFIRMED"},
            }
    current = repository.get_current_authority(session, memory_id=memory_id, lock=True)
    if current is None:
        raise ValueError("LEGACY_MEMORY_NOT_PROMOTABLE")
    if str(current["project_id"]) != project_id:
        raise PermissionError("cross-project memory promotion blocked")
    if int(current["version"]) != expected_authority_version:
        raise ValueError("STALE_CANDIDATE")
    if str(current["authority_state"]) != "CANDIDATE":
        raise ValueError("PROMOTION_CONFLICT")
    _validate_promotion_request(
        authority_state=authority_state,
        actor_type=actor_type,
        actor_id=actor_id,
        promotion_gate=promotion_gate,
        evidence_ref=evidence_ref,
        evidence_status=evidence_status,
        source_ref=str(current["source_ref"]) if current["source_ref"] is not None else None,
        result_ref=result_ref or (str(current["result_ref"]) if current["result_ref"] is not None else None),
    )
    provenance = _authority_provenance(current)
    if evidence_ref is not None:
        provenance["evidence_ref"] = evidence_ref
    if result_ref is not None:
        provenance["result_ref"] = result_ref
    if evidence_status != "UNKNOWN":
        provenance["evidence_status"] = evidence_status
    event = repository.insert_authority_event(
        session,
        memory_id=memory_id,
        project_id=project_id,
        version=expected_authority_version + 1,
        authority_state=authority_state,
        origin_type=provenance["origin_type"],
        origin_actor_type=provenance["origin_actor_type"],
        origin_actor_id=provenance["origin_actor_id"],
        actor_type=actor_type,
        actor_id=actor_id,
        source_ref=provenance["source_ref"],
        evidence_ref=provenance["evidence_ref"],
        evidence_status=provenance["evidence_status"],
        result_ref=provenance["result_ref"],
        run_id=provenance["run_id"],
        provider=provenance["provider"],
        model=provenance["model"],
        runtime=provenance["runtime"],
        contract_id=provenance["contract_id"],
        promotion_gate=promotion_gate,
        promotion_reason=promotion_reason,
        supersedes_memory_id=provenance["supersedes_memory_id"],
        superseded_by_memory_id=None,
        previous_event_id=str(current["id"]),
        operation_key=operation_key or f"promotion:{memory_id}:{expected_authority_version + 1}",
    )
    repository.update_memory_compatibility(
        session,
        memory_id=memory_id,
        source_trust_level=(
            SourceTrustLevel.USER_CONFIRMED.value
            if authority_state == "HUMAN_CONFIRMED"
            else SourceTrustLevel.VERIFIED_GENERATED.value
        ),
        verification_status=VerificationStatus.VERIFIED.value,
    )
    superseded_memory_id = provenance["supersedes_memory_id"]
    if superseded_memory_id is not None:
        previous = repository.get_current_authority(session, memory_id=superseded_memory_id, lock=True)
        if previous is None:
            raise ValueError("SUPERSEDES_LEGACY_MEMORY_NOT_ALLOWED")
        if expected_superseded_version is not None and int(previous["version"]) != expected_superseded_version:
            raise ValueError("STALE_SUPERSEDED_MEMORY")
        if str(previous["authority_state"]) not in {
            "PROMOTED",
            "HUMAN_CONFIRMED",
            "CONFLICT_CANDIDATE",
        }:
            raise ValueError("SUPERSEDED_MEMORY_NOT_CURRENT_AUTHORITY")
        previous_provenance = _authority_provenance(previous)
        repository.insert_authority_event(
            session,
            memory_id=superseded_memory_id,
            project_id=project_id,
            version=int(previous["version"]) + 1,
            authority_state="SUPERSEDED",
            origin_type=previous_provenance["origin_type"],
            origin_actor_type=previous_provenance["origin_actor_type"],
            origin_actor_id=previous_provenance["origin_actor_id"],
            actor_type=actor_type,
            actor_id=actor_id,
            source_ref=previous_provenance["source_ref"],
            evidence_ref=previous_provenance["evidence_ref"],
            evidence_status=previous_provenance["evidence_status"],
            result_ref=previous_provenance["result_ref"],
            run_id=previous_provenance["run_id"],
            provider=previous_provenance["provider"],
            model=previous_provenance["model"],
            runtime=previous_provenance["runtime"],
            contract_id=previous_provenance["contract_id"],
            promotion_gate=promotion_gate,
            promotion_reason=promotion_reason or "superseded by newer semantic version",
            supersedes_memory_id=None,
            superseded_by_memory_id=memory_id,
            previous_event_id=str(previous["id"]),
            operation_key=f"supersede:{superseded_memory_id}:{memory_id}",
        )
        repository.update_memory_compatibility(
            session,
            memory_id=superseded_memory_id,
            verification_status=VerificationStatus.DEPRECATED.value,
        )
        repository.insert_relationship(
            session,
            project_id=project_id,
            from_memory_id=memory_id,
            to_memory_id=superseded_memory_id,
            relationship_type="SUPERSEDES",
        )
    return {
        "memory_id": memory_id,
        "authority_event_id": str(event["id"]),
        "authority_state": authority_state,
        "authority_version": int(event["version"]),
        "context_eligible": True,
    }


def reject_memory(
    session: Session,
    *,
    memory_id: str,
    project_id: str,
    expected_authority_version: int,
    actor_type: str,
    actor_id: str,
    promotion_gate: str,
    reason: str,
    operation_key: str | None = None,
) -> dict[str, Any]:
    if actor_type not in ACTOR_TYPES or actor_type in {"MODEL", "AGENT", "LEGACY", "UNKNOWN"} or not actor_id:
        raise ValueError("rejection requires an explicit non-model actor")
    if not promotion_gate or not reason:
        raise ValueError("rejection requires gate and reason")
    current = repository.get_current_authority(session, memory_id=memory_id, lock=True)
    if current is None:
        raise ValueError("LEGACY_MEMORY_NOT_REJECTABLE")
    if str(current["project_id"]) != project_id:
        raise PermissionError("cross-project memory rejection blocked")
    if int(current["version"]) != expected_authority_version:
        raise ValueError("STALE_CANDIDATE")
    if str(current["authority_state"]) != "CANDIDATE":
        raise ValueError("REJECTION_CONFLICT")
    provenance = _authority_provenance(current)
    event = repository.insert_authority_event(
        session,
        memory_id=memory_id,
        project_id=project_id,
        version=expected_authority_version + 1,
        authority_state="REJECTED",
        origin_type=provenance["origin_type"],
        origin_actor_type=provenance["origin_actor_type"],
        origin_actor_id=provenance["origin_actor_id"],
        actor_type=actor_type,
        actor_id=actor_id,
        source_ref=provenance["source_ref"],
        evidence_ref=provenance["evidence_ref"],
        evidence_status=provenance["evidence_status"],
        result_ref=provenance["result_ref"],
        run_id=provenance["run_id"],
        provider=provenance["provider"],
        model=provenance["model"],
        runtime=provenance["runtime"],
        contract_id=provenance["contract_id"],
        promotion_gate=promotion_gate,
        promotion_reason=reason,
        supersedes_memory_id=provenance["supersedes_memory_id"],
        superseded_by_memory_id=None,
        previous_event_id=str(current["id"]),
        operation_key=operation_key or f"reject:{memory_id}:{expected_authority_version + 1}",
    )
    repository.update_memory_compatibility(
        session,
        memory_id=memory_id,
        verification_status=VerificationStatus.REJECTED.value,
    )
    return {
        "memory_id": memory_id,
        "authority_event_id": str(event["id"]),
        "authority_state": "REJECTED",
        "authority_version": int(event["version"]),
        "context_eligible": False,
    }


def mark_conflict(
    session: Session,
    *,
    project_id: str,
    first_memory_id: str,
    second_memory_id: str,
    evidence_uri: str | None = None,
) -> None:
    for memory_id in (first_memory_id, second_memory_id):
        current = repository.get_current_authority(session, memory_id=memory_id, lock=True)
        if current is None:
            raise ValueError("LEGACY_MEMORY_CONFLICT_STATE_UNAVAILABLE")
        provenance = _authority_provenance(current)
        repository.insert_authority_event(
            session,
            memory_id=memory_id,
            project_id=project_id,
            version=int(current["version"]) + 1,
            authority_state="CONFLICT_CANDIDATE",
            origin_type=provenance["origin_type"],
            origin_actor_type=provenance["origin_actor_type"],
            origin_actor_id=provenance["origin_actor_id"],
            actor_type="SYSTEM",
            actor_id="memory-service",
            source_ref=provenance["source_ref"],
            evidence_ref=evidence_uri or provenance["evidence_ref"],
            evidence_status="PROVIDED" if evidence_uri else provenance["evidence_status"],
            result_ref=provenance["result_ref"],
            run_id=provenance["run_id"],
            provider=provenance["provider"],
            model=provenance["model"],
            runtime=provenance["runtime"],
            contract_id=provenance["contract_id"],
            promotion_gate=f"conflict:{first_memory_id}:{second_memory_id}",
            promotion_reason="conflicting memory versions",
            supersedes_memory_id=provenance["supersedes_memory_id"],
            superseded_by_memory_id=None,
            previous_event_id=str(current["id"]),
            operation_key=f"conflict:{first_memory_id}:{second_memory_id}:{memory_id}",
        )
        repository.update_memory_compatibility(
            session,
            memory_id=memory_id,
            verification_status=VerificationStatus.CONFLICTED.value,
        )
    repository.insert_relationship(
        session,
        project_id=project_id,
        from_memory_id=first_memory_id,
        to_memory_id=second_memory_id,
        relationship_type="CONFLICTS_WITH",
        evidence_uri=evidence_uri,
    )


def mark_memory_stale(session: Session, *, memory_id: str) -> None:
    repository.update_memory_status(
        session,
        memory_id=memory_id,
        freshness_status=FreshnessStatus.STALE.value,
    )


def deprecate_memory(session: Session, *, memory_id: str) -> None:
    current = repository.get_current_authority(session, memory_id=memory_id, lock=True)
    if current is None:
        raise ValueError("LEGACY_MEMORY_DEPRECATION_REQUIRES_REVIEW")
    provenance = _authority_provenance(current)
    repository.insert_authority_event(
        session,
        memory_id=memory_id,
        project_id=str(current["project_id"]),
        version=int(current["version"]) + 1,
        authority_state="SUPERSEDED",
        origin_type=provenance["origin_type"],
        origin_actor_type=provenance["origin_actor_type"],
        origin_actor_id=provenance["origin_actor_id"],
        actor_type="SYSTEM",
        actor_id="memory-service",
        source_ref=provenance["source_ref"],
        evidence_ref=provenance["evidence_ref"],
        evidence_status=provenance["evidence_status"],
        result_ref=provenance["result_ref"],
        run_id=provenance["run_id"],
        provider=provenance["provider"],
        model=provenance["model"],
        runtime=provenance["runtime"],
        contract_id=provenance["contract_id"],
        promotion_gate="memory-deprecate",
        promotion_reason="explicit deprecation",
        supersedes_memory_id=None,
        superseded_by_memory_id=None,
        previous_event_id=str(current["id"]),
        operation_key=f"deprecate:{memory_id}:{int(current['version']) + 1}",
    )
    repository.update_memory_compatibility(
        session,
        memory_id=memory_id,
        verification_status=VerificationStatus.DEPRECATED.value,
    )


def create_run(
    session: Session,
    *,
    project_id: str,
    run_type: str,
    worker_type: str,
) -> str:
    run_id = repository.create_run(
        session,
        project_id=project_id,
        run_type=run_type,
        worker_type=worker_type,
    )
    append_run_event(
        session,
        run_id=run_id,
        project_id=project_id,
        event_type="RUN_CREATED",
        payload_json={"summary": f"{run_type} run created"},
    )
    return run_id


def append_run_event(
    session: Session,
    *,
    run_id: str,
    project_id: str,
    event_type: str,
    payload_json: dict[str, Any],
    evidence_uri: str | None = None,
) -> str:
    if event_type not in VALID_EVENT_PAYLOAD_KEYS:
        allowed = ", ".join(sorted(VALID_EVENT_PAYLOAD_KEYS))
        raise ValueError(f"invalid event_type: {event_type}; allowed values: {allowed}")
    if not isinstance(payload_json, dict):
        raise ValueError("payload must be a JSON object")  # noqa: TRY004 - API maps ValueError to 409
    allowed_keys = VALID_EVENT_PAYLOAD_KEYS[event_type]
    received_keys = set(payload_json)
    if not received_keys.issubset(allowed_keys):
        raise ValueError(
            "invalid event payload schema; "
            f"event type: {event_type}; "
            f"allowed keys: {', '.join(sorted(allowed_keys)) or '(none)'}; "
            f"received keys: {', '.join(sorted(received_keys)) or '(none)'}"
        )
    validate_payload(payload_json)
    return repository.append_event(
        session,
        run_id=run_id,
        project_id=project_id,
        event_type=event_type,
        payload_json=payload_json,
        evidence_uri=evidence_uri,
    )


def record_decision(
    session: Session,
    *,
    project_id: str,
    run_id: str | None,
    decision_type: str,
    question: str,
    decision: str,
    rationale: str,
    status: str,
    decided_by: str | None = None,
    evidence_uri: str | None = None,
    supersedes_decision_id: str | None = None,
) -> str:
    for field_name, value in {
        "question": question,
        "decision": decision,
        "rationale": rationale,
    }.items():
        assert_no_secret(value, field_name=field_name)
    decision_id = repository.insert_decision(
        session,
        project_id=project_id,
        run_id=run_id,
        decision_type=decision_type,
        question=question,
        decision=decision,
        rationale=rationale,
        status=status,
        decided_by=decided_by,
        evidence_uri=evidence_uri,
        supersedes_decision_id=supersedes_decision_id,
    )
    memory_id = add_memory_record(
        session,
        project_id=project_id,
        memory_type=MemoryType.DECISION.value,
        title=question,
        content_summary=f"{decision}\nRationale: {rationale}",
        source_type="decision",
        source_uri=f"decision:{decision_id}",
        source_trust_level=SourceTrustLevel.OBSERVED.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        freshness_status=FreshnessStatus.CURRENT.value,
        importance=80,
        retrieval_priority=80,
        origin_type="HUMAN" if decided_by else "UNKNOWN",
        actor_type="HUMAN" if decided_by else "UNKNOWN",
        actor_id=decided_by,
        run_id=run_id,
        evidence_ref=evidence_uri,
        evidence_status="PROVIDED" if evidence_uri else "EVIDENCE_UNAVAILABLE",
        operation_key=f"decision-memory:{decision_id}",
    )
    if status == "APPROVED":
        if not decided_by:
            raise ValueError("APPROVED decision requires decided_by for human confirmation")
        promote_memory(
            session,
            memory_id=memory_id,
            project_id=project_id,
            expected_authority_version=1,
            authority_state="HUMAN_CONFIRMED",
            actor_type="HUMAN",
            actor_id=decided_by,
            promotion_gate=f"decision:{decision_id}",
            evidence_ref=evidence_uri,
            evidence_status="PROVIDED" if evidence_uri else "EVIDENCE_UNAVAILABLE",
            promotion_reason=rationale,
            operation_key=f"decision-promotion:{decision_id}",
        )
    if run_id is not None:
        append_run_event(
            session,
            run_id=run_id,
            project_id=project_id,
            event_type="DECISION_RECORDED",
            payload_json={"decision_id": decision_id, "status": status},
            evidence_uri=evidence_uri,
        )
    return decision_id


def register_artifact(
    session: Session,
    *,
    project_id: str,
    path: Path,
    artifact_type: str,
    run_id: str | None = None,
    media_type: str = "application/octet-stream",
) -> str:
    resolved = _assert_registered_path(session, project_id=project_id, path=path)
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    digest = repository.sha256_file(resolved)
    artifact_id = repository.insert_artifact(
        session,
        project_id=project_id,
        run_id=run_id,
        artifact_type=artifact_type,
        canonical_path=str(resolved),
        sha256=digest,
        size_bytes=resolved.stat().st_size,
        media_type=media_type,
        verification_status=VerificationStatus.VERIFIED.value,
    )
    memory_id = add_memory_record(
        session,
        project_id=project_id,
        memory_type=MemoryType.ARTIFACT.value,
        title=resolved.name,
        content_summary=f"Artifact pointer: {resolved.name} sha256={digest}",
        source_type="artifact",
        source_uri=f"artifact:{artifact_id}",
        source_hash=digest,
        source_trust_level=SourceTrustLevel.OBSERVED.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        freshness_status=FreshnessStatus.CURRENT.value,
        importance=60,
        retrieval_priority=60,
        origin_type="IMPORT",
        actor_type="IMPORT",
        actor_id="artifact-register",
        run_id=run_id,
        evidence_status="EVIDENCE_UNAVAILABLE",
        operation_key=f"artifact-memory:{artifact_id}",
    )
    promote_memory(
        session,
        memory_id=memory_id,
        project_id=project_id,
        expected_authority_version=1,
        authority_state="PROMOTED",
        actor_type="SYSTEM",
        actor_id="artifact-register",
        promotion_gate=f"artifact:{artifact_id}",
        evidence_status="EVIDENCE_UNAVAILABLE",
        promotion_reason="verified artifact hash registered as reusable artifact memory",
        operation_key=f"artifact-promotion:{artifact_id}",
    )
    if run_id is not None:
        append_run_event(
            session,
            run_id=run_id,
            project_id=project_id,
            event_type="ARTIFACT_CREATED",
            payload_json={"artifact_id": artifact_id, "path": str(resolved), "sha256": digest},
        )
    return artifact_id


def validate_artifact_integrity(session: Session, *, artifact_id: str) -> str:
    row = session.execute(
        text(
            """
            SELECT canonical_path, sha256 FROM memory_artifacts
            WHERE id = CAST(:artifact_id AS uuid)
            """
        ),
        {"artifact_id": artifact_id},
    ).mappings().one()
    path = Path(str(row["canonical_path"]))
    if not path.exists():
        return "MISSING_ARTIFACT"
    actual = repository.sha256_file(path)
    if actual.lower() != str(row["sha256"]).lower():
        return "HASH_MISMATCH"
    return "VERIFIED"
