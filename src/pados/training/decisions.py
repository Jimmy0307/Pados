# SPDX-License-Identifier: AGPL-3.0-only
"""Verification of signed, bound, single-use human decisions (run approval, promotion, rejection).

Implemented locally after the concept of ``pados.release.approval`` (neither imported nor changed). A
decision is not self-declared JSON: it is a record signed with HMAC-SHA256 that binds a unique 128-bit
decision id, the operation (``APPROVE_TRAINING_RUN``, ``PROMOTE_MODEL``, ``REJECT_MODEL``), the exact
subject hash, a registered human approver, ``origin = HUMAN_OPERATOR``, ``issued_at`` and a mandatory
``expires_at`` at most 24 hours later.

Subjects (CR-12b TP-01a) bind one exact log entry, not merely content: a run approval binds the contract
hash, the job id and the job's CREATED event hash (``pados.training.jobs.run_approval_subject``); a
promotion or rejection binds the candidate hash, the candidate id and its REGISTERED event hash
(``pados.training.registry.decision_subject``). One signed decision therefore authorizes exactly one job or
one registry entry, never another that merely shares its contract or artifact.

Per-approver keys (CR-12b TP-02a): the operator registry maps every human approver to that approver's own
SecretRef. A record is verified only under the key registered for the approver it names, so a valid MAC
proves possession of that approver's key; one approver's key cannot sign for another. The registry
(``pados.training-operators/v3``, CR-12c TP-02) also binds every approver to one OS account, which the
operator-side signer requires the signing process to run as (``pados.training.operator``).

Verify-only (CR-12 TP-02, CR-12b TP-02b): ``DecisionVerifier`` is what training jobs and the registry
receive. It retains no key bytes and exposes no MAC computation: ``_signature_matches`` resolves the
approver's key through ``SecretBoundary`` for one comparison and returns only a boolean. Signing lives in
``pados.training.operator``. Residual: HMAC is symmetric and any in-process code holding the same
``SecretBoundary`` can resolve a SecretRef - the platform's operator-held symmetric model
(``ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED``).

The full signed record is retained in every authority-bearing log entry (consumed ledger, job APPROVED
event, registry PROMOTED/REJECTED event) so that readers re-verify the MAC instead of trusting unsigned
ledger fields (CR-12 TP-01).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.enterprise.secrets import SecretBoundary, is_secret_ref
from pados.lifecycle.retention import DataKind
from pados.training._chain import (
    ANCHOR_CODES,
    LEDGER_LOG_NAME,
    Anchoring,
    ChainedLogError,
    ExternalAnchor,
    HashChainedLog,
)

DECISION_SCHEMA = "pados.training-decision/v2"
REGISTRY_SCHEMA = "pados.training-operators/v3"
ORIGIN_HUMAN = "HUMAN_OPERATOR"
MAX_DECISION_LIFETIME = timedelta(hours=24)
MIN_KEY_BYTES = 32
LEDGER_DATA_KIND = DataKind.TRAINING_DECISION_LEDGER
SIGNED_FIELDS = ("schema", "decision_id", "operation", "subject_hash", "approver", "origin", "issued_at",
                 "expires_at")
RECORD_FIELDS = (*SIGNED_FIELDS, "signature")
LEDGER_FIELDS = ("seq", "decision_id", "operation", "subject_hash", "approver", "consumed_at", "decision",
                 "prev_hash", "entry_hash")
CI_ENVIRONMENT_MARKERS = ("GITHUB_ACTIONS", "GITHUB_RUN_ID", "ACTIONS_ID_TOKEN_REQUEST_URL", "RUNNER_TEMP", "CI")
_AUTOMATION_ORIGIN = re.compile(r"CI|GITHUB|ACTIONS|AUTOMATION|BOT|RUNNER|WORKFLOW|JOB|TRAIN|PIPELINE",
                                re.IGNORECASE)
# Principals that denote automation (CI, bots, the training plane itself) can never approve.
AUTOMATION_PRINCIPAL = re.compile(
    r"\[bot\]|github-actions|dependabot|^ci[:/._-]|^runner[:/._-]|workflow|^pados[._-]|^training[._-]"
    r"|^job[._-]|^trainer|^pipeline|^scheduler|^worker[._-]|^service[._-]", re.IGNORECASE)
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PRINCIPAL = re.compile(r"[a-z][a-z0-9_.-]{2,63}")
_LOGIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.$-]{0,63}")
_DOMAIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_PRIVILEGED_LOGINS = frozenset({"root", "system", "localsystem", "administrator"})


def _environment() -> Mapping[str, str]:
    """Process environment source. Private test seam (not exported); production never overrides it."""
    return os.environ


class DecisionOperation(StrEnum):
    APPROVE_TRAINING_RUN = "APPROVE_TRAINING_RUN"
    PROMOTE_MODEL = "PROMOTE_MODEL"
    REJECT_MODEL = "REJECT_MODEL"


class DecisionCode(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    CONSUMED = "CONSUMED"
    VERIFIED = "VERIFIED"
    AUTOMATION_CANNOT_DECIDE = "AUTOMATION_CANNOT_DECIDE"
    DECISION_MALFORMED = "DECISION_MALFORMED"
    DECISION_SIGNATURE_INVALID = "DECISION_SIGNATURE_INVALID"
    DECISION_KEY_UNAVAILABLE = "DECISION_KEY_UNAVAILABLE"
    APPROVER_NOT_REGISTERED = "APPROVER_NOT_REGISTERED"
    DECISION_OPERATION_MISMATCH = "DECISION_OPERATION_MISMATCH"
    DECISION_SUBJECT_MISMATCH = "DECISION_SUBJECT_MISMATCH"
    DECISION_NOT_YET_VALID = "DECISION_NOT_YET_VALID"
    DECISION_EXPIRED = "DECISION_EXPIRED"
    DECISION_LIFETIME_INVALID = "DECISION_LIFETIME_INVALID"
    DECISION_ALREADY_CONSUMED = "DECISION_ALREADY_CONSUMED"
    DECISION_LEDGER_CORRUPT = "DECISION_LEDGER_CORRUPT"
    LEDGER_ROLLBACK_DETECTED = "LEDGER_ROLLBACK_DETECTED"
    LEDGER_ANCHOR_UNAVAILABLE = "LEDGER_ANCHOR_UNAVAILABLE"
    LEDGER_HEAD_NOT_ANCHORED = "LEDGER_HEAD_NOT_ANCHORED"
    LEDGER_ANCHOR_LOST = "LEDGER_ANCHOR_LOST"
    ANCHOR_LOG_NAME_IN_USE = "ANCHOR_LOG_NAME_IN_USE"
    ANCHOR_REQUIRED = "ANCHOR_REQUIRED"
    OPERATOR_SESSION_MISMATCH = "OPERATOR_SESSION_MISMATCH"
    OPERATOR_PLATFORM_UNSUPPORTED = "OPERATOR_PLATFORM_UNSUPPORTED"
    OPERATOR_SESSION_NOT_INTERACTIVE = "OPERATOR_SESSION_NOT_INTERACTIVE"
    OPERATOR_CONFIRMATION_MISMATCH = "OPERATOR_CONFIRMATION_MISMATCH"


_ANCHOR_CODES = frozenset(DecisionCode(code) for code in ANCHOR_CODES)


class DecisionError(ValueError):
    def __init__(self, code: DecisionCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class DecisionOutcome:
    code: DecisionCode
    decision_id: str | None = None
    approver: str | None = None

    @property
    def ok(self) -> bool:
        return self.code in {DecisionCode.AUTHORIZED, DecisionCode.CONSUMED, DecisionCode.VERIFIED}


@dataclass(frozen=True)
class DecisionBinding:
    """What the verifier expects the human to have decided about."""

    operation: DecisionOperation
    subject_hash: str


def running_in_automation(environ: Mapping[str, str]) -> bool:
    return any(marker in environ for marker in CI_ENVIRONMENT_MARKERS)


def has_automation_markers(record: Mapping[str, Any]) -> bool:
    origin, approver = str(record.get("origin", "")), str(record.get("approver", ""))
    if origin != ORIGIN_HUMAN and _AUTOMATION_ORIGIN.search(origin):
        return True
    if AUTOMATION_PRINCIPAL.search(approver):
        return True
    return any(re.match(r"(?i)(github|ci|runner|actions|job|training)[_-]", str(key)) for key in record)


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("NOT_A_TIMESTAMP")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return parsed


def signed_payload(record: Mapping[str, Any]) -> bytes:
    return json.dumps({k: record[k] for k in SIGNED_FIELDS}, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


def shape_ok(record: Any) -> bool:
    if not isinstance(record, Mapping) or set(record) != set(RECORD_FIELDS):
        return False
    if record.get("schema") != DECISION_SCHEMA:
        return False
    if not all(isinstance(record[k], str) for k in RECORD_FIELDS):
        return False
    return bool(_HEX32.fullmatch(record["decision_id"]) and _HEX64.fullmatch(record["subject_hash"])
                and _HEX64.fullmatch(record["signature"]) and _PRINCIPAL.fullmatch(record["approver"])
                and record["operation"] in {op.value for op in DecisionOperation}
                and record["origin"] == ORIGIN_HUMAN)


def retained_record(record: Mapping[str, Any]) -> dict[str, str]:
    """The exact signed record as stored in log entries (all bound fields + MAC; never key material)."""
    if not shape_ok(record):
        raise DecisionError(DecisionCode.DECISION_MALFORMED)
    return {k: str(record[k]) for k in RECORD_FIELDS}


@dataclass(frozen=True)
class OsAccount:
    """The OS login an approver must be signed in as when signing (CR-12c TP-02).

    POSIX: the numeric uid plus the login name (``os.getuid()`` and ``pwd``). Windows: the account name and
    domain (``getpass.getuser()``, ``USERNAME``, ``USERDOMAIN``) - best-effort, PENDING Windows validation."""

    platform: str
    login: str
    uid: int | None = None
    domain: str | None = None

    @classmethod
    def from_document(cls, value: Any) -> OsAccount:
        if not isinstance(value, Mapping) or not isinstance(value.get("login"), str):
            raise ValueError("OPERATOR_REGISTRY_ACCOUNT_INVALID")
        login = value["login"]
        if not _LOGIN.fullmatch(login) or login.casefold() in _PRIVILEGED_LOGINS:
            raise ValueError("OPERATOR_REGISTRY_ACCOUNT_INVALID")
        if value.get("platform") == "posix" and set(value) == {"platform", "uid", "login"}:
            uid = value["uid"]
            if isinstance(uid, bool) or not isinstance(uid, int) or not 0 < uid < 2**32:
                raise ValueError("OPERATOR_REGISTRY_ACCOUNT_INVALID")
            return cls("posix", login, uid=uid)
        if value.get("platform") == "windows" and set(value) == {"platform", "domain", "login"}:
            domain = value["domain"]
            if not isinstance(domain, str) or not _DOMAIN.fullmatch(domain):
                raise ValueError("OPERATOR_REGISTRY_ACCOUNT_INVALID")
            return cls("windows", login, domain=domain)
        raise ValueError("OPERATOR_REGISTRY_ACCOUNT_INVALID")

    @property
    def identity(self) -> tuple[str, str]:
        if self.platform == "posix":
            return ("posix", str(self.uid))
        return ("windows", f"{self.domain}\\{self.login}".casefold())


@dataclass(frozen=True)
class RegisteredOperator:
    operator_id: str
    key_ref: str
    account: OsAccount


@dataclass(frozen=True)
class TrainingOperatorRegistry:
    """Registered human principals allowed to decide, each with their OWN key SecretRef (never key bytes) and
    their OWN OS account.

    Loaded from an operator-owned document. Automation principals, duplicate principals, shared key
    references and shared or privileged OS accounts are refused, so a valid MAC under a principal's key
    names exactly one human approver, and that approver signs only from their own OS login."""

    operators: tuple[RegisteredOperator, ...]

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> TrainingOperatorRegistry:
        if document.get("schema") != REGISTRY_SCHEMA or not isinstance(document.get("operators"), list):
            raise ValueError("OPERATOR_REGISTRY_INVALID")
        seen: set[str] = set()
        refs: set[str] = set()
        accounts: set[tuple[str, str]] = set()
        allowed: list[RegisteredOperator] = []
        for entry in document["operators"]:
            if (not isinstance(entry, Mapping) or not isinstance(entry.get("operator_id"), str)
                    or not isinstance(entry.get("key_ref"), str) or not isinstance(entry.get("enabled"), bool)
                    or set(entry) != {"operator_id", "enabled", "key_ref", "os_account"}):
                raise ValueError("OPERATOR_REGISTRY_INVALID")
            operator_id, key_ref = entry["operator_id"], entry["key_ref"]
            if not _PRINCIPAL.fullmatch(operator_id) or AUTOMATION_PRINCIPAL.search(operator_id):
                raise ValueError("OPERATOR_REGISTRY_PRINCIPAL_INVALID")
            if not is_secret_ref(key_ref):
                raise ValueError("OPERATOR_REGISTRY_KEY_REF_INVALID")
            account = OsAccount.from_document(entry["os_account"])
            if operator_id in seen:
                raise ValueError("OPERATOR_REGISTRY_DUPLICATE_PRINCIPAL")
            if key_ref in refs:
                raise ValueError("OPERATOR_REGISTRY_KEY_SHARED")
            if account.identity in accounts:
                raise ValueError("OPERATOR_REGISTRY_ACCOUNT_SHARED")
            seen.add(operator_id)
            refs.add(key_ref)
            accounts.add(account.identity)
            if entry["enabled"]:
                allowed.append(RegisteredOperator(operator_id, key_ref, account))
        return cls(tuple(sorted(allowed, key=lambda item: item.operator_id)))

    @classmethod
    def from_file(cls, path: Path) -> TrainingOperatorRegistry:
        return cls.from_document(json.loads(path.read_text(encoding="utf-8")))

    def _entry(self, principal: str) -> RegisteredOperator | None:
        return next((item for item in self.operators if item.operator_id == principal), None)

    def allows(self, principal: str) -> bool:
        return self._entry(principal) is not None

    def key_ref(self, principal: str) -> str | None:
        """The approver's own key reference, or ``None`` when the principal is not an enabled operator."""
        entry = self._entry(principal)
        return None if entry is None else entry.key_ref

    def os_account(self, principal: str) -> OsAccount | None:
        """The OS account the approver must be logged in as to sign, or ``None`` when not an enabled operator."""
        entry = self._entry(principal)
        return None if entry is None else entry.account


def _ledger_code(error: ChainedLogError) -> DecisionCode:
    if error.code in {code.value for code in _ANCHOR_CODES}:
        return DecisionCode(error.code)
    return DecisionCode.DECISION_LEDGER_CORRUPT


class ConsumedDecisionLedger:
    """Append-only, hash-chained JSONL of consumed decisions, each retaining the full signed record."""

    def __init__(self, path: Path, *, anchor: ExternalAnchor | None = None,
                 log_name: str = LEDGER_LOG_NAME) -> None:
        self._log = HashChainedLog(path, fields=LEDGER_FIELDS, corrupt_code=DecisionCode.DECISION_LEDGER_CORRUPT,
                                   log_name=log_name, anchor=anchor)

    @property
    def anchoring(self) -> Anchoring:
        return self._log.anchoring

    def verify_head(self) -> None:
        """The ledger head equals its anchored head (``DecisionError`` with the anchor code otherwise)."""
        self._entries()

    def _entries(self) -> list[dict[str, Any]]:
        try:
            return self._log.entries()
        except ChainedLogError as exc:
            raise DecisionError(_ledger_code(exc)) from None

    def consumed(self, decision_id: str) -> dict[str, Any] | None:
        return next((e for e in self._entries() if e["decision_id"] == decision_id), None)

    def append(self, record: Mapping[str, Any], *, consumed_at: str) -> dict[str, Any]:
        retained = retained_record(record)

        def build(entries: list[dict[str, Any]]) -> dict[str, Any]:
            if any(e["decision_id"] == retained["decision_id"] for e in entries):
                raise DecisionError(DecisionCode.DECISION_ALREADY_CONSUMED)
            return {"decision_id": retained["decision_id"], "operation": retained["operation"],
                    "subject_hash": retained["subject_hash"], "approver": retained["approver"],
                    "consumed_at": consumed_at, "decision": retained}

        try:
            entry = self._log.append(build)
        except ChainedLogError as exc:
            raise DecisionError(_ledger_code(exc)) from None
        if entry is None:
            raise DecisionError(DecisionCode.DECISION_LEDGER_CORRUPT)
        return entry


def _signature_matches(boundary: SecretBoundary, key_ref: str, record: Mapping[str, Any]) -> bool:
    """The only MAC computation on the verification side: resolve the approver's key for this one
    comparison, compare in constant time and return a boolean. No MAC value ever leaves this function and
    the key bytes are not retained."""
    try:
        with boundary.resolved({key_ref}, authorized_refs={key_ref}) as values:
            key = values[key_ref].reveal().encode("utf-8")
            if len(key) < MIN_KEY_BYTES:
                raise DecisionError(DecisionCode.DECISION_KEY_UNAVAILABLE)
            return hmac.compare_digest(hmac.new(key, signed_payload(record), hashlib.sha256).hexdigest(),
                                       str(record["signature"]))
    except PermissionError:
        raise DecisionError(DecisionCode.DECISION_KEY_UNAVAILABLE) from None


class DecisionVerifier:
    """Verify-only decision authority handed to training jobs and the registry.

    It holds the ``SecretBoundary`` (not key bytes), the operator registry (key references) and the consumed
    ledger. It has no signing method and no method that returns a MAC over a caller-chosen payload."""

    def __init__(self, *, boundary: SecretBoundary, registry: TrainingOperatorRegistry,
                 ledger: ConsumedDecisionLedger) -> None:
        if not isinstance(boundary, SecretBoundary) or not isinstance(registry, TrainingOperatorRegistry):
            raise TypeError("DECISION_VERIFIER_CONFIGURATION_INVALID")
        self._boundary = boundary
        self._registry = registry
        self.ledger = ledger

    @property
    def anchoring(self) -> Anchoring:
        return self.ledger.anchoring

    def _authenticate_and_bind(self, record: Any, binding: DecisionBinding) -> DecisionOutcome:
        if not shape_ok(record):
            return DecisionOutcome(DecisionCode.DECISION_MALFORMED)
        decision_id, approver = str(record["decision_id"]), str(record["approver"])
        if has_automation_markers(record):
            return DecisionOutcome(DecisionCode.AUTOMATION_CANNOT_DECIDE, decision_id)
        key_ref = self._registry.key_ref(approver)
        if key_ref is None:
            return DecisionOutcome(DecisionCode.APPROVER_NOT_REGISTERED, decision_id)
        try:
            if not _signature_matches(self._boundary, key_ref, record):
                return DecisionOutcome(DecisionCode.DECISION_SIGNATURE_INVALID, decision_id)
        except DecisionError as error:
            return DecisionOutcome(error.code, decision_id)
        if record["operation"] != binding.operation.value:
            return DecisionOutcome(DecisionCode.DECISION_OPERATION_MISMATCH, decision_id)
        if not hmac.compare_digest(str(record["subject_hash"]), binding.subject_hash):
            return DecisionOutcome(DecisionCode.DECISION_SUBJECT_MISMATCH, decision_id)
        return DecisionOutcome(DecisionCode.VERIFIED, decision_id, approver)

    @staticmethod
    def _window(record: Mapping[str, Any], at: datetime, outcome: DecisionOutcome) -> DecisionOutcome:
        try:
            issued, expires = parse_time(record["issued_at"]), parse_time(record["expires_at"])
        except ValueError:
            return DecisionOutcome(DecisionCode.DECISION_MALFORMED, outcome.decision_id)
        if not timedelta(0) < expires - issued <= MAX_DECISION_LIFETIME:
            return DecisionOutcome(DecisionCode.DECISION_LIFETIME_INVALID, outcome.decision_id)
        if issued > at:
            return DecisionOutcome(DecisionCode.DECISION_NOT_YET_VALID, outcome.decision_id)
        if at >= expires:
            return DecisionOutcome(DecisionCode.DECISION_EXPIRED, outcome.decision_id)
        return outcome

    def verify(self, record: Any, binding: DecisionBinding, *, at: datetime) -> DecisionOutcome:
        """Verify a decision: shape, registered approver and that approver's own MAC, operation, exact subject
        and that ``at`` (now, or the recorded transition time) lies inside the signed validity window."""
        outcome = self._authenticate_and_bind(record, binding)
        if not outcome.ok:
            return outcome
        return self._window(record, at, outcome)

    def check(self, record: Any, binding: DecisionBinding, *, now: datetime) -> DecisionOutcome:
        if running_in_automation(_environment()):
            return DecisionOutcome(DecisionCode.AUTOMATION_CANNOT_DECIDE)
        outcome = self.verify(record, binding, at=now)
        if not outcome.ok:
            return outcome
        try:
            if self.ledger.consumed(str(record["decision_id"])) is not None:
                return DecisionOutcome(DecisionCode.DECISION_ALREADY_CONSUMED, outcome.decision_id)
        except DecisionError as error:
            return DecisionOutcome(error.code, outcome.decision_id)
        return DecisionOutcome(DecisionCode.AUTHORIZED, outcome.decision_id, outcome.approver)

    def consume(self, record: Any, binding: DecisionBinding, *, now: datetime) -> DecisionOutcome:
        outcome = self.check(record, binding, now=now)
        if not outcome.ok:
            return outcome
        try:
            self.ledger.append(record, consumed_at=now.astimezone(UTC).isoformat())
        except DecisionError as error:
            return DecisionOutcome(error.code, outcome.decision_id)
        return DecisionOutcome(DecisionCode.CONSUMED, outcome.decision_id, outcome.approver)

    def consumed_matches(self, record: Mapping[str, Any], binding: DecisionBinding) -> bool:
        """The consumed ledger holds exactly this signed record for exactly this binding.

        A corrupt ledger reads as ``False``; a rollback below the anchored head (or an unavailable anchor)
        raises ``DecisionError`` so callers fail closed with that explicit code."""
        try:
            entry = self.ledger.consumed(str(record.get("decision_id")))
        except DecisionError as error:
            if error.code in _ANCHOR_CODES:
                raise
            return False
        return bool(entry is not None and entry["operation"] == binding.operation.value
                    and entry["subject_hash"] == binding.subject_hash and entry["decision"] == dict(record))


def require_verifier(candidate: object) -> DecisionVerifier:
    """Training jobs and the registry accept a verifier only, never a signing-capable object."""
    if not isinstance(candidate, DecisionVerifier) or hasattr(candidate, "issue"):
        raise TypeError("DECISION_VERIFIER_REQUIRED")
    return candidate
