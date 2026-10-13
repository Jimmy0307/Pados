# SPDX-License-Identifier: AGPL-3.0-only
"""Operator-side decision signing (CR-12 TP-02, CR-12b TP-02a, CR-12c TP-02). Imported only by the operator entry
module ``pados.training.operator_cli``; never by jobs, backends, evaluation, registry, contracts, decisions or
``_chain`` (enforced by a static test).

``DecisionSigner`` is the only object that can issue a signed training decision. It is opened through
``open_operator_signer(approver=...)`` from ``operator_cli.main`` - the future ``pados training decide``
command on the operator's own machine - and is never handed to the training process, which receives a
verify-only ``DecisionVerifier`` instead (static test and runtime type check).

Every signature requires, checked when the signer is opened AND again inside ``issue``:

1. no CI or automation marker in the environment, and a registered, enabled human approver;
2. an authenticated OS login session: the process runs as the OS account the operator registry binds to
   that approver (POSIX: numeric uid from ``os.getuid()`` plus the ``pwd`` login name), otherwise
   ``OPERATOR_SESSION_MISMATCH``. On Windows signing FAILS CLOSED (``OPERATOR_PLATFORM_UNSUPPORTED``): the
   environment-derived account name is spoofable, and a token/SID-based identity is PENDING native validation;
3. an interactive session: stdin and stdout are terminals, otherwise ``OPERATOR_SESSION_NOT_INTERACTIVE``;
4. explicit human confirmation: the operation name and the first 12 hex characters of the subject hash,
   typed on the controlling terminal, otherwise ``OPERATOR_CONFIRMATION_MISMATCH``;
5. the approver's own key, resolved through ``SecretBoundary`` from the registry's key reference
   (``DECISION_KEY_UNAVAILABLE``).

What this authenticates: that the decision was signed from the approver's own OS login, in an interactive
terminal, after someone at that terminal typed the confirmation. What it does NOT authenticate: an attacker
who already runs code as that same OS account and controls its terminal (or a pseudo-terminal) can pass
every check; and with symmetric HMAC, any code that can resolve the approver's SecretRef can compute the
MAC without this module (``ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED``, see the Training Plane
doc).

``_current_session`` and ``_read_confirmation`` are private test seams (not exported); production never
overrides them. This module holds the only MAC-producing code in the package (a static test enforces it).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

from pados.enterprise.secrets import SecretBoundary
from pados.training import decisions
from pados.training.decisions import (
    AUTOMATION_PRINCIPAL,
    DECISION_SCHEMA,
    MAX_DECISION_LIFETIME,
    MIN_KEY_BYTES,
    ORIGIN_HUMAN,
    DecisionCode,
    DecisionError,
    DecisionOperation,
    OsAccount,
    TrainingOperatorRegistry,
    running_in_automation,
    shape_ok,
    signed_payload,
)

CONFIRMATION_PREFIX_HEX = 12
MAX_CONFIRMATION_CHARS = 256


@dataclass(frozen=True)
class OperatorSession:
    """The OS login and terminal state of the signing process."""

    platform: str
    login: str
    uid: int | None
    domain: str | None
    interactive: bool

    def is_account(self, account: OsAccount) -> bool:
        if self.platform != account.platform:
            return False
        if account.platform == "posix":
            return self.uid is not None and self.uid == account.uid and self.login == account.login
        return (self.login.casefold() == account.login.casefold() and self.domain is not None
                and account.domain is not None and self.domain.casefold() == account.domain.casefold())


@dataclass(frozen=True)
class ConfirmationRequest:
    """What the human is asked to confirm on the terminal before the decision is signed."""

    approver: str
    operation: DecisionOperation
    subject_hash: str
    expires_at: str

    @property
    def expected(self) -> str:
        return f"{self.operation.value} {self.subject_hash[:CONFIRMATION_PREFIX_HEX]}"


def _is_tty(stream: TextIO | None) -> bool:
    try:
        return stream is not None and stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _current_session() -> OperatorSession:
    """This process's OS account and whether it runs in an interactive terminal. Private test seam."""
    interactive = _is_tty(sys.stdin) and _is_tty(sys.stdout)
    if sys.platform == "win32":
        # CR-12d-02: getpass/USERNAME/USERDOMAIN are environment-derived and spoofable. Signing fails closed on
        # Windows until the identity comes from the authenticated process token (SID) and is validated
        # natively (WINDOWS_NATIVE_VALIDATION = PENDING).
        raise DecisionError(DecisionCode.OPERATOR_PLATFORM_UNSUPPORTED)
    else:
        import pwd

        uid = os.getuid()
        return OperatorSession("posix", pwd.getpwuid(uid).pw_name, uid, None, interactive)


def _prompt(request: ConfirmationRequest) -> str:
    return (f"\nPADOS training decision, signing as {request.approver}\n"
            f"  operation : {request.operation.value}\n  subject   : {request.subject_hash}\n"
            f"  expires   : {request.expires_at}\n"
            f"Type the operation and the first {CONFIRMATION_PREFIX_HEX} characters of the subject to sign: ")


def _read_confirmation(request: ConfirmationRequest) -> str:
    """Show the decision on the controlling terminal and read the typed confirmation from it (never from a
    redirected stdin). Private test seam."""
    try:
        if sys.platform == "win32":
            with open("CONOUT$", "w", encoding="utf-8") as screen, open("CONIN$", encoding="utf-8") as keyboard:
                screen.write(_prompt(request))
                screen.flush()
                return keyboard.readline(MAX_CONFIRMATION_CHARS)
        with open("/dev/tty", "r+", encoding="utf-8") as terminal:
            terminal.write(_prompt(request))
            terminal.flush()
            return terminal.readline(MAX_CONFIRMATION_CHARS)
    except OSError:
        raise DecisionError(DecisionCode.OPERATOR_SESSION_NOT_INTERACTIVE) from None


def _sign(boundary: SecretBoundary, key_ref: str, record: dict[str, Any]) -> str:
    """MAC over one decision record under the approver's own key; the key is resolved and dropped here."""
    try:
        with boundary.resolved({key_ref}, authorized_refs={key_ref}) as values:
            key = values[key_ref].reveal().encode("utf-8")
            if len(key) < MIN_KEY_BYTES:
                raise DecisionError(DecisionCode.DECISION_KEY_UNAVAILABLE)
            return hmac.new(key, signed_payload(record), hashlib.sha256).hexdigest()
    except PermissionError:
        raise DecisionError(DecisionCode.DECISION_KEY_UNAVAILABLE) from None


def _key_available(boundary: SecretBoundary, key_ref: str) -> None:
    try:
        with boundary.resolved({key_ref}, authorized_refs={key_ref}) as values:
            if len(values[key_ref].reveal().encode("utf-8")) < MIN_KEY_BYTES:
                raise DecisionError(DecisionCode.DECISION_KEY_UNAVAILABLE)
    except PermissionError:
        raise DecisionError(DecisionCode.DECISION_KEY_UNAVAILABLE) from None


def _operator_session(approver: str, registry: TrainingOperatorRegistry) -> str:
    """Preconditions 1-3 of the module docstring; returns the approver's own key reference."""
    if running_in_automation(decisions._environment()):
        raise DecisionError(DecisionCode.AUTOMATION_CANNOT_DECIDE)
    if not isinstance(approver, str) or AUTOMATION_PRINCIPAL.search(approver):
        raise DecisionError(DecisionCode.AUTOMATION_CANNOT_DECIDE)
    if not isinstance(registry, TrainingOperatorRegistry):
        raise DecisionError(DecisionCode.APPROVER_NOT_REGISTERED)
    key_ref, account = registry.key_ref(approver), registry.os_account(approver)
    if key_ref is None or account is None:
        raise DecisionError(DecisionCode.APPROVER_NOT_REGISTERED)
    session = _current_session()
    if not session.is_account(account):
        raise DecisionError(DecisionCode.OPERATOR_SESSION_MISMATCH)
    if not session.interactive:
        raise DecisionError(DecisionCode.OPERATOR_SESSION_NOT_INTERACTIVE)
    return key_ref


class DecisionSigner:
    """Signs single-use decisions as ONE registered approver with that approver's own key, from that approver's
    own interactive OS session and after typed confirmation. Operator tool only."""

    def __init__(self, *, approver: str, registry: TrainingOperatorRegistry, boundary: SecretBoundary) -> None:
        self._approver = approver
        self._registry = registry
        self._boundary = boundary

    @property
    def approver(self) -> str:
        return self._approver

    def issue(self, *, operation: DecisionOperation, subject_hash: str, issued_at: datetime, lifetime: timedelta,
              decision_id: str | None = None) -> dict[str, Any]:
        key_ref = _operator_session(self._approver, self._registry)
        if not timedelta(0) < lifetime <= MAX_DECISION_LIFETIME:
            raise DecisionError(DecisionCode.DECISION_LIFETIME_INVALID)
        record: dict[str, Any] = {
            "schema": DECISION_SCHEMA, "decision_id": decision_id or secrets.token_hex(16),
            "operation": DecisionOperation(operation).value, "subject_hash": subject_hash,
            "approver": self._approver, "origin": ORIGIN_HUMAN, "issued_at": issued_at.astimezone(UTC).isoformat(),
            "expires_at": (issued_at + lifetime).astimezone(UTC).isoformat(), "signature": "0" * 64,
        }
        if not shape_ok(record):
            raise DecisionError(DecisionCode.DECISION_MALFORMED)
        request = ConfirmationRequest(self._approver, DecisionOperation(operation), subject_hash,
                                      record["expires_at"])
        answer = _read_confirmation(request)
        if not isinstance(answer, str) or " ".join(answer.split()) != request.expected:
            raise DecisionError(DecisionCode.OPERATOR_CONFIRMATION_MISMATCH)
        record["signature"] = _sign(self._boundary, key_ref, record)
        return record


def open_operator_signer(*, approver: str, boundary: SecretBoundary,
                         registry: TrainingOperatorRegistry) -> DecisionSigner:
    """Operator tool entry point (``operator_cli.main``, future ``pados training decide``) for one approver.

    Refuses automation contexts and principals, unregistered approvers, a process not running as the
    approver's bound OS account, a non-interactive session, and an approver whose OWN registered key cannot be
    resolved through ``SecretBoundary`` (``DECISION_KEY_UNAVAILABLE``)."""
    key_ref = _operator_session(approver, registry)
    _key_available(boundary, key_ref)
    return DecisionSigner(approver=approver, registry=registry, boundary=boundary)
