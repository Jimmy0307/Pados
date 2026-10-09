# SPDX-License-Identifier: AGPL-3.0-only
"""Operator entry point for human-gate approvals (``python -m pados.capability.operator_cli``).

This is the ONLY module under ``src/pados`` that imports ``pados.capability.operator_decisions``; no
execution-side fabric module and no pack imports either of them (static AST test). A human operator runs it in
their own interactive terminal:

    python -m pados.capability.operator_cli --operators BINDINGS.json --approver NAME \\
        --task-id TASK --plan-hash PLAN_HASH --out DECISION.json [--lifetime-minutes 60]

``BINDINGS.json`` is the operator mapping (``pados.capability.operator-bindings/v1``): approver name ->
``key_ref`` (that approver's own SecretRef), ``login`` and ``uid`` (POSIX) or ``domain`` (Windows, best
effort). It must be a regular file the current user owns with no group/other access (POSIX), otherwise
``OPERATOR_BINDINGS_UNSAFE``. The issuer then requires that this process runs as the bound OS account, that
stdin and stdout are TTYs, and that the human types the first 12 hex characters of the plan hash and the task
id. The signed record is written to ``--out`` (created exclusively, owner-only, minimized), never printed.
Every refusal prints its closed code to stderr and exits 1; nothing is signed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pados.capability.human_decision import RECORD_FIELDS, HumanDecisionRefused
from pados.capability.operator_decisions import (
    OperatorAccount,
    OperatorBinding,
    open_operator_issuer,
)
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized

BINDINGS_SCHEMA = "pados.capability.operator-bindings/v1"
MAX_BINDINGS_BYTES = 64 * 1024
MAX_LIFETIME_MINUTES = 24 * 60
_BINDING_KEYS = frozenset({"key_ref", "login", "uid", "domain"})
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

if sys.platform == "win32":
    _READ_FLAGS = os.O_RDONLY | os.O_BINARY
    _CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY

    def _owner_only(info: os.stat_result) -> bool:
        return True  # ACCEPTED_RISK_WINDOWS: NTFS ACLs are not modelled by st_mode
else:
    _READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    _CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

    def _owner_only(info: os.stat_result) -> bool:
        return info.st_uid == os.getuid() and not info.st_mode & 0o077


class OperatorCliError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_bindings_file(path: Path) -> bytes:
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as exc:
        raise OperatorCliError("OPERATOR_BINDINGS_UNAVAILABLE") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not _owner_only(info):
            raise OperatorCliError("OPERATOR_BINDINGS_UNSAFE")
        if info.st_size > MAX_BINDINGS_BYTES:
            raise OperatorCliError("OPERATOR_BINDINGS_INVALID")
        with os.fdopen(os.dup(fd), "rb") as handle:
            return handle.read(MAX_BINDINGS_BYTES + 1)
    finally:
        os.close(fd)


def load_bindings(path: Path) -> dict[str, OperatorBinding]:
    """Parse the operator mapping strictly (closed schema; every approver bound to an OS account)."""
    raw = _read_bindings_file(path)
    try:
        document = json.loads(raw.decode("utf-8"))
        if not (isinstance(document, dict) and set(document) == {"schema", "operators"}
                and document["schema"] == BINDINGS_SCHEMA and isinstance(document["operators"], dict)):
            raise ValueError("shape")
        bindings: dict[str, OperatorBinding] = {}
        for name, entry in document["operators"].items():
            if not (isinstance(entry, dict) and {"key_ref", "login"} <= set(entry) <= _BINDING_KEYS):
                raise ValueError("entry")
            account = OperatorAccount(login=entry["login"], uid=entry.get("uid"), domain=entry.get("domain"))
            if not isinstance(entry["key_ref"], str):
                raise ValueError("key_ref")
            bindings[str(name)] = OperatorBinding(key_ref=entry["key_ref"], account=account)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise OperatorCliError("OPERATOR_BINDINGS_INVALID") from exc
    return bindings


def _write_record(path: Path, record: Mapping[str, str]) -> None:
    try:
        assert_minimized(dict(record), allowed_keys=RECORD_FIELDS, max_string=128, max_items=16,
                         max_total_bytes=4096)
    except DataMinimizationViolation as exc:
        raise OperatorCliError("OPERATOR_DECISION_NOT_MINIMIZED") from exc
    data = (json.dumps(dict(record), sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    try:
        fd = os.open(path, _CREATE_FLAGS, 0o600)
    except OSError as exc:
        raise OperatorCliError("OPERATOR_DECISION_OUTPUT_UNAVAILABLE") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if type(written) is not int or not 0 < written <= len(view):
                raise OperatorCliError("OPERATOR_DECISION_OUTPUT_UNAVAILABLE")
            view = view[written:]
        os.fsync(fd)
    except OSError as exc:
        raise OperatorCliError("OPERATOR_DECISION_OUTPUT_UNAVAILABLE") from exc
    finally:
        os.close(fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pados-operator-approve",
                                     description="Approve ONE capability plan as a human operator.")
    parser.add_argument("--operators", required=True, type=Path, help="operator bindings JSON (owner-only)")
    parser.add_argument("--approver", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--plan-hash", required=True)
    parser.add_argument("--out", required=True, type=Path, help="decision record to create (must not exist)")
    parser.add_argument("--lifetime-minutes", type=int, default=60)
    return parser


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         clock: Callable[[], datetime] | None = None) -> int:
    """Run one approval. ``environ``/``clock`` exist for tests; the command line never sets them. The terminal
    session is never a parameter (see ``operator_decisions._session_factory``)."""
    args = _parser().parse_args(argv)
    env = os.environ if environ is None else environ
    now = clock or (lambda: datetime.now(UTC))
    try:
        if not 0 < args.lifetime_minutes <= MAX_LIFETIME_MINUTES:
            raise OperatorCliError("HUMAN_DECISION_LIFETIME_INVALID")
        issuer = open_operator_issuer(approver=args.approver, operators=load_bindings(args.operators),
                                      boundary=SecretBoundary(EnvironmentSecretResolver(env)), environ=env)
        record = issuer.approve(task_id=args.task_id, plan_hash=args.plan_hash, issued_at=now(),
                                lifetime=timedelta(minutes=args.lifetime_minutes))
        _write_record(args.out, record)
    except (HumanDecisionRefused, OperatorCliError) as exc:
        _report(str(exc.code))
        return 1
    except ValueError as exc:
        _report(_closed(exc))
        return 1
    sys.stdout.write(f"APPROVED decision_id={record['decision_id']} task_id={record['task_id']}\n")
    return 0


def _closed(exc: Exception) -> str:
    text: Any = exc.args[0] if exc.args else ""
    return text if isinstance(text, str) and _CODE.fullmatch(text) else "OPERATOR_APPROVAL_REFUSED"


def _report(code: str) -> None:
    sys.stderr.write(f"REFUSED {code}\n")


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
