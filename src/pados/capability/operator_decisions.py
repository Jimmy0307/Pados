# SPDX-License-Identifier: AGPL-3.0-only
"""Operator-side issuance of human-gate decisions. Imported only by the operator entry module.

``pados.capability.operator_cli`` is the ONLY module under ``src/pados`` that imports this one, and nothing on
the execution side (executor, planner, registry, verification, inference, adapters, packs) imports either of
them (static AST test). The execution side receives only a ``HumanDecisionVerifier``, which cannot sign.

What issuing a decision authenticates (CR13b-01, under the existing operator-held HMAC model):

* the OS login session: ``open_operator_issuer`` requires an operator mapping that binds every approver to
  that approver's OWN SecretRef key AND OS account (``OperatorAccount``). The current process must BE that
  account - on POSIX the real and effective uid and the ``pwd`` login name, on Windows ``getpass.getuser()``
  plus ``USERDOMAIN`` (best effort: ``WINDOWS_NATIVE_VALIDATION = PENDING``) - else
  ``OPERATOR_SESSION_MISMATCH``. The account is read from the OS, never from a caller-supplied value;
* an interactive session: stdin and stdout must be TTYs (``OPERATOR_SESSION_NOT_INTERACTIVE``);
* an explicit human confirmation: ``approve`` asks, through the session's TTY reader, for the first 12 hex
  characters of the plan hash and for the task id; anything but an exact match refuses with
  ``OPERATOR_CONFIRMATION_MISMATCH`` and nothing is signed;
* no automation: any CI/automation environment marker or automation-looking approver name refuses.

What it does NOT authenticate: an attacker who controls the same OS account and its TTY (they ARE that
session), or in-process code that can resolve the operator's SecretRef through the boundary and compute the
HMAC itself. ``ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED`` remains the decision for the
symmetric-secret part: only an asymmetric, operator-held signing key removes the second residual.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from pados.capability.human_decision import (
    AUTOMATION_PRINCIPAL,
    DECISION_APPROVE,
    HUMAN_DECISION_SCHEMA,
    MAX_DECISION_LIFETIME,
    MAX_OPERATORS,
    MIN_KEY_BYTES,
    ORIGIN_HUMAN,
    HumanDecisionCode,
    HumanDecisionRefused,
    running_in_automation,
    shape_ok,
    signed_payload,
    valid_operator,
)
from pados.enterprise.secrets import SecretBoundary, is_secret_ref

WINDOWS_NATIVE_VALIDATION = "PENDING"  # the win32 account check is best effort until validated natively
CONFIRMATION_HASH_CHARS = 12
MAX_CONFIRMATION_CHARS = 128
_LOGIN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@$-]{0,63}$")
_DOMAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CONSTRUCTION = object()  # only ``open_operator_issuer`` builds an issuer


class OperatorIssuanceCode(StrEnum):
    OPERATOR_SESSION_MISMATCH = "OPERATOR_SESSION_MISMATCH"
    OPERATOR_SESSION_NOT_INTERACTIVE = "OPERATOR_SESSION_NOT_INTERACTIVE"
    OPERATOR_CONFIRMATION_MISMATCH = "OPERATOR_CONFIRMATION_MISMATCH"
    OPERATOR_PLATFORM_UNSUPPORTED = "OPERATOR_PLATFORM_UNSUPPORTED"


@dataclass(frozen=True)
class OperatorAccount:
    """An OS account. POSIX: ``login`` + ``uid``; Windows: ``login`` + ``domain`` (best effort)."""

    login: str
    uid: int | None = None
    domain: str | None = None

    def __post_init__(self) -> None:
        if type(self.login) is not str or not _LOGIN.fullmatch(self.login):
            raise ValueError("OPERATOR_ACCOUNT_INVALID")
        if self.uid is not None and (type(self.uid) is not int or self.uid < 0):
            raise ValueError("OPERATOR_ACCOUNT_INVALID")
        if self.domain is not None and (type(self.domain) is not str or not _DOMAIN.fullmatch(self.domain)):
            raise ValueError("OPERATOR_ACCOUNT_INVALID")


@dataclass(frozen=True)
class OperatorBinding:
    """One approver: that approver's OWN key reference and OS account."""

    key_ref: str
    account: OperatorAccount


class OperatorSession(Protocol):
    """The interactive terminal the approver works in. Production code cannot choose it: ``open_operator_issuer``
    takes no session argument and uses ``_session_factory`` (``TerminalSession``); only tests replace that
    private seam (CR13d: a caller-supplied session could fake interactivity and the typed confirmation)."""

    def interactive(self) -> bool:
        """True only when stdin AND stdout are TTYs."""

    def read_confirmation(self, prompt: str) -> str:
        """Show ``prompt`` and return one line the human typed."""


class TerminalSession:
    """The process's own terminal: stdin/stdout TTY check and a bounded line read from stdin."""

    def interactive(self) -> bool:
        try:
            return bool(sys.stdin is not None and sys.stdout is not None
                        and sys.stdin.isatty() and sys.stdout.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def read_confirmation(self, prompt: str) -> str:
        """Show the prompt on, and read the line from, the CONTROLLING terminal (never a redirected stdin)."""
        try:
            with open("/dev/tty", "r+", encoding="utf-8") as terminal:
                terminal.write(prompt)
                terminal.flush()
                return terminal.readline(MAX_CONFIRMATION_CHARS + 1)
        except OSError:
            raise HumanDecisionRefused(OperatorIssuanceCode.OPERATOR_SESSION_NOT_INTERACTIVE) from None


_session_factory: Callable[[], OperatorSession] = TerminalSession  # private test seam, never an API parameter


if sys.platform == "win32":

    def current_os_account() -> OperatorAccount:
        """CR13d-01: ``getpass``/``USERDOMAIN`` are environment-derived and spoofable, so issuance FAILS CLOSED on
        Windows until the identity comes from the process token (SID) and is validated natively
        (``WINDOWS_NATIVE_VALIDATION = PENDING``)."""
        raise HumanDecisionRefused(OperatorIssuanceCode.OPERATOR_PLATFORM_UNSUPPORTED)

    def _binding_complete(account: OperatorAccount) -> bool:
        return account.domain is not None

    def _same_account(bound: OperatorAccount, actual: OperatorAccount) -> bool:
        return (bound.login.casefold() == actual.login.casefold() and bound.domain is not None
                and actual.domain is not None and bound.domain.casefold() == actual.domain.casefold())
else:
    import pwd

    def current_os_account() -> OperatorAccount:
        """The account this process runs as: the real uid (which must equal the effective uid) and its login."""
        uid = os.getuid()
        if os.geteuid() != uid:
            raise HumanDecisionRefused(OperatorIssuanceCode.OPERATOR_SESSION_MISMATCH)
        return OperatorAccount(login=pwd.getpwuid(uid).pw_name, uid=uid)

    def _binding_complete(account: OperatorAccount) -> bool:
        return account.uid is not None

    def _same_account(bound: OperatorAccount, actual: OperatorAccount) -> bool:
        return bound.uid is not None and bound.uid == actual.uid and bound.login == actual.login


def _sign(boundary: SecretBoundary, key_ref: str, payload: bytes) -> str:
    """Resolve the approver's key for ONE MAC; the key never leaves this function."""
    try:
        with boundary.resolved({key_ref}, authorized_refs={key_ref}) as values:
            key = values[key_ref].reveal().encode("utf-8")
            if len(key) < MIN_KEY_BYTES:
                raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_KEY_UNAVAILABLE)
            return hmac.new(key, payload, hashlib.sha256).hexdigest()
    except HumanDecisionRefused:
        raise
    except Exception:  # noqa: BLE001 - an unresolvable key never signs (and never leaks into the error)
        raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_KEY_UNAVAILABLE) from None


def _validated_operators(operators: Mapping[str, OperatorBinding]) -> Mapping[str, OperatorBinding]:
    try:
        bindings = dict(operators)
    except (TypeError, ValueError) as exc:
        raise ValueError("OPERATOR_BINDINGS_INVALID") from exc
    if not bindings or len(bindings) > MAX_OPERATORS:
        raise ValueError("OPERATOR_BINDINGS_INVALID")
    for name, binding in bindings.items():
        if not isinstance(binding, OperatorBinding) or not isinstance(binding.account, OperatorAccount):
            raise ValueError("OPERATOR_BINDINGS_INVALID")
        if not valid_operator(name):
            raise ValueError("OPERATOR_BINDINGS_INVALID")
        if not is_secret_ref(binding.key_ref):
            raise ValueError("HUMAN_DECISION_KEY_REF_INVALID")
        if not _binding_complete(binding.account):
            raise ValueError("OPERATOR_ACCOUNT_BINDING_INCOMPLETE")
    if len({binding.key_ref for binding in bindings.values()}) != len(bindings):
        raise ValueError("HUMAN_DECISION_OPERATOR_KEY_SHARED")
    if len({binding.account for binding in bindings.values()}) != len(bindings):
        raise ValueError("OPERATOR_ACCOUNT_SHARED")  # one OS account never approves as two operators
    return MappingProxyType(bindings)


def _require_session(binding: OperatorBinding, session: OperatorSession, environ: Mapping[str, str]) -> None:
    """Automation markers, then the OS account, then the interactive terminal (fail closed on any error)."""
    if running_in_automation(environ):
        raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_AUTOMATION_CONTEXT)
    try:
        actual = current_os_account()
    except HumanDecisionRefused:
        raise
    except Exception:  # noqa: BLE001 - an account that cannot be read is not the bound account
        raise HumanDecisionRefused(OperatorIssuanceCode.OPERATOR_SESSION_MISMATCH) from None
    if not _same_account(binding.account, actual):
        raise HumanDecisionRefused(OperatorIssuanceCode.OPERATOR_SESSION_MISMATCH)
    try:
        interactive = session.interactive() is True
    except Exception:  # noqa: BLE001 - a session that cannot answer is not interactive
        interactive = False
    if not interactive:
        raise HumanDecisionRefused(OperatorIssuanceCode.OPERATOR_SESSION_NOT_INTERACTIVE)


def _typed(session: OperatorSession, prompt: str) -> str | None:
    try:
        line = session.read_confirmation(prompt)
    except Exception:  # noqa: BLE001 - no answer is never a confirmation
        return None
    if type(line) is not str or len(line) > MAX_CONFIRMATION_CHARS:
        return None
    return line.strip()


class OperatorDecisionIssuer:
    """One approver's signing tool (operator side only), bound to that approver's OS session."""

    __slots__ = ("_approver", "_binding", "_boundary", "_session", "_environ")

    def __init__(self, construction: object, *, approver: str, binding: OperatorBinding, boundary: SecretBoundary,
                 session: OperatorSession, environ: Mapping[str, str]) -> None:
        if construction is not _CONSTRUCTION:
            raise TypeError("OPERATOR_ISSUER_REQUIRES_open_operator_issuer")
        self._approver = approver
        self._binding = binding
        self._boundary = boundary
        self._session = session
        self._environ = environ

    @property
    def approver(self) -> str:
        return self._approver

    def approve(self, *, task_id: str, plan_hash: str, issued_at: datetime, lifetime: timedelta,
                decision_id: str | None = None) -> dict[str, str]:
        """The explicit approval action: the human at the bound session types the first 12 hex characters of
        ``plan_hash`` and ``task_id``; only then is APPROVE signed for exactly that task and plan (single use)."""
        _require_session(self._binding, self._session, self._environ)  # the action itself, not just the opening
        if not timedelta(0) < lifetime <= MAX_DECISION_LIFETIME:
            raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_LIFETIME_INVALID)
        if not isinstance(issued_at, datetime) or issued_at.tzinfo is None:
            raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_MALFORMED)
        record = {"schema": HUMAN_DECISION_SCHEMA, "decision_id": decision_id or secrets.token_hex(16),
                  "task_id": task_id, "plan_hash": plan_hash, "decision": DECISION_APPROVE,
                  "operator": self._approver, "origin": ORIGIN_HUMAN,
                  "issued_at": issued_at.astimezone(UTC).isoformat(),
                  "expires_at": (issued_at + lifetime).astimezone(UTC).isoformat(), "signature": "0" * 64}
        if not all(isinstance(value, str) for value in record.values()) or not shape_ok(record):
            raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_MALFORMED)
        typed_hash = _typed(self._session, f"Approve as {self._approver}: type the first "
                                           f"{CONFIRMATION_HASH_CHARS} hex characters of the reviewed plan hash: ")
        typed_task = _typed(self._session, "Type the task id: ")
        if not (typed_hash is not None and typed_task is not None
                and hmac.compare_digest(typed_hash.encode(), plan_hash[:CONFIRMATION_HASH_CHARS].encode())
                and hmac.compare_digest(typed_task.encode(), task_id.encode())):
            raise HumanDecisionRefused(OperatorIssuanceCode.OPERATOR_CONFIRMATION_MISMATCH)
        record["signature"] = _sign(self._boundary, self._binding.key_ref, signed_payload(record))
        return record


def open_operator_issuer(*, approver: str, operators: Mapping[str, OperatorBinding], boundary: SecretBoundary,
                         environ: Mapping[str, str] | None = None) -> OperatorDecisionIssuer:
    """An issuer for ONE human approver, opened only from that approver's own interactive OS session."""
    env = os.environ if environ is None else environ
    if running_in_automation(env):
        raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_AUTOMATION_CONTEXT)
    if not valid_operator(approver):
        raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_AUTOMATION_CONTEXT
                                   if isinstance(approver, str) and AUTOMATION_PRINCIPAL.search(approver)
                                   else HumanDecisionCode.HUMAN_DECISION_OPERATOR_NOT_REGISTERED)
    bindings = _validated_operators(operators)
    binding = bindings.get(approver)
    if binding is None:
        raise HumanDecisionRefused(HumanDecisionCode.HUMAN_DECISION_OPERATOR_NOT_REGISTERED)
    terminal = _session_factory()
    _require_session(binding, terminal, env)
    return OperatorDecisionIssuer(_CONSTRUCTION, approver=approver, binding=binding, boundary=boundary,
                                  session=terminal, environ=env)
