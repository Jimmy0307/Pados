# SPDX-License-Identifier: AGPL-3.0-only
"""Human-gate decisions for capability plans: verified, bound, short-lived, single use. Never acceptance.

A task with ``human_gate`` runs only after a ``HumanDecisionVerifier`` confirms an affirmative, current,
unexpired decision made by a registered human operator for exactly that task id AND that plan hash. A
syntactically valid reference proves nothing. This module only VERIFIES. Issuance is a separate,
operator-side capability (``pados.capability.operator_decisions``, reached only through the operator entry
module ``pados.capability.operator_cli``, and bound to the approver's OS account, an interactive terminal and
a typed confirmation); no execution-side module or pack imports either of them (static test), and the
executor refuses a verifier that exposes any signing method.

``HmacHumanDecisionVerifier`` (the capability-local V1 verifier; the same concept as the deployment approval
authority, implemented here without importing it):

* per-operator keys: ``operator_keys`` maps every registered operator to that operator's OWN SecretRef. A
  record is checked only under the key of the operator it names, so naming an operator without holding that
  operator's key never verifies. The verifier holds only key REFERENCES and the boundary; the key is resolved
  inside ``_mac_matches`` for one comparison at verification time and nothing is retained afterwards (no key
  bytes, no resolved value, no MAC, no cache); it never returns a MAC and has no issue/sign method;
* the caller's record is read exactly ONCE into a plain ``dict`` of plain ``str`` (non-plain mappings or
  values are refused); validation, authentication, consumption and the returned reference all use that
  snapshot only, so a mapping that answers differently on each read gains nothing;
* ``origin`` must be ``HUMAN_OPERATOR``; any CI/automation environment marker or automation-looking
  operator/origin refuses regardless of signature;
* ``expires_at`` is mandatory and at most 24 hours after ``issued_at``; not-yet-valid and expired refuse;
* single use: the decision id is consumed through a ``ConsumedDecisionLedger`` with an atomic
  consume-if-unused before execution starts. Gated execution requires a durable, process-shared ledger
  (``decision_ledger.FileConsumedDecisionLedger``).

RESIDUAL: ``ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED``. HMAC is symmetric: in-process code that
reaches this verifier's boundary (or any other boundary able to resolve an operator's SecretRef) can compute a
MAC without the operator's terminal session. Module separation is not an isolation boundary inside one Python
process. Removing that residual needs an asymmetric operator-held signing key, i.e. a new crypto dependency and
alignment with the frozen governance primitives, which use the same operator-held HMAC model.

The executor records only the verified decision id as ``PlanReceipt.human_decision_ref`` — a reference to
evidence, never semantic acceptance (that still happens only through receipt -> validation -> CAS).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from pados.enterprise.secrets import SecretBoundary, is_secret_ref

HUMAN_DECISION_SCHEMA = "pados.capability.human-decision/v1"
ORIGIN_HUMAN = "HUMAN_OPERATOR"
DECISION_APPROVE = "APPROVE"
MAX_DECISION_LIFETIME = timedelta(hours=24)
MIN_KEY_BYTES = 32
MAX_OPERATORS = 64
SIGNED_FIELDS = ("schema", "decision_id", "task_id", "plan_hash", "decision", "operator", "origin", "issued_at",
                 "expires_at")
RECORD_FIELDS = frozenset({*SIGNED_FIELDS, "signature"})
CI_ENVIRONMENT_MARKERS = ("CI", "GITHUB_ACTIONS", "GITHUB_RUN_ID", "ACTIONS_ID_TOKEN_REQUEST_URL", "RUNNER_TEMP",
                          "GITLAB_CI", "BUILDKITE", "JENKINS_URL", "TF_BUILD")
# Any of these on a verifier means it can mint or expose signatures: the executor refuses such a verifier.
SIGNING_ATTRIBUTES = ("issue", "sign", "approve", "mac", "_mac", "signature_for", "issuer", "key", "_key")
AUTOMATION_PRINCIPAL = re.compile(r"\[bot\]|github-actions|dependabot|^ci[:/._-]|^runner[:/._-]|workflow|automation",
                                  re.IGNORECASE)
OPERATOR_NAME = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_MAX_FIELD = 128


class HumanDecisionCode(StrEnum):
    VERIFIED = "VERIFIED"
    HUMAN_DECISION_AUTOMATION_CONTEXT = "HUMAN_DECISION_AUTOMATION_CONTEXT"
    HUMAN_DECISION_MALFORMED = "HUMAN_DECISION_MALFORMED"
    HUMAN_DECISION_KEY_UNAVAILABLE = "HUMAN_DECISION_KEY_UNAVAILABLE"
    HUMAN_DECISION_SIGNATURE_INVALID = "HUMAN_DECISION_SIGNATURE_INVALID"
    HUMAN_DECISION_OPERATOR_NOT_REGISTERED = "HUMAN_DECISION_OPERATOR_NOT_REGISTERED"
    HUMAN_DECISION_NOT_AFFIRMATIVE = "HUMAN_DECISION_NOT_AFFIRMATIVE"
    HUMAN_DECISION_TASK_MISMATCH = "HUMAN_DECISION_TASK_MISMATCH"
    HUMAN_DECISION_PLAN_MISMATCH = "HUMAN_DECISION_PLAN_MISMATCH"
    HUMAN_DECISION_LIFETIME_INVALID = "HUMAN_DECISION_LIFETIME_INVALID"
    HUMAN_DECISION_NOT_YET_VALID = "HUMAN_DECISION_NOT_YET_VALID"
    HUMAN_DECISION_EXPIRED = "HUMAN_DECISION_EXPIRED"
    HUMAN_DECISION_ALREADY_CONSUMED = "HUMAN_DECISION_ALREADY_CONSUMED"
    HUMAN_DECISION_LEDGER_FULL = "HUMAN_DECISION_LEDGER_FULL"
    HUMAN_DECISION_LEDGER_CORRUPT = "HUMAN_DECISION_LEDGER_CORRUPT"
    HUMAN_DECISION_LEDGER_UNAVAILABLE = "HUMAN_DECISION_LEDGER_UNAVAILABLE"
    HUMAN_DECISION_LEDGER_UNSAFE = "HUMAN_DECISION_LEDGER_UNSAFE"
    HUMAN_DECISION_LEDGER_TORN_ENTRY = "HUMAN_DECISION_LEDGER_TORN_ENTRY"


class HumanDecisionRefused(PermissionError):
    def __init__(self, code: HumanDecisionCode | str) -> None:
        super().__init__(str(code))
        self.code = str(code)


@dataclass(frozen=True)
class VerifiedHumanDecision:
    """What the executor may record: a reference to verified evidence (not acceptance)."""

    decision_ref: str
    task_id: str
    plan_hash: str
    operator: str
    expires_at: str


class HumanDecisionVerifier(Protocol):
    @property
    def durable_single_use(self) -> bool:
        """True only when consumption goes to a durable, process-shared ledger (required for gated tasks)."""

    def verify_and_consume(self, record: Mapping[str, Any], *, task_id: str, plan_hash: str,
                           now: datetime) -> VerifiedHumanDecision:
        """Raise ``HumanDecisionRefused`` unless the decision is valid for exactly this task and plan;
        on success the decision is consumed (it can never authorize a second execution)."""


class ConsumedDecisionLedger(Protocol):
    @property
    def durable(self) -> bool: ...

    def is_consumed(self, decision_id: str) -> bool: ...

    def consume(self, decision_id: str) -> None:
        """Atomic consume-if-unused: refuse ``HUMAN_DECISION_ALREADY_CONSUMED`` when already recorded."""


def running_in_automation(environ: Mapping[str, str]) -> bool:
    return any(marker in environ for marker in CI_ENVIRONMENT_MARKERS)


def signing_capable(candidate: object) -> bool:
    """True when ``candidate`` exposes anything that can mint a decision or a MAC (fail closed)."""
    try:
        return any(hasattr(candidate, name) for name in SIGNING_ATTRIBUTES)
    except Exception:  # noqa: BLE001 - an object that cannot be inspected is treated as signing-capable
        return True


def snapshot(record: object) -> dict[str, str] | None:
    """Read the caller's record ONCE into a plain dict of plain ``str``; ``None`` for anything else.

    Only an exact ``dict`` with exact ``str`` keys and values is accepted (no subclasses, no custom mappings),
    so nothing the caller controls runs during or after the copy."""
    if type(record) is not dict:
        return None
    items = list(dict.items(record))
    if len(items) != len(RECORD_FIELDS):
        return None
    copied: dict[str, str] = {}
    for key, value in items:
        if type(key) is not str or type(value) is not str or len(value) > _MAX_FIELD:
            return None
        copied[key] = value
    return copied if set(copied) == RECORD_FIELDS else None


def signed_payload(record: Mapping[str, str]) -> bytes:
    """Canonical bytes of the signed fields (shared by the verifier and the operator issuer)."""
    return json.dumps({key: record[key] for key in SIGNED_FIELDS}, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


def shape_ok(record: Mapping[str, str]) -> bool:
    """Closed record shape (call on a ``snapshot``)."""
    return bool(set(record) == RECORD_FIELDS and record["schema"] == HUMAN_DECISION_SCHEMA
                and _HEX32.fullmatch(record["decision_id"]) and _TASK_ID.fullmatch(record["task_id"])
                and _HEX64.fullmatch(record["plan_hash"]) and OPERATOR_NAME.fullmatch(record["operator"])
                and _HEX64.fullmatch(record["signature"]))


def valid_operator(name: object) -> bool:
    return isinstance(name, str) and bool(OPERATOR_NAME.fullmatch(name)) and not AUTOMATION_PRINCIPAL.search(name)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return parsed


def _mac_matches(boundary: SecretBoundary, key_ref: str, payload: bytes, signature: str) -> bool:
    """Resolve ONE operator key for ONE comparison. Returns only the verdict; the key and MAC never escape."""
    try:
        with boundary.resolved({key_ref}, authorized_refs={key_ref}) as values:
            key = values[key_ref].reveal().encode("utf-8")
            if len(key) < MIN_KEY_BYTES:
                raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_KEY_UNAVAILABLE)
            return hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).hexdigest(), signature)
    except HumanDecisionRefused:
        raise
    except Exception:  # noqa: BLE001 - an unresolvable key never verifies (and never leaks into the error)
        raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_KEY_UNAVAILABLE) from None


class HmacHumanDecisionVerifier:
    """V1 ``HumanDecisionVerifier``: HMAC-SHA256 under per-operator, operator-held keys (SecretRef only).

    Verification only: there is no issue/sign method and no key material on the object."""

    __slots__ = ("_operator_keys", "_boundary", "_ledger", "_environ")

    def __init__(self, *, operator_keys: Mapping[str, str], boundary: SecretBoundary,
                 ledger: ConsumedDecisionLedger, environ: Mapping[str, str] | None = None) -> None:
        keys = dict(operator_keys)
        if not keys or len(keys) > MAX_OPERATORS or any(not valid_operator(op) for op in keys):
            raise ValueError("HUMAN_DECISION_OPERATOR_INVALID")
        if any(not is_secret_ref(ref) for ref in keys.values()):
            raise ValueError("HUMAN_DECISION_KEY_REF_INVALID")
        if len(set(keys.values())) != len(keys):
            raise ValueError("HUMAN_DECISION_OPERATOR_KEY_SHARED")  # every operator holds its own key
        if not (callable(getattr(ledger, "consume", None)) and callable(getattr(ledger, "is_consumed", None))):
            raise ValueError("HUMAN_DECISION_LEDGER_REQUIRED")
        self._operator_keys: Mapping[str, str] = MappingProxyType(keys)
        self._boundary = boundary
        self._ledger = ledger
        self._environ = os.environ if environ is None else environ

    @property
    def operators(self) -> frozenset[str]:
        return frozenset(self._operator_keys)

    @property
    def durable_single_use(self) -> bool:
        try:
            return self._ledger.durable is True
        except Exception:  # noqa: BLE001 - a ledger that cannot say it is durable is not durable
            return False

    def check(self, record: object, *, task_id: str, plan_hash: str, now: datetime) -> HumanDecisionCode:
        """Every check except consumption; returns a closed code (``VERIFIED`` when valid)."""
        copied = snapshot(record)
        if copied is None:
            return HumanDecisionCode.HUMAN_DECISION_MALFORMED
        return self._check(copied, task_id=task_id, plan_hash=plan_hash, now=now)

    def verify_and_consume(self, record: Mapping[str, Any], *, task_id: str, plan_hash: str,
                           now: datetime) -> VerifiedHumanDecision:
        copied = snapshot(record)  # the ONLY read of the caller's object
        if copied is None:
            raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_MALFORMED)
        code = self._check(copied, task_id=task_id, plan_hash=plan_hash, now=now)
        if code is not HumanDecisionCode.VERIFIED:
            raise HumanDecisionRefused(code)
        self._ledger.consume(copied["decision_id"])
        return VerifiedHumanDecision(f"human-decision:{copied['decision_id']}", copied["task_id"],
                                     copied["plan_hash"], copied["operator"], copied["expires_at"])

    def _check(self, record: dict[str, str], *, task_id: str, plan_hash: str, now: datetime) -> HumanDecisionCode:
        if running_in_automation(self._environ):
            return HumanDecisionCode.HUMAN_DECISION_AUTOMATION_CONTEXT
        if not shape_ok(record):
            return HumanDecisionCode.HUMAN_DECISION_MALFORMED
        if record["origin"] != ORIGIN_HUMAN or AUTOMATION_PRINCIPAL.search(record["operator"]):
            return HumanDecisionCode.HUMAN_DECISION_AUTOMATION_CONTEXT
        key_ref = self._operator_keys.get(record["operator"])
        if key_ref is None:  # only the NAMED operator's own key can verify its record
            return HumanDecisionCode.HUMAN_DECISION_OPERATOR_NOT_REGISTERED
        try:
            authentic = _mac_matches(self._boundary, key_ref, signed_payload(record), record["signature"])
        except HumanDecisionRefused as exc:
            return HumanDecisionCode(exc.code)
        if not authentic:
            return HumanDecisionCode.HUMAN_DECISION_SIGNATURE_INVALID
        if record["decision"] != DECISION_APPROVE:
            return HumanDecisionCode.HUMAN_DECISION_NOT_AFFIRMATIVE
        if record["task_id"] != task_id:
            return HumanDecisionCode.HUMAN_DECISION_TASK_MISMATCH
        if not hmac.compare_digest(record["plan_hash"], plan_hash):
            return HumanDecisionCode.HUMAN_DECISION_PLAN_MISMATCH
        try:
            issued, expires = _timestamp(record["issued_at"]), _timestamp(record["expires_at"])
        except ValueError:
            return HumanDecisionCode.HUMAN_DECISION_MALFORMED
        if not timedelta(0) < expires - issued <= MAX_DECISION_LIFETIME:
            return HumanDecisionCode.HUMAN_DECISION_LIFETIME_INVALID
        if issued > now:
            return HumanDecisionCode.HUMAN_DECISION_NOT_YET_VALID
        if now >= expires:
            return HumanDecisionCode.HUMAN_DECISION_EXPIRED
        try:
            if self._ledger.is_consumed(record["decision_id"]):
                return HumanDecisionCode.HUMAN_DECISION_ALREADY_CONSUMED
        except HumanDecisionRefused as exc:
            return HumanDecisionCode(exc.code) if exc.code in HumanDecisionCode.__members__ else (
                HumanDecisionCode.HUMAN_DECISION_LEDGER_UNAVAILABLE)
        return HumanDecisionCode.VERIFIED
