# SPDX-License-Identifier: AGPL-3.0-only
"""Deployment approval authority for the Home Base (signed, bound, single use).

An approval is not self-declared JSON. ``HmacApprovalAuthority`` (the V1
``DeploymentApprovalAuthority``) accepts only records signed with HMAC-SHA256
under an operator-held Home Base key referenced by SecretRef and resolved via
``SecretBoundary``; CI never holds that key, and the key value never appears in
records, logs or errors. A record binds a unique 128-bit approval id, the
manifest hash, the staged digest, the Home Base target, the operation
(``DEPLOY``), a registered approver principal (an operator registry entry; a
display name proves nothing), ``issued_at`` and a mandatory ``expires_at`` at
most 24 hours later.

``check`` verifies, in order: not running inside GitHub Actions and no CI origin
markers (``CI_CANNOT_AUTHORIZE_DEPLOYMENT`` regardless of signature), strict
schema, signature (``hmac.compare_digest``), registered approver, bindings, time
window and that the id is absent from the append-only consumed-approvals ledger.
``consume`` is the separate explicit step that re-checks and records the id in
the hash-chained ledger. Nothing here deploys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pados.enterprise.secrets import SecretBoundary, is_secret_ref
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_minimized
from pados.lifecycle.retention import DataKind

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

APPROVAL_SCHEMA = "pados.deploy-approval/v1"
REGISTRY_SCHEMA = "pados.deploy-operators/v1"
OPERATION_DEPLOY = "DEPLOY"
ORIGIN_HUMAN = "HUMAN_OPERATOR"
MAX_APPROVAL_LIFETIME = timedelta(hours=24)
MIN_KEY_BYTES = 32
LEDGER_DATA_KIND = DataKind.DEPLOYMENT_APPROVAL_LEDGER  # consumed-approval bindings (R3 metadata)
SIGNED_FIELDS = ("schema", "approval_id", "manifest_sha256", "staged_digest", "target", "operation",
                 "approver", "origin", "issued_at", "expires_at")
LEDGER_FIELDS = ("seq", "approval_id", "manifest_sha256", "staged_digest", "target", "approver",
                 "consumed_at", "prev_hash", "entry_hash")
GENESIS = "0" * 64
# Environment variables whose presence means "this process runs inside GitHub Actions".
CI_ENVIRONMENT_MARKERS = ("GITHUB_ACTIONS", "GITHUB_RUN_ID", "ACTIONS_ID_TOKEN_REQUEST_URL", "RUNNER_TEMP")
_CI_ORIGINS = re.compile(r"CI|GITHUB|ACTIONS|AUTOMATION|BOT|RUNNER|WORKFLOW", re.IGNORECASE)
_CI_ACTOR = re.compile(r"\[bot\]|github-actions|dependabot|^ci[:/._-]|^runner[:/._-]|workflow", re.IGNORECASE)
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PRINCIPAL = re.compile(r"[a-z][a-z0-9_.-]{2,63}")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class ApprovalCode(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    CONSUMED = "CONSUMED"
    CI_CANNOT_AUTHORIZE_DEPLOYMENT = "CI_CANNOT_AUTHORIZE_DEPLOYMENT"
    APPROVAL_MALFORMED = "APPROVAL_MALFORMED"
    APPROVAL_SIGNATURE_INVALID = "APPROVAL_SIGNATURE_INVALID"
    APPROVAL_KEY_UNAVAILABLE = "APPROVAL_KEY_UNAVAILABLE"
    APPROVER_NOT_REGISTERED = "APPROVER_NOT_REGISTERED"
    APPROVAL_MANIFEST_MISMATCH = "APPROVAL_MANIFEST_MISMATCH"
    APPROVAL_STAGED_DIGEST_MISMATCH = "APPROVAL_STAGED_DIGEST_MISMATCH"
    APPROVAL_TARGET_MISMATCH = "APPROVAL_TARGET_MISMATCH"
    APPROVAL_OPERATION_INVALID = "APPROVAL_OPERATION_INVALID"
    APPROVAL_NOT_YET_VALID = "APPROVAL_NOT_YET_VALID"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_LIFETIME_INVALID = "APPROVAL_LIFETIME_INVALID"
    APPROVAL_ALREADY_CONSUMED = "APPROVAL_ALREADY_CONSUMED"
    APPROVAL_LEDGER_CORRUPT = "APPROVAL_LEDGER_CORRUPT"


class ApprovalError(ValueError):
    def __init__(self, code: ApprovalCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class ApprovalDecision:
    code: ApprovalCode
    approval_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.code in {ApprovalCode.AUTHORIZED, ApprovalCode.CONSUMED}


@dataclass(frozen=True)
class Binding:
    """What the Home Base verified: the approval must match all of it."""

    manifest_sha256: str
    staged_digest: str
    target: str


class DeploymentApprovalAuthority(Protocol):
    def check(self, record: Mapping[str, Any], binding: Binding, *, now: datetime) -> ApprovalDecision:
        """Decide whether ``record`` authorizes deployment of ``binding`` at ``now``."""

    def consume(self, record: Mapping[str, Any], binding: Binding, *, now: datetime) -> ApprovalDecision:
        """Re-check and record the approval as used (single use)."""


def running_in_ci(environ: Mapping[str, str]) -> bool:
    return any(marker in environ for marker in CI_ENVIRONMENT_MARKERS)


def _has_ci_markers(record: Mapping[str, Any]) -> bool:
    origin, approver = str(record.get("origin", "")), str(record.get("approver", ""))
    if origin != ORIGIN_HUMAN and _CI_ORIGINS.search(origin):
        return True
    if _CI_ACTOR.search(approver):
        return True
    return any(re.match(r"(?i)(github|ci|runner|actions)[_-]", str(key)) for key in record)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("NOT_A_TIMESTAMP")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return parsed


def _payload(record: Mapping[str, Any]) -> bytes:
    return json.dumps({k: record[k] for k in SIGNED_FIELDS}, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


def _shape_ok(record: Mapping[str, Any]) -> bool:
    if set(record) != {*SIGNED_FIELDS, "signature"} or record.get("schema") != APPROVAL_SCHEMA:
        return False
    strings = all(isinstance(record[k], str) for k in (*SIGNED_FIELDS, "signature"))
    return bool(strings and _HEX32.fullmatch(record["approval_id"]) and _HEX64.fullmatch(record["manifest_sha256"])
                and _HEX64.fullmatch(record["staged_digest"]) and _HEX64.fullmatch(record["signature"])
                and _TARGET.fullmatch(record["target"]) and _PRINCIPAL.fullmatch(record["approver"])
                and record["origin"] == ORIGIN_HUMAN)


@dataclass(frozen=True)
class OperatorRegistry:
    """Registered principals allowed to approve deployments (loaded from an operator-owned file)."""

    operators: frozenset[str]

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> OperatorRegistry:
        if document.get("schema") != REGISTRY_SCHEMA or not isinstance(document.get("operators"), list):
            raise ValueError("OPERATOR_REGISTRY_INVALID")
        allowed: set[str] = set()
        for entry in document["operators"]:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("operator_id"), str):
                raise ValueError("OPERATOR_REGISTRY_INVALID")
            operator_id = entry["operator_id"]
            if not _PRINCIPAL.fullmatch(operator_id) or _CI_ACTOR.search(operator_id):
                raise ValueError("OPERATOR_REGISTRY_PRINCIPAL_INVALID")
            if entry.get("enabled") is True:
                allowed.add(operator_id)
        return cls(frozenset(allowed))

    @classmethod
    def from_file(cls, path: Path) -> OperatorRegistry:
        return cls.from_document(json.loads(path.read_text(encoding="utf-8")))

    def allows(self, principal: str) -> bool:
        return principal in self.operators


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class ConsumedApprovalLedger:
    """Append-only, hash-chained JSONL of consumed approval ids (process-safe)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _entries(self) -> list[dict[str, Any]]:
        if self.path.is_symlink():
            raise ApprovalError(ApprovalCode.APPROVAL_LEDGER_CORRUPT)
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        previous = GENESIS
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                entry = json.loads(line)
            except ValueError as error:
                raise ApprovalError(ApprovalCode.APPROVAL_LEDGER_CORRUPT) from error
            if (not isinstance(entry, dict) or set(entry) != set(LEDGER_FIELDS) or entry["seq"] != number
                    or entry["prev_hash"] != previous
                    or entry["entry_hash"] != canonical_hash({k: entry[k] for k in LEDGER_FIELDS
                                                              if k != "entry_hash"})):
                raise ApprovalError(ApprovalCode.APPROVAL_LEDGER_CORRUPT)
            previous = entry["entry_hash"]
            entries.append(entry)
        return entries

    def is_consumed(self, approval_id: str) -> bool:
        with _locked(self.path):
            return any(e["approval_id"] == approval_id for e in self._entries())

    def append(self, record: Mapping[str, Any], *, consumed_at: str) -> dict[str, Any]:
        with _locked(self.path):
            entries = self._entries()
            if any(e["approval_id"] == record["approval_id"] for e in entries):
                raise ApprovalError(ApprovalCode.APPROVAL_ALREADY_CONSUMED)
            entry: dict[str, Any] = {
                "seq": len(entries) + 1, "approval_id": record["approval_id"],
                "manifest_sha256": record["manifest_sha256"], "staged_digest": record["staged_digest"],
                "target": record["target"], "approver": record["approver"], "consumed_at": consumed_at,
                "prev_hash": entries[-1]["entry_hash"] if entries else GENESIS,
            }
            entry["entry_hash"] = canonical_hash(entry)
            assert_minimized(entry, allowed_keys=LEDGER_FIELDS)
            line = (json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            return entry


class HmacApprovalAuthority:
    """V1 ``DeploymentApprovalAuthority``: HMAC-SHA256 with an operator-held Home Base key."""

    def __init__(self, *, key_ref: str, boundary: SecretBoundary, registry: OperatorRegistry,
                 ledger: ConsumedApprovalLedger, environ: Mapping[str, str] | None = None) -> None:
        if not is_secret_ref(key_ref):
            raise ValueError("APPROVAL_KEY_REF_INVALID")
        self._key_ref = key_ref
        self._boundary = boundary
        self._registry = registry
        self._ledger = ledger
        self._environ = os.environ if environ is None else environ

    def _mac(self, payload: bytes) -> str:
        try:
            with self._boundary.resolved({self._key_ref}, authorized_refs={self._key_ref}) as values:
                key = values[self._key_ref].reveal().encode("utf-8")
                if len(key) < MIN_KEY_BYTES:
                    raise ApprovalError(ApprovalCode.APPROVAL_KEY_UNAVAILABLE)
                return hmac.new(key, payload, hashlib.sha256).hexdigest()
        except PermissionError:
            raise ApprovalError(ApprovalCode.APPROVAL_KEY_UNAVAILABLE) from None

    def issue(self, *, manifest_sha256: str, staged_digest: str, target: str, approver: str,
              issued_at: datetime, lifetime: timedelta, approval_id: str | None = None) -> dict[str, Any]:
        """Operator tool on the Home Base: sign a single-use approval for one staged release."""
        if running_in_ci(self._environ):
            raise ApprovalError(ApprovalCode.CI_CANNOT_AUTHORIZE_DEPLOYMENT)
        if not self._registry.allows(approver):
            raise ApprovalError(ApprovalCode.APPROVER_NOT_REGISTERED)
        if not timedelta(0) < lifetime <= MAX_APPROVAL_LIFETIME:
            raise ApprovalError(ApprovalCode.APPROVAL_LIFETIME_INVALID)
        record: dict[str, Any] = {
            "schema": APPROVAL_SCHEMA, "approval_id": approval_id or secrets.token_hex(16),
            "manifest_sha256": manifest_sha256, "staged_digest": staged_digest, "target": target,
            "operation": OPERATION_DEPLOY, "approver": approver, "origin": ORIGIN_HUMAN,
            "issued_at": issued_at.astimezone(UTC).isoformat(),
            "expires_at": (issued_at + lifetime).astimezone(UTC).isoformat(),
        }
        record["signature"] = "0" * 64
        if not _shape_ok(record):
            raise ApprovalError(ApprovalCode.APPROVAL_MALFORMED)
        record["signature"] = self._mac(_payload(record))
        return record

    def _decide(self, record: Mapping[str, Any], binding: Binding, now: datetime) -> ApprovalDecision:
        if running_in_ci(self._environ) or _has_ci_markers(record):
            return ApprovalDecision(ApprovalCode.CI_CANNOT_AUTHORIZE_DEPLOYMENT)
        if not _shape_ok(record):
            return ApprovalDecision(ApprovalCode.APPROVAL_MALFORMED)
        approval_id = str(record["approval_id"])
        try:
            expected = self._mac(_payload(record))
        except ApprovalError as error:
            return ApprovalDecision(error.code, approval_id)
        if not hmac.compare_digest(expected, str(record["signature"])):
            return ApprovalDecision(ApprovalCode.APPROVAL_SIGNATURE_INVALID, approval_id)
        if not self._registry.allows(str(record["approver"])):
            return ApprovalDecision(ApprovalCode.APPROVER_NOT_REGISTERED, approval_id)
        if record["operation"] != OPERATION_DEPLOY:
            return ApprovalDecision(ApprovalCode.APPROVAL_OPERATION_INVALID, approval_id)
        if record["manifest_sha256"] != binding.manifest_sha256:
            return ApprovalDecision(ApprovalCode.APPROVAL_MANIFEST_MISMATCH, approval_id)
        if record["staged_digest"] != binding.staged_digest:
            return ApprovalDecision(ApprovalCode.APPROVAL_STAGED_DIGEST_MISMATCH, approval_id)
        if record["target"] != binding.target:
            return ApprovalDecision(ApprovalCode.APPROVAL_TARGET_MISMATCH, approval_id)
        try:
            issued, expires = _parse_time(record["issued_at"]), _parse_time(record["expires_at"])
        except ValueError:
            return ApprovalDecision(ApprovalCode.APPROVAL_MALFORMED, approval_id)
        if not timedelta(0) < expires - issued <= MAX_APPROVAL_LIFETIME:
            return ApprovalDecision(ApprovalCode.APPROVAL_LIFETIME_INVALID, approval_id)
        if issued > now:
            return ApprovalDecision(ApprovalCode.APPROVAL_NOT_YET_VALID, approval_id)
        if now >= expires:
            return ApprovalDecision(ApprovalCode.APPROVAL_EXPIRED, approval_id)
        try:
            if self._ledger.is_consumed(approval_id):
                return ApprovalDecision(ApprovalCode.APPROVAL_ALREADY_CONSUMED, approval_id)
        except ApprovalError as error:
            return ApprovalDecision(error.code, approval_id)
        return ApprovalDecision(ApprovalCode.AUTHORIZED, approval_id)

    def check(self, record: Mapping[str, Any], binding: Binding, *, now: datetime) -> ApprovalDecision:
        return self._decide(record, binding, now)

    def consume(self, record: Mapping[str, Any], binding: Binding, *, now: datetime) -> ApprovalDecision:
        decision = self._decide(record, binding, now)
        if not decision.ok:
            return decision
        try:
            self._ledger.append(record, consumed_at=now.astimezone(UTC).isoformat())
        except ApprovalError as error:
            return ApprovalDecision(error.code, decision.approval_id)
        return ApprovalDecision(ApprovalCode.CONSUMED, decision.approval_id)
