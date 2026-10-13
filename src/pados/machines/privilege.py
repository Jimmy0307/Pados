# SPDX-License-Identifier: AGPL-3.0-only
"""Privileged machine operations: authenticated two-person grant, bound request, recorded.

``TERMINAL``, ``SHELL``, ``ADMIN`` and ``FILE_TRANSFER`` are
``PRIVILEGED_OPERATION``s. They are not remote intents: an ordinary Remote
Operations session (``pados.remote.auth.RemoteSession`` or its ``RemoteActor``)
can never request, approve, satisfy or execute one
(``REMOTE_SESSION_CANNOT_GRANT_PRIVILEGED_OPERATION``).

Flow (every step recorded in the grant ledger):

1. ``OperatorAuthenticator.challenge(request)`` - a PADOS-MAC'd challenge bound
   to the full ``GrantRequest`` digest, machine, project and expiry.
2. ``issue_grant`` - verifies the requester's and the approver's HMAC proofs
   itself (a caller-built ``LocalOperator`` is never accepted as proof), requires
   two different operators unless the inventory is ``LOCAL_SINGLE_USER``, uses
   each challenge once, bounds the lifetime (15 minutes; 5 for ``break_glass``,
   which is flagged and never relaxes approval) and records ``ISSUED`` with
   operator ids and proof digests only.
3. ``authorize_privileged`` - compares machine, operation, project, command and
   the request digest (so the validated arguments) with the grant, checks
   expiry, profile, machine state and the registered command schema, and
   consumes the grant once (``CONSUMED``). ``SHELL``/``ADMIN`` need a registered
   ``PrivilegedCommand``; none is registered by default, so no shell is exposed.
4. ``consume_authorization`` - the execution boundary: the executed request must
   have the authorization's digest, and each authorization executes exactly
   once (``EXECUTED``).

The ledger is append-only JSONL. Each entry is hash-chained and carries an HMAC
under the Home Base key (SecretRef), so ``ISSUED`` records cannot be fabricated
without the key; a separate owner-only head file holds (count, last entry hash,
MAC) and detects truncation or rollback of ``CONSUMED``/``EXECUTED`` records.
Both files are opened descriptor-based (no link following) and must be regular,
owned by the current user, mode 0600, in a parent that is not group/world
writable. Records pass ``assert_minimized`` with an explicit schema and never
contain command text, argument values, terminal output or scrollback.
ACCEPTED_RISK: an attacker holding both files *and* the Home Base key can
rewrite the ledger consistently.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.compute_node.identity_providers import SecurityProfile
from pados.execution.context import canonical_hash
from pados.lifecycle.fswalk import fsync_directory
from pados.lifecycle.locking import ensure_durable_dir, exclusive_lock
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.machines.errors import MachineError
from pados.machines.fsguard import open_owner_only, read_owner_only, replace_owner_only
from pados.machines.identity import MACHINE_ID_PATTERN
from pados.machines.inventory import MachineInventory, utc_now
from pados.machines.keys import Mac, SecretKey
from pados.machines.operators import Challenge, LocalOperator, OperatorAuthenticator, ProofRole
from pados.machines.profile import ConnectionState, guard_input, validated
from pados.machines.requests import (
    COMMAND_OPERATIONS,
    IDENTIFIER,
    PRIVILEGED_OPERATIONS,
    GrantRequest,
    MachineOperation,
    OperationClass,
    operation_class,
    request_for,
)
from pados.remote.auth import RemoteActor, RemoteSession

__all__ = [
    "COMMAND_OPERATIONS", "NO_PRIVILEGED_COMMANDS", "PRIVILEGED_OPERATIONS", "REMOTE_REFUSED", "GrantEvent",
    "GrantLedger", "GrantRequest", "LocalOperator", "MachineOperation", "OperationClass", "PrivilegedAuthorization",
    "PrivilegedCommand", "PrivilegedGrant", "authorize_privileged", "consume_authorization", "issue_grant",
    "operation_class", "request_for",
]

logger = logging.getLogger("pados.machines")
REMOTE_REFUSED = "REMOTE_SESSION_CANNOT_GRANT_PRIVILEGED_OPERATION"
MAX_GRANT_LIFETIME = timedelta(minutes=15)
MAX_BREAK_GLASS_LIFETIME = timedelta(minutes=5)
GENESIS = "0" * 64
GRANT_LEDGER_SCHEMA = "pados.machines.privileged-grant-ledger.v2"
GRANT_HEAD_SCHEMA = "pados.machines.privileged-grant-ledger-head.v1"
ENTRY_DOMAIN = "pados-grant-ledger-entry-v1"
HEAD_DOMAIN = "pados-grant-ledger-head-v1"
_REASON = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{8,200}$")
_HEX64 = r"^[0-9a-f]{64}$"


@dataclass(frozen=True)
class PrivilegedCommand:
    command_id: str
    operation: MachineOperation
    allowed_args: frozenset[str] = frozenset()


NO_PRIVILEGED_COMMANDS: Mapping[str, PrivilegedCommand] = MappingProxyType({})


class PrivilegedGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str = Field(pattern=r"^pg-[0-9a-f]{20}$")
    actor: str
    approver: str
    machine_id: str = Field(pattern=MACHINE_ID_PATTERN)
    operation: MachineOperation
    project_id: str
    command_id: str | None = None
    request_digest: str = Field(pattern=_HEX64)
    challenge_id: str = Field(pattern=r"^ch-[0-9a-f]{32}$")
    requester_proof_digest: str = Field(pattern=_HEX64)
    approver_proof_digest: str = Field(pattern=_HEX64)
    reason: str
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    break_glass: bool = False
    security_profile: SecurityProfile

    @model_validator(mode="before")
    @classmethod
    def minimized(cls, data: Any) -> Any:
        return guard_input(data, GRANT_KEYS, max_string=256)

    @field_validator("actor", "approver", "project_id", "command_id")
    @classmethod
    def identifier(cls, value: str | None) -> str | None:
        if value is not None and not IDENTIFIER.fullmatch(value):
            raise MachineError("PRIVILEGED_GRANT_IDENTIFIER_INVALID")
        return value

    @field_validator("reason")
    @classmethod
    def plain_reason(cls, value: str) -> str:
        if not _REASON.fullmatch(value):
            raise MachineError("PRIVILEGED_GRANT_REASON_INVALID")
        return value

    @model_validator(mode="after")
    def bounded(self) -> PrivilegedGrant:
        if self.operation not in PRIVILEGED_OPERATIONS:
            raise MachineError("MACHINE_OPERATION_NOT_PRIVILEGED")
        lifetime = self.expires_at - self.issued_at
        limit = MAX_BREAK_GLASS_LIFETIME if self.break_glass else MAX_GRANT_LIFETIME
        if not timedelta(0) < lifetime <= limit:
            raise MachineError("PRIVILEGED_GRANT_LIFETIME_INVALID")
        if self.approver == self.actor and self.security_profile is not SecurityProfile.LOCAL_SINGLE_USER:
            raise MachineError("PRIVILEGED_GRANT_SELF_APPROVAL_FORBIDDEN")
        if (self.operation in COMMAND_OPERATIONS) != (self.command_id is not None):
            raise MachineError("PRIVILEGED_GRANT_COMMAND_SCOPE_INVALID")
        return self

    @property
    def grant_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


GRANT_KEYS = frozenset(PrivilegedGrant.model_fields)


class GrantEvent(StrEnum):
    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    EXECUTED = "EXECUTED"
    DENIED = "DENIED"


GRANT_RECORD_KEYS = frozenset({
    "schema", "event", "grant_id", "grant_hash", "authorization_id", "request_digest", "challenge_id", "actor",
    "approver", "requester_proof_digest", "approver_proof_digest", "machine_id", "operation", "project_id",
    "command_id", "break_glass", "security_profile", "issued_at", "expires_at", "reason", "recorded_at",
    "decision_code",
})
_ENTRY_KEYS = frozenset({"prev_hash", "record", "entry_hash", "entry_mac"})
_HEAD_KEYS = frozenset({"schema", "count", "last_entry_hash", "head_mac"})


def _record(event: GrantEvent, *, recorded_at: datetime, grant: PrivilegedGrant | None = None,
            **fields: Any) -> dict[str, Any]:
    data = grant.model_dump(mode="json") if grant is not None else {}
    record: dict[str, Any] = {key: data.get(key) for key in GRANT_RECORD_KEYS}
    record.update(schema=GRANT_LEDGER_SCHEMA, event=event.value, recorded_at=recorded_at.isoformat(),
                  grant_hash=grant.grant_hash if grant is not None else None,
                  break_glass=bool(data.get("break_glass", False)))
    if event is not GrantEvent.ISSUED:
        record["reason"] = None
    record.update(fields)
    return record


def _validate_record(record: Mapping[str, Any]) -> None:
    if not isinstance(record, Mapping) or set(record) != GRANT_RECORD_KEYS:
        raise MachineError("GRANT_LEDGER_RECORD_INVALID")
    if record["schema"] != GRANT_LEDGER_SCHEMA or record["event"] not in set(GrantEvent):
        raise MachineError("GRANT_LEDGER_RECORD_INVALID")
    try:
        assert_minimized(record, allowed_keys=GRANT_RECORD_KEYS, max_string=256, max_items=32,
                         max_total_bytes=8192)
    except DataMinimizationViolation:
        raise MachineError("GRANT_LEDGER_RECORD_INVALID") from None


class GrantLedger:
    """Append-only, hash-chained, HMAC-authenticated grant ledger with a rollback-detecting head."""

    def __init__(self, path: str | Path, *, home_key: SecretKey, root: str | Path | None = None) -> None:
        """``root`` is the trusted authority directory (e.g. the inventory directory): every ancestor of
        the ledger up to it must be a real, owner/root-owned, not group/world-writable directory."""
        self.path = Path(path)
        self.root = Path(root) if root is not None else None
        self.head_path = self.path.with_name(self.path.name + ".head")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._key = home_key
        ensure_durable_dir(self.path.parent)

    def _head_payload(self, count: object, last: object) -> dict[str, Any]:
        return {"ledger": self.path.name, "count": count, "last_entry_hash": last}

    def _verified(self, mac: Mac) -> list[dict[str, Any]]:
        raw = read_owner_only(self.path, "GRANT_LEDGER", self.root)
        head_raw = read_owner_only(self.head_path, "GRANT_LEDGER_HEAD", self.root)
        entries: list[dict[str, Any]] = []
        prev = GENESIS
        failed = False
        try:
            for line in (raw or b"").decode("utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
                    failed = True
                    break
                body = {"prev_hash": entry["prev_hash"], "record": entry["record"]}
                if (entry["prev_hash"] != prev or canonical_hash(body) != entry["entry_hash"]
                        or not hmac.compare_digest(mac(ENTRY_DOMAIN, {"entry_hash": entry["entry_hash"]}),
                                                   entry["entry_mac"])):
                    failed = True
                    break
                _validate_record(entry["record"])
                prev = str(entry["entry_hash"])
                entries.append(entry)
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            failed = True
        if failed:
            raise MachineError("GRANT_LEDGER_CHAIN_INVALID")
        self._check_head(mac, head_raw, len(entries), prev)
        return entries

    def _check_head(self, mac: Mac, head_raw: bytes | None, count: int, last: str) -> None:
        if head_raw is None:
            if count:
                raise MachineError("GRANT_LEDGER_HEAD_MISSING")
            return
        try:
            head = json.loads(head_raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise MachineError("GRANT_LEDGER_HEAD_INVALID") from None
        if not isinstance(head, dict) or set(head) != _HEAD_KEYS or head["schema"] != GRANT_HEAD_SCHEMA:
            raise MachineError("GRANT_LEDGER_HEAD_INVALID")
        expected = mac(HEAD_DOMAIN, self._head_payload(head["count"], head["last_entry_hash"]))
        if not isinstance(head["head_mac"], str) or not hmac.compare_digest(expected, head["head_mac"]):
            raise MachineError("GRANT_LEDGER_HEAD_INVALID")
        if (head["count"], head["last_entry_hash"]) != (count, last):
            raise MachineError("GRANT_LEDGER_ROLLBACK_DETECTED")

    @contextmanager
    def _locked(self) -> Iterator[tuple[Mac, list[dict[str, Any]]]]:
        with exclusive_lock(self.lock_path), self._key.macs() as mac:
            yield mac, self._verified(mac)

    def entries(self) -> list[dict[str, Any]]:
        with self._locked() as (_, entries):
            return entries

    def _append(self, mac: Mac, existing: list[dict[str, Any]], record: dict[str, Any]) -> dict[str, Any]:
        _validate_record(record)
        prev = existing[-1]["entry_hash"] if existing else GENESIS
        entry: dict[str, Any] = {"prev_hash": prev, "record": dict(record)}
        entry["entry_hash"] = canonical_hash(entry)
        entry["entry_mac"] = mac(ENTRY_DOMAIN, {"entry_hash": entry["entry_hash"]})
        created = not self.path.exists()
        descriptor = open_owner_only(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, "GRANT_LEDGER",
                                     self.root)
        try:
            os.write(descriptor, (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            fsync_directory(self.path.parent)
        count = len(existing) + 1
        head = {"schema": GRANT_HEAD_SCHEMA, "count": count, "last_entry_hash": entry["entry_hash"],
                "head_mac": mac(HEAD_DOMAIN, self._head_payload(count, entry["entry_hash"]))}
        replace_owner_only(self.head_path, json.dumps(head, sort_keys=True).encode("utf-8"), "GRANT_LEDGER_HEAD",
                           self.root)
        return entry

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Public append: only ``DENIED`` records. ISSUED/CONSUMED/EXECUTED have dedicated, checked writers."""
        event = record.get("event") if isinstance(record, Mapping) else None
        if event == GrantEvent.ISSUED.value:
            raise MachineError("GRANT_LEDGER_ISSUE_REQUIRES_PROOFS")
        if event != GrantEvent.DENIED.value:
            raise MachineError("GRANT_LEDGER_TRANSITION_REQUIRES_AUTHORIZATION")
        with self._locked() as (mac, existing):
            return self._append(mac, existing, record)

    def _transition(self, build: Callable[[list[dict[str, Any]]], dict[str, Any]]) -> dict[str, Any]:
        """Verify the chain, derive the next record from the authenticated records, append (one lock)."""
        with self._locked() as (mac, existing):
            return self._append(mac, existing, build([entry["record"] for entry in existing]))

    def _record_issued(self, grant: PrivilegedGrant, recorded_at: datetime,
                       verified: tuple[LocalOperator, LocalOperator]) -> dict[str, Any]:
        """The only ISSUED writer, called by ``issue_grant`` after it verified both proofs itself.

        Python cannot make this method truly private; it refuses a grant whose proof digests,
        roles and challenge do not match the verified operators, and ``issue_grant`` is the only
        caller that obtains verified operators from raw proofs.
        """
        requester, approver = verified
        if ((requester.role, approver.role) != (ProofRole.REQUESTER, ProofRole.APPROVER)
                or (requester.actor_id, approver.actor_id) != (grant.actor, grant.approver)
                or (requester.proof_digest, approver.proof_digest)
                != (grant.requester_proof_digest, grant.approver_proof_digest)
                or {requester.challenge_id, approver.challenge_id} != {grant.challenge_id}):
            raise MachineError("GRANT_LEDGER_ISSUE_REQUIRES_PROOFS")

        def build(records: list[dict[str, Any]]) -> dict[str, Any]:
            if any(r["event"] == GrantEvent.ISSUED.value and r["challenge_id"] == grant.challenge_id
                   for r in records):
                raise MachineError("CHALLENGE_ALREADY_USED")
            return _record(GrantEvent.ISSUED, recorded_at=recorded_at, grant=grant)

        return self._transition(build)

    def consume(self, grant: PrivilegedGrant, authorization_id: str, recorded_at: datetime) -> dict[str, Any]:
        """ISSUED (same content hash) and not yet CONSUMED -> CONSUMED (single use)."""
        def build(records: list[dict[str, Any]]) -> dict[str, Any]:
            mine = [r for r in records if r["grant_id"] == grant.grant_id]
            if not any(r["event"] == GrantEvent.ISSUED.value and r["grant_hash"] == grant.grant_hash for r in mine):
                raise MachineError("PRIVILEGED_GRANT_NOT_RECORDED")
            if any(r["event"] == GrantEvent.CONSUMED.value for r in mine):
                raise MachineError("PRIVILEGED_GRANT_ALREADY_USED")
            return _record(GrantEvent.CONSUMED, recorded_at=recorded_at, grant=grant,
                           authorization_id=authorization_id, decision_code="AUTHORIZED")

        return self._transition(build)

    def execute(self, authorization: PrivilegedAuthorization, request_digest: str,
                recorded_at: datetime) -> dict[str, Any]:
        """Execute once, trusting only the authenticated ledger.

        The CONSUMED record for ``authorization_id`` (and the ISSUED record of its grant)
        supply expiry, operation, machine, project, command and request digest; any field of
        the caller's ``authorization`` object that differs is ``PRIVILEGED_AUTHORIZATION_TAMPERED``.
        """
        def build(records: list[dict[str, Any]]) -> dict[str, Any]:
            consumed = next((r for r in records if r["event"] == GrantEvent.CONSUMED.value
                             and r["authorization_id"] == authorization.authorization_id), None)
            issued = None if consumed is None else next(
                (r for r in records if r["event"] == GrantEvent.ISSUED.value
                 and r["grant_id"] == consumed["grant_id"] and r["grant_hash"] == consumed["grant_hash"]), None)
            if consumed is None or issued is None:
                raise MachineError("PRIVILEGED_AUTHORIZATION_NOT_RECORDED")
            expires_at = _ledger_time(consumed["expires_at"])
            if expires_at is None or _ledger_time(issued["expires_at"]) != expires_at:
                raise MachineError("GRANT_LEDGER_RECORD_INVALID")
            if _authorization_view(authorization) != {**{k: consumed[k] for k in _BOUND_FIELDS},
                                                      "expires_at": expires_at}:
                raise MachineError("PRIVILEGED_AUTHORIZATION_TAMPERED")
            if request_digest != consumed["request_digest"]:
                raise MachineError("PRIVILEGED_EXECUTION_REQUEST_MISMATCH")
            if recorded_at >= expires_at:
                raise MachineError("PRIVILEGED_AUTHORIZATION_EXPIRED")
            if any(r["event"] == GrantEvent.EXECUTED.value and r["authorization_id"] == consumed["authorization_id"]
                   for r in records):
                raise MachineError("PRIVILEGED_AUTHORIZATION_ALREADY_EXECUTED")
            fields = {k: consumed[k] for k in GRANT_RECORD_KEYS
                      if k not in {"schema", "event", "recorded_at", "decision_code", "reason"}}
            return _record(GrantEvent.EXECUTED, recorded_at=recorded_at, **fields, decision_code="EXECUTED")

        return self._transition(build)


_BOUND_FIELDS = ("grant_id", "request_digest", "machine_id", "operation", "project_id", "command_id", "actor",
                 "break_glass")


def _ledger_time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _authorization_view(authorization: PrivilegedAuthorization) -> dict[str, Any]:
    return {"grant_id": authorization.grant_id, "request_digest": authorization.request_digest,
            "machine_id": authorization.machine_id, "operation": str(authorization.operation),
            "project_id": authorization.project_id, "command_id": authorization.command_id,
            "actor": authorization.actor, "break_glass": authorization.break_glass,
            "expires_at": authorization.expires_at}


def _is_remote(value: object) -> bool:
    return isinstance(value, (RemoteSession, RemoteActor))


def _claimed_actor(principal: object) -> str | None:
    """The id a refused caller claimed (unverified; recorded only on DENIED)."""
    if isinstance(principal, RemoteSession):
        actor: object = principal.actor.actor_id
    elif isinstance(principal, RemoteActor):
        actor = principal.actor_id
    else:
        actor = getattr(principal, "operator_id", None)
    return actor if isinstance(actor, str) and IDENTIFIER.fullmatch(actor) else None


def _deny(ledger: GrantLedger, at: datetime, principal: object, machine_id: object, operation: object,
          code: str, grant: PrivilegedGrant | None = None) -> NoReturn:
    safe_machine = machine_id if isinstance(machine_id, str) and re.fullmatch(MACHINE_ID_PATTERN, machine_id) else None
    safe_operation = str(operation) if str(operation) in set(MachineOperation) else None
    fields: dict[str, Any] = {"decision_code": code}
    if grant is None:
        fields.update(actor=_claimed_actor(principal), machine_id=safe_machine, operation=safe_operation)
    ledger.append(_record(GrantEvent.DENIED, recorded_at=at, grant=grant, **fields))
    logger.warning("machine.privileged_denied machine_id=%s operation=%s code=%s", safe_machine, safe_operation, code)
    raise MachineError(code, machine_id=safe_machine)


def issue_grant(request: GrantRequest, *, challenge: Challenge, requester: object, approver: object,
                authenticator: OperatorAuthenticator, ledger: GrantLedger, inventory: MachineInventory,
                reason: str, ttl: timedelta = timedelta(minutes=10), break_glass: bool = False,
                now: datetime | None = None) -> PrivilegedGrant:
    """Verify both operator proofs over ``challenge`` and record the grant for ``request``."""
    at = now or utc_now()
    if not isinstance(request, GrantRequest):
        raise MachineError("PRIVILEGED_REQUEST_INVALID")
    if _is_remote(requester) or _is_remote(approver):
        _deny(ledger, at, requester if _is_remote(requester) else approver, request.machine_id,
              request.operation, REMOTE_REFUSED)
    inventory.get(request.machine_id)
    verified: list[LocalOperator] = []
    for proof, role in ((requester, ProofRole.REQUESTER), (approver, ProofRole.APPROVER)):
        code: str | None = None
        try:
            verified.append(authenticator.verify(proof, role=role, challenge=challenge, request=request, now=at))
        except MachineError as exc:
            code = exc.code
        if code is not None:
            _deny(ledger, at, proof, request.machine_id, request.operation, code)
    actor, approved_by = verified
    if actor.actor_id == approved_by.actor_id and inventory.security_profile is not SecurityProfile.LOCAL_SINGLE_USER:
        _deny(ledger, at, requester, request.machine_id, request.operation, "PRIVILEGED_GRANT_SELF_APPROVAL_FORBIDDEN")
    grant = validated(PrivilegedGrant, {
        "grant_id": "pg-" + secrets.token_hex(10), "actor": actor.actor_id, "approver": approved_by.actor_id,
        "machine_id": request.machine_id, "operation": request.operation, "project_id": request.project_id,
        "command_id": request.command_id, "request_digest": request.digest, "challenge_id": challenge.challenge_id,
        "requester_proof_digest": actor.proof_digest, "approver_proof_digest": approved_by.proof_digest,
        "reason": reason, "issued_at": at, "expires_at": at + ttl, "break_glass": break_glass,
        "security_profile": inventory.security_profile,
    }, prefix="PRIVILEGED_GRANT")
    ledger._record_issued(grant, at, (actor, approved_by))
    logger.info("machine.privileged_grant_issued grant_id=%s machine_id=%s operation=%s break_glass=%s",
                grant.grant_id, grant.machine_id, grant.operation.value, grant.break_glass)
    return grant


@dataclass(frozen=True)
class PrivilegedAuthorization:
    """One authorized request; ``consume_authorization`` executes it exactly once."""

    authorization_id: str
    grant_id: str
    request_digest: str
    machine_id: str
    operation: MachineOperation
    project_id: str
    command_id: str | None
    actor: str
    authorized_at: datetime
    expires_at: datetime
    ledger_entry_hash: str
    break_glass: bool


def _check_command(grant: PrivilegedGrant, request: GrantRequest,
                   commands: Mapping[str, PrivilegedCommand]) -> str | None:
    if grant.command_id is None:
        return None
    command = commands.get(grant.command_id)
    if command is None or command.command_id != grant.command_id:
        return "PRIVILEGED_COMMAND_NOT_REGISTERED"
    if command.operation is not grant.operation:
        return "PRIVILEGED_COMMAND_OPERATION_MISMATCH"
    if {name for name, _ in request.arguments} - command.allowed_args:
        return "PRIVILEGED_COMMAND_ARGUMENT_SCHEMA_INVALID"
    return None


def _grant_problem(grant: PrivilegedGrant, request: GrantRequest, inventory: MachineInventory,
                   at: datetime) -> str | None:
    if grant.machine_id != request.machine_id:
        return "PRIVILEGED_GRANT_MACHINE_MISMATCH"
    if grant.operation is not request.operation:
        return "PRIVILEGED_GRANT_OPERATION_MISMATCH"
    if grant.project_id != request.project_id:
        return "PRIVILEGED_GRANT_PROJECT_MISMATCH"
    if grant.command_id != request.command_id:
        return "PRIVILEGED_GRANT_COMMAND_MISMATCH"
    if grant.request_digest != request.digest:
        return "PRIVILEGED_REQUEST_DIGEST_MISMATCH"
    if grant.security_profile is not inventory.security_profile:
        return "PRIVILEGED_GRANT_PROFILE_MISMATCH"
    if at < grant.issued_at:
        return "PRIVILEGED_GRANT_NOT_YET_VALID"
    if at >= grant.expires_at:
        return "PRIVILEGED_GRANT_EXPIRED"
    try:
        profile = inventory.get(request.machine_id)
    except MachineError:
        return "MACHINE_NOT_FOUND"
    if profile.identity_alert is not None or profile.connection_state is ConnectionState.UNAUTHORIZED:
        return "PRIVILEGED_MACHINE_UNAUTHORIZED"
    return None


def _request_or_deny(ledger: GrantLedger, at: datetime, session: object, machine_id: str,
                     operation: MachineOperation | str, project_id: str, command_id: str | None,
                     arguments: Mapping[str, str] | None) -> GrantRequest:
    code = "PRIVILEGED_REQUEST_INVALID"
    try:
        return request_for(machine_id=machine_id, operation=operation, project_id=project_id,
                           command_id=command_id, arguments=arguments)
    except MachineError as exc:
        code = exc.code
    _deny(ledger, at, session, machine_id, operation, code)


def authorize_privileged(grant: object, *, operation: MachineOperation | str, machine_id: str, project_id: str,
                         command_id: str | None = None, arguments: Mapping[str, str] | None = None,
                         ledger: GrantLedger, inventory: MachineInventory,
                         commands: Mapping[str, PrivilegedCommand] = NO_PRIVILEGED_COMMANDS,
                         session: object = None, now: datetime | None = None) -> PrivilegedAuthorization:
    """Authorize exactly the bound request; every refusal is recorded and raised as a code."""
    at = now or utc_now()
    if _is_remote(session) or _is_remote(grant):
        _deny(ledger, at, session if _is_remote(session) else grant, machine_id, operation, REMOTE_REFUSED)
    if operation_class(str(operation)) is not OperationClass.PRIVILEGED_OPERATION:
        raise MachineError("MACHINE_OPERATION_NOT_PRIVILEGED")
    if not isinstance(grant, PrivilegedGrant):
        _deny(ledger, at, session, machine_id, operation, "PRIVILEGED_GRANT_REQUIRED")
    request = _request_or_deny(ledger, at, session, machine_id, operation, project_id, command_id, arguments)
    code = _grant_problem(grant, request, inventory, at) or _check_command(grant, request, commands)
    if code is not None:
        _deny(ledger, at, session, machine_id, operation, code, grant)
    authorization_id = "pa-" + secrets.token_hex(10)
    consume_code: str | None = None
    try:
        entry = ledger.consume(grant, authorization_id, at)
    except MachineError as exc:
        if not exc.code.startswith("PRIVILEGED_GRANT"):
            raise
        consume_code = exc.code
    if consume_code is not None:
        _deny(ledger, at, session, machine_id, operation, consume_code, grant)
    logger.info("machine.privileged_authorized grant_id=%s machine_id=%s operation=%s", grant.grant_id,
                machine_id, grant.operation.value)
    return PrivilegedAuthorization(authorization_id, grant.grant_id, request.digest, request.machine_id,
                                   request.operation, request.project_id, request.command_id, grant.actor, at,
                                   grant.expires_at, str(entry["entry_hash"]), grant.break_glass)


def consume_authorization(authorization: object, *, operation: MachineOperation | str, machine_id: str,
                          project_id: str, command_id: str | None = None,
                          arguments: Mapping[str, str] | None = None, ledger: GrantLedger,
                          session: object = None, now: datetime | None = None) -> dict[str, Any]:
    """Execution boundary: the executed request must match the ledger's authorization, exactly once.

    Nothing on the ``authorization`` object is trusted: expiry and every bound field come from
    the authenticated CONSUMED/ISSUED records (``GrantLedger.execute``).
    """
    at = now or utc_now()
    if _is_remote(session):
        _deny(ledger, at, session, machine_id, operation, REMOTE_REFUSED)
    if not isinstance(authorization, PrivilegedAuthorization):
        raise MachineError("PRIVILEGED_AUTHORIZATION_REQUIRED")
    request = _request_or_deny(ledger, at, session, machine_id, operation, project_id, command_id, arguments)
    code: str | None = None
    try:
        entry = ledger.execute(authorization, request.digest, at)
    except MachineError as exc:
        if not exc.code.startswith("PRIVILEGED_"):
            raise
        code = exc.code
    if code is not None:
        _deny(ledger, at, session, machine_id, operation, code)
    logger.info("machine.privileged_executed authorization_id=%s machine_id=%s", authorization.authorization_id,
                machine_id)
    return entry
