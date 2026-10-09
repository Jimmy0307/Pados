# SPDX-License-Identifier: AGPL-3.0-only
"""Backend-governed fresh approval step-up.

A consequential decision (APPROVE / REJECT of a result) is never authorized by
the remote actor's standing credential alone. The flow is:

1. ``open_session`` — the authenticated actor obtains a server-issued session
   handle; only its SHA-256 is stored. The handle is binding context, never an
   authentication factor: every request still carries the actor credential.
2. ``issue_challenge`` — the server derives every binding field from PADOS
   authority (actor, session, project, task, goal, candidate id and digest,
   action, classification, expected ScopeVersion, approval ref) and stores a
   short-lived challenge with a fresh nonce.
3. ``verify_presence`` — the authenticator signs the nonce with user
   verification (WebAuthn). One attempt per challenge; success mints a
   single-use, short-lived proof reference.
4. ``consume`` — the decision path atomically consumes the proof after
   recomputing the binding from *current* authority. Any drift (candidate,
   digest, ScopeVersion, actor, session, action ...) or reuse is refused.

The client never chooses a trusted binding field; it only names the approval
ref, the action and, later, the challenge / proof references it was given.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import Session

from pados.execution.context import canonical_hash
from pados.remote.presence import (
    PresenceCredential,
    PresenceError,
    WebAuthnPresenceVerifier,
    b64url_decode,
    b64url_encode,
    user_handle_for,
)

STEP_UP_SCHEMA = "pados.approval-step-up.v1"
STEP_UP_PROTOCOL = "1.0"
CHALLENGE_TTL = timedelta(seconds=120)
PROOF_TTL = timedelta(seconds=60)
SESSION_TTL = timedelta(hours=8)
PROOF_PREFIX = "step-up-proof:"
SESSION_HEADER = "X-PADOS-Remote-Session"
ENROLLMENT_TTL = timedelta(seconds=180)
MAX_ACTIVE_CHALLENGES_PER_ACTOR = 16
MAX_ACTIVE_SESSIONS_PER_ACTOR = 32
_CLASSIFICATIONS = frozenset({"PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"})


class StepUpAction(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class StepUpError(PermissionError):
    """Step-up refused (fail closed); ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ApprovalTarget:
    """What is being decided, derived only from PADOS authority."""

    approval_ref: str
    kind: str  # PRODUCT_RUN | RESULT_CANDIDATE
    project_id: str
    task_id: str
    goal_id: str | None
    candidate_id: str
    candidate_digest: str
    classification: str
    expected_scope_version: int
    target_run_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"PRODUCT_RUN", "RESULT_CANDIDATE"}:
            raise StepUpError("STEP_UP_TARGET_KIND_INVALID")
        if self.classification not in _CLASSIFICATIONS:
            raise StepUpError("STEP_UP_TARGET_CLASSIFICATION_INVALID")
        if isinstance(self.expected_scope_version, bool) or self.expected_scope_version < 0:
            raise StepUpError("STEP_UP_TARGET_SCOPE_INVALID")
        for value in (self.approval_ref, self.project_id, self.task_id, self.candidate_id,
                      self.candidate_digest, self.target_run_id):
            if not isinstance(value, str) or not value or len(value) > 160:
                raise StepUpError("STEP_UP_TARGET_INVALID")


@dataclass(frozen=True)
class StepUpBinding:
    actor_id: str
    session_id: str
    action: StepUpAction
    target: ApprovalTarget
    protocol_version: str = STEP_UP_PROTOCOL
    schema_version: str = STEP_UP_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "session_id": self.session_id,
            "action": self.action.value,
            "target": asdict(self.target),
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
        }

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    actor_id: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ChallengeRecord:
    challenge_id: str
    nonce: str
    binding: StepUpBinding
    binding_digest: str
    issued_at: datetime
    expires_at: datetime
    status: str = "ISSUED"
    verified_at: datetime | None = None
    presence_method: str | None = None
    credential_id: str | None = None
    sign_count: int | None = None
    proof_id: str | None = None
    proof_expires_at: datetime | None = None
    proof_consumed_at: datetime | None = None
    # Public WebAuthn evidence (re-verifiable later against the enrolled public key).
    assertion_evidence: Mapping[str, str] | None = None

    @property
    def proof_ref(self) -> str | None:
        return None if self.proof_id is None else f"{PROOF_PREFIX}{self.proof_id}"


class StepUpStore(Protocol):
    def create_session(self, record: SessionRecord, handle_hash: str) -> None: ...
    def find_session(self, handle_hash: str) -> SessionRecord | None: ...
    def insert_challenge(self, record: ChallengeRecord) -> None: ...
    def get_challenge(self, challenge_id: str) -> ChallengeRecord | None: ...
    def get_by_proof(self, proof_id: str) -> ChallengeRecord | None: ...
    def fail_challenge(self, challenge_id: str) -> None: ...
    def mark_verified(self, *, challenge_id: str, credential_id: str, sign_count: int, method: str,
                      proof_id: str, proof_expires_at: datetime, now: datetime,
                      evidence: Mapping[str, str]) -> bool: ...
    def last_sign_count(self, credential_id: str) -> int: ...
    def count_active(self, *, actor_id: str, now: datetime) -> tuple[int, int]: ...
    def consume_proof(self, *, proof_id: str, binding_digest: str, now: datetime) -> bool: ...


def signed_challenge(nonce_b64: str, binding_digest: str) -> bytes:
    """The WebAuthn challenge: SHA-256(nonce || binding digest), so the signature commits to the binding."""
    return hashlib.sha256(b64url_decode(nonce_b64) + bytes.fromhex(binding_digest)).digest()


def assertion_digest(evidence: Mapping[str, str]) -> str:
    return canonical_hash(dict(evidence))


def _hash_handle(handle: str) -> str:
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()


def _proof_uuid(proof_ref: object) -> str:
    if not isinstance(proof_ref, str) or not proof_ref.startswith(PROOF_PREFIX):
        raise StepUpError("APPROVAL_STEP_UP_PROOF_REQUIRED")
    try:
        return str(UUID(proof_ref.removeprefix(PROOF_PREFIX)))
    except ValueError as exc:
        raise StepUpError("APPROVAL_PROOF_NOT_FOUND") from exc


class StepUpService:
    def __init__(
        self,
        store: StepUpStore,
        verifier: WebAuthnPresenceVerifier | None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.verifier = verifier
        self.clock = clock
        # Enrollment challenges never become authority (the operator enrols descriptors); kept per process.
        self._enrollments: dict[str, tuple[bytes, datetime]] = {}
        self._enrollment_lock = threading.Lock()

    # -- sessions -----------------------------------------------------------
    def open_session(self, actor_id: str) -> dict[str, Any]:
        now = self.clock()
        if self.store.count_active(actor_id=actor_id, now=now)[0] >= MAX_ACTIVE_SESSIONS_PER_ACTOR:
            raise StepUpError("REMOTE_SESSION_LIMIT_REACHED")
        handle = secrets.token_urlsafe(32)
        record = SessionRecord(
            session_id=f"rs-{secrets.token_hex(16)}",
            actor_id=actor_id,
            issued_at=now,
            expires_at=now + SESSION_TTL,
        )
        self.store.create_session(record, _hash_handle(handle))
        return {"session_handle": handle, "session_id": record.session_id,
                "expires_at": record.expires_at.isoformat(), "header": SESSION_HEADER}

    def resolve_session(self, actor_id: str, handle: str) -> str:
        if not isinstance(handle, str) or not 16 <= len(handle) <= 128:
            raise StepUpError("REMOTE_SESSION_INVALID")
        record = self.store.find_session(_hash_handle(handle))
        if record is None or record.actor_id != actor_id:
            raise StepUpError("REMOTE_SESSION_INVALID")
        if record.expires_at <= self.clock():
            raise StepUpError("REMOTE_SESSION_EXPIRED")
        return record.session_id

    # -- challenge ----------------------------------------------------------
    def issue_challenge(
        self, binding: StepUpBinding, credentials: Iterable[PresenceCredential]
    ) -> dict[str, Any]:
        verifier = self._verifier()
        registered = tuple(credentials)
        if not registered:
            raise StepUpError("APPROVAL_PRESENCE_CREDENTIAL_NOT_ENROLLED")
        now = self.clock()
        if self.store.count_active(actor_id=binding.actor_id, now=now)[1] >= MAX_ACTIVE_CHALLENGES_PER_ACTOR:
            raise StepUpError("APPROVAL_CHALLENGE_LIMIT_REACHED")
        nonce = secrets.token_bytes(32)
        record = ChallengeRecord(
            challenge_id=str(uuid4()),
            nonce=b64url_encode(nonce),
            binding=binding,
            binding_digest=binding.digest,
            issued_at=now,
            expires_at=now + CHALLENGE_TTL,
        )
        self.store.insert_challenge(record)
        return {
            "challenge_id": record.challenge_id,
            "issued_at": record.issued_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "binding_digest": record.binding_digest,
            "binding": _display_binding(binding),
            "public_key": verifier.request_options(
                signed_challenge(record.nonce, record.binding_digest), registered,
                timeout_ms=int(CHALLENGE_TTL.total_seconds() * 1000),
            ),
            "protocol_version": STEP_UP_PROTOCOL,
            "schema_version": STEP_UP_SCHEMA,
        }

    def verify_presence(
        self,
        *,
        challenge_id: str,
        actor_id: str,
        session_id: str,
        approval_ref: str,
        assertion: Mapping[str, Any],
        credentials: Iterable[PresenceCredential],
    ) -> dict[str, Any]:
        verifier = self._verifier()
        try:
            UUID(str(challenge_id))
        except ValueError as exc:
            raise StepUpError("APPROVAL_CHALLENGE_NOT_FOUND") from exc
        record = self.store.get_challenge(str(challenge_id))
        if record is None:
            raise StepUpError("APPROVAL_CHALLENGE_NOT_FOUND")
        if record.binding.actor_id != actor_id:
            raise StepUpError("APPROVAL_CHALLENGE_ACTOR_MISMATCH")
        if record.binding.session_id != session_id:
            raise StepUpError("APPROVAL_CHALLENGE_SESSION_MISMATCH")
        if record.binding.target.approval_ref != approval_ref:
            raise StepUpError("APPROVAL_CHALLENGE_TARGET_MISMATCH")
        if record.status != "ISSUED":
            raise StepUpError("APPROVAL_CHALLENGE_ALREADY_USED")
        now = self.clock()
        if record.expires_at <= now:
            self.store.fail_challenge(record.challenge_id)
            raise StepUpError("APPROVAL_CHALLENGE_EXPIRED")
        try:
            result = verifier.verify_assertion(
                assertion,
                challenge=signed_challenge(record.nonce, record.binding_digest),
                credentials=credentials,
                user_handle=user_handle_for(actor_id),
            )
        except PresenceError as exc:
            self.store.fail_challenge(record.challenge_id)  # one attempt per challenge
            raise StepUpError(f"APPROVAL_PRESENCE_{exc.code}") from exc
        # Once an authenticator has reported a counter, it must strictly increase (0 included).
        last = self.store.last_sign_count(result.credential_id)
        if last > 0 and result.sign_count <= last:
            self.store.fail_challenge(record.challenge_id)
            raise StepUpError("APPROVAL_PRESENCE_SIGN_COUNT_REGRESSION")
        evidence = {key: str(assertion[key]) for key in
                    ("credential_id", "client_data_json", "authenticator_data", "signature")}
        proof_id = str(uuid4())
        proof_expires = now + PROOF_TTL
        if not self.store.mark_verified(
            challenge_id=record.challenge_id,
            credential_id=result.credential_id,
            sign_count=result.sign_count,
            method=result.method,
            proof_id=proof_id,
            proof_expires_at=proof_expires,
            now=now,
            evidence=evidence,
        ):
            raise StepUpError("APPROVAL_CHALLENGE_ALREADY_USED")
        return {"proof_ref": f"{PROOF_PREFIX}{proof_id}", "expires_at": proof_expires.isoformat(),
                "binding_digest": record.binding_digest, "assertion_digest": assertion_digest(evidence),
                "single_use": True}

    # -- enrollment (descriptor only) ----------------------------------------
    def enrollment_challenge(self, actor_id: str) -> bytes:
        challenge = secrets.token_bytes(32)
        with self._enrollment_lock:
            now = self.clock()
            self._enrollments = {k: v for k, v in self._enrollments.items() if v[1] > now}
            self._enrollments[actor_id] = (challenge, now + ENROLLMENT_TTL)
        return challenge

    def take_enrollment_challenge(self, actor_id: str, presented: bytes) -> bytes:
        """Single-use, actor-bound, short-lived: the server's own challenge, never the client's."""
        with self._enrollment_lock:
            stored = self._enrollments.pop(actor_id, None)
        if stored is None or stored[1] <= self.clock() or not hmac.compare_digest(stored[0], presented):
            raise StepUpError("ENROLLMENT_CHALLENGE_INVALID")
        return stored[0]

    # -- consumption --------------------------------------------------------
    def consume(self, proof_ref: object, binding: StepUpBinding) -> ChallengeRecord:
        """Atomically consume a proof for exactly ``binding`` (recomputed from authority)."""
        proof_id = _proof_uuid(proof_ref)
        record = self.store.get_by_proof(proof_id)
        if record is None:
            raise StepUpError("APPROVAL_PROOF_NOT_FOUND")
        if record.proof_consumed_at is not None or record.status == "CONSUMED":
            raise StepUpError("APPROVAL_PROOF_REPLAYED")
        now = self.clock()
        if record.proof_expires_at is None or record.proof_expires_at <= now:
            raise StepUpError("APPROVAL_PROOF_EXPIRED")
        bound, current = record.binding, binding
        checks = (
            (bound.actor_id == current.actor_id, "APPROVAL_PROOF_ACTOR_MISMATCH"),
            (bound.session_id == current.session_id, "APPROVAL_PROOF_SESSION_MISMATCH"),
            (bound.action == current.action, "APPROVAL_PROOF_ACTION_MISMATCH"),
            (bound.target.project_id == current.target.project_id, "APPROVAL_PROOF_PROJECT_MISMATCH"),
            (bound.target.task_id == current.target.task_id, "APPROVAL_PROOF_TASK_MISMATCH"),
            (bound.target.goal_id == current.target.goal_id, "APPROVAL_PROOF_GOAL_MISMATCH"),
            (bound.target.approval_ref == current.target.approval_ref, "APPROVAL_PROOF_TARGET_MISMATCH"),
            (bound.target.candidate_id == current.target.candidate_id, "APPROVAL_PROOF_CANDIDATE_MISMATCH"),
            (bound.target.candidate_digest == current.target.candidate_digest,
             "APPROVAL_PROOF_CANDIDATE_DIGEST_MISMATCH"),
            (bound.target.classification == current.target.classification,
             "APPROVAL_PROOF_CLASSIFICATION_MISMATCH"),
            (bound.target.expected_scope_version == current.target.expected_scope_version,
             "APPROVAL_PROOF_SCOPE_VERSION_MISMATCH"),
            (bound.target == current.target and bound.digest == record.binding_digest
             and current.digest == record.binding_digest, "APPROVAL_PROOF_BINDING_MISMATCH"),
        )
        for ok, code in checks:
            if not ok:
                raise StepUpError(code)
        if not self.store.consume_proof(proof_id=proof_id, binding_digest=record.binding_digest, now=now):
            raise StepUpError("APPROVAL_PROOF_REPLAYED")  # lost the atomic race
        return replace(record, status="CONSUMED", proof_consumed_at=now)

    def _verifier(self) -> WebAuthnPresenceVerifier:
        if self.verifier is None:
            raise StepUpError("APPROVAL_PRESENCE_NOT_CONFIGURED")
        return self.verifier


def _display_binding(binding: StepUpBinding) -> dict[str, Any]:
    """What the human is shown next to the authenticator prompt."""
    target = binding.target
    return {
        "action": binding.action.value,
        "project_id": target.project_id,
        "task_id": target.task_id,
        "goal_id": target.goal_id,
        "candidate_id": target.candidate_id,
        "candidate_digest": target.candidate_digest,
        "classification": target.classification,
        "expected_scope_version": target.expected_scope_version,
        "kind": target.kind,
    }


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
class InMemoryStepUpStore:
    """Single-process store (tests / local simulation). Same atomic contract as Postgres."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionRecord] = {}
        self._challenges: dict[str, ChallengeRecord] = {}

    def create_session(self, record: SessionRecord, handle_hash: str) -> None:
        with self._lock:
            if handle_hash in self._sessions:
                raise StepUpError("REMOTE_SESSION_COLLISION")
            self._sessions[handle_hash] = record

    def find_session(self, handle_hash: str) -> SessionRecord | None:
        with self._lock:
            return self._sessions.get(handle_hash)

    def insert_challenge(self, record: ChallengeRecord) -> None:
        with self._lock:
            if record.challenge_id in self._challenges or any(
                item.nonce == record.nonce for item in self._challenges.values()
            ):
                raise StepUpError("APPROVAL_CHALLENGE_COLLISION")
            self._challenges[record.challenge_id] = record

    def get_challenge(self, challenge_id: str) -> ChallengeRecord | None:
        with self._lock:
            return self._challenges.get(challenge_id)

    def get_by_proof(self, proof_id: str) -> ChallengeRecord | None:
        with self._lock:
            return next((item for item in self._challenges.values() if item.proof_id == proof_id), None)

    def fail_challenge(self, challenge_id: str) -> None:
        with self._lock:
            record = self._challenges.get(challenge_id)
            if record is not None and record.status == "ISSUED":
                self._challenges[challenge_id] = replace(record, status="FAILED")

    def mark_verified(self, *, challenge_id: str, credential_id: str, sign_count: int, method: str,
                      proof_id: str, proof_expires_at: datetime, now: datetime,
                      evidence: Mapping[str, str]) -> bool:
        with self._lock:
            record = self._challenges.get(challenge_id)
            if record is None or record.status != "ISSUED" or record.expires_at <= now:
                return False
            self._challenges[challenge_id] = replace(
                record, status="VERIFIED", verified_at=now, presence_method=method,
                credential_id=credential_id, sign_count=sign_count, proof_id=proof_id,
                proof_expires_at=proof_expires_at, assertion_evidence=dict(evidence),
            )
            return True

    def last_sign_count(self, credential_id: str) -> int:
        with self._lock:
            return max((item.sign_count or 0 for item in self._challenges.values()
                        if item.credential_id == credential_id), default=0)

    def count_active(self, *, actor_id: str, now: datetime) -> tuple[int, int]:
        with self._lock:
            sessions = sum(1 for item in self._sessions.values() if item.actor_id == actor_id and item.expires_at > now)
            challenges = sum(1 for item in self._challenges.values() if item.binding.actor_id == actor_id
                             and item.status == "ISSUED" and item.expires_at > now)
            return sessions, challenges

    def consume_proof(self, *, proof_id: str, binding_digest: str, now: datetime) -> bool:
        with self._lock:
            for key, record in self._challenges.items():
                if record.proof_id != proof_id:
                    continue
                if (record.status != "VERIFIED" or record.proof_consumed_at is not None
                        or record.proof_expires_at is None or record.proof_expires_at <= now
                        or record.binding_digest != binding_digest):
                    return False
                self._challenges[key] = replace(record, status="CONSUMED", proof_consumed_at=now)
                return True
            return False


class PostgresStepUpStore:
    """Operational-database store; uniqueness and single use are enforced by the schema."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create_session(self, record: SessionRecord, handle_hash: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO remote_step_up_sessions(session_id, handle_hash, actor_id, issued_at, expires_at)
                    VALUES (:session_id, :handle_hash, :actor_id, :issued_at, :expires_at)
                    """
                ),
                {"session_id": record.session_id, "handle_hash": handle_hash, "actor_id": record.actor_id,
                 "issued_at": record.issued_at, "expires_at": record.expires_at},
            )

    def find_session(self, handle_hash: str) -> SessionRecord | None:
        with Session(self.engine) as session:
            row = session.execute(
                text("SELECT * FROM remote_step_up_sessions WHERE handle_hash = :h"), {"h": handle_hash}
            ).mappings().first()
        if row is None:
            return None
        return SessionRecord(str(row["session_id"]), str(row["actor_id"]), row["issued_at"], row["expires_at"])

    def insert_challenge(self, record: ChallengeRecord) -> None:
        target = record.binding.target
        with Session(self.engine) as session, session.begin():
            session.execute(
                text(
                    """
                    INSERT INTO approval_step_up_challenges(
                        challenge_id, nonce, actor_id, session_id, approval_ref, kind, project_id,
                        task_id, goal_id, candidate_id, candidate_digest, action, classification,
                        expected_scope_version, target_run_id, binding_digest, protocol_version,
                        schema_version, issued_at, expires_at, status)
                    VALUES (
                        CAST(:challenge_id AS uuid), :nonce, :actor_id, :session_id, :approval_ref, :kind,
                        :project_id, :task_id, :goal_id, :candidate_id, :candidate_digest, :action,
                        :classification, :expected_scope_version, :target_run_id, :binding_digest,
                        :protocol_version, :schema_version, :issued_at, :expires_at, 'ISSUED')
                    """
                ),
                {
                    "challenge_id": record.challenge_id, "nonce": record.nonce,
                    "actor_id": record.binding.actor_id, "session_id": record.binding.session_id,
                    "approval_ref": target.approval_ref, "kind": target.kind,
                    "project_id": target.project_id, "task_id": target.task_id, "goal_id": target.goal_id,
                    "candidate_id": target.candidate_id, "candidate_digest": target.candidate_digest,
                    "action": record.binding.action.value, "classification": target.classification,
                    "expected_scope_version": target.expected_scope_version,
                    "target_run_id": target.target_run_id, "binding_digest": record.binding_digest,
                    "protocol_version": record.binding.protocol_version,
                    "schema_version": record.binding.schema_version,
                    "issued_at": record.issued_at, "expires_at": record.expires_at,
                },
            )

    @staticmethod
    def _record(row: Mapping[str, Any]) -> ChallengeRecord:
        binding = StepUpBinding(
            actor_id=str(row["actor_id"]),
            session_id=str(row["session_id"]),
            action=StepUpAction(str(row["action"])),
            target=ApprovalTarget(
                approval_ref=str(row["approval_ref"]), kind=str(row["kind"]),
                project_id=str(row["project_id"]), task_id=str(row["task_id"]),
                goal_id=None if row["goal_id"] is None else str(row["goal_id"]),
                candidate_id=str(row["candidate_id"]), candidate_digest=str(row["candidate_digest"]),
                classification=str(row["classification"]),
                expected_scope_version=int(row["expected_scope_version"]),
                target_run_id=str(row["target_run_id"]),
            ),
            protocol_version=str(row["protocol_version"]),
            schema_version=str(row["schema_version"]),
        )
        return ChallengeRecord(
            challenge_id=str(row["challenge_id"]), nonce=str(row["nonce"]), binding=binding,
            binding_digest=str(row["binding_digest"]), issued_at=row["issued_at"],
            expires_at=row["expires_at"], status=str(row["status"]), verified_at=row["verified_at"],
            presence_method=row["presence_method"], credential_id=row["credential_id"],
            sign_count=None if row["sign_count"] is None else int(row["sign_count"]),
            proof_id=None if row["proof_id"] is None else str(row["proof_id"]),
            proof_expires_at=row["proof_expires_at"], proof_consumed_at=row["proof_consumed_at"],
            assertion_evidence=None if row["assertion_signature"] is None else {
                "credential_id": str(row["credential_id"]),
                "client_data_json": str(row["assertion_client_data"]),
                "authenticator_data": str(row["assertion_authenticator_data"]),
                "signature": str(row["assertion_signature"]),
            },
        )

    def get_challenge(self, challenge_id: str) -> ChallengeRecord | None:
        with Session(self.engine) as session:
            row = session.execute(
                text("SELECT * FROM approval_step_up_challenges WHERE challenge_id = CAST(:id AS uuid)"),
                {"id": challenge_id},
            ).mappings().first()
        return None if row is None else self._record(dict(row))

    def get_by_proof(self, proof_id: str) -> ChallengeRecord | None:
        with Session(self.engine) as session:
            row = session.execute(
                text("SELECT * FROM approval_step_up_challenges WHERE proof_id = CAST(:id AS uuid)"),
                {"id": proof_id},
            ).mappings().first()
        return None if row is None else self._record(dict(row))

    def fail_challenge(self, challenge_id: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                text(
                    "UPDATE approval_step_up_challenges SET status = 'FAILED' "
                    "WHERE challenge_id = CAST(:id AS uuid) AND status = 'ISSUED'"
                ),
                {"id": challenge_id},
            )

    def mark_verified(self, *, challenge_id: str, credential_id: str, sign_count: int, method: str,
                      proof_id: str, proof_expires_at: datetime, now: datetime,
                      evidence: Mapping[str, str]) -> bool:
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                text(
                    """
                    UPDATE approval_step_up_challenges
                    SET status = 'VERIFIED', verified_at = :now, presence_method = :method,
                        credential_id = :credential_id, sign_count = :sign_count,
                        proof_id = CAST(:proof_id AS uuid), proof_expires_at = :proof_expires_at,
                        assertion_client_data = :client_data, assertion_authenticator_data = :auth_data,
                        assertion_signature = :signature, assertion_digest = :assertion_digest
                    WHERE challenge_id = CAST(:id AS uuid) AND status = 'ISSUED' AND expires_at > :now
                    """
                ),
                {"id": challenge_id, "now": now, "method": method, "credential_id": credential_id,
                 "sign_count": sign_count, "proof_id": proof_id, "proof_expires_at": proof_expires_at,
                 "client_data": evidence["client_data_json"], "auth_data": evidence["authenticator_data"],
                 "signature": evidence["signature"], "assertion_digest": assertion_digest(evidence)},
            )
            return int(getattr(result, "rowcount", 0)) == 1

    def last_sign_count(self, credential_id: str) -> int:
        with Session(self.engine) as session:
            value = session.execute(
                text("SELECT COALESCE(max(sign_count), 0) FROM approval_step_up_challenges "
                     "WHERE credential_id = :c"),
                {"c": credential_id},
            ).scalar_one()
        return int(value)

    def count_active(self, *, actor_id: str, now: datetime) -> tuple[int, int]:
        with Session(self.engine) as session:
            row = session.execute(
                text(
                    """
                    SELECT (SELECT count(*) FROM remote_step_up_sessions
                            WHERE actor_id = :a AND expires_at > :now) AS sessions,
                           (SELECT count(*) FROM approval_step_up_challenges
                            WHERE actor_id = :a AND status = 'ISSUED' AND expires_at > :now) AS challenges
                    """
                ),
                {"a": actor_id, "now": now},
            ).one()
        return int(row[0]), int(row[1])

    def consume_proof(self, *, proof_id: str, binding_digest: str, now: datetime) -> bool:
        with Session(self.engine) as session, session.begin():
            result: CursorResult[Any] = session.execute(  # type: ignore[assignment]
                text(
                    """
                    UPDATE approval_step_up_challenges
                    SET status = 'CONSUMED', proof_consumed_at = :now
                    WHERE proof_id = CAST(:proof_id AS uuid) AND status = 'VERIFIED'
                      AND proof_consumed_at IS NULL AND proof_expires_at > :now
                      AND binding_digest = :binding_digest
                    """
                ),
                {"proof_id": proof_id, "now": now, "binding_digest": binding_digest},
            )
            return int(result.rowcount) == 1
