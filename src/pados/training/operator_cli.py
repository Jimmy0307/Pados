# SPDX-License-Identifier: AGPL-3.0-only
"""Operator-side entry module for signing training decisions (CR-12c TP-02).

``main`` is what the future ``pados training decide`` command calls on the operator's own machine; this is the
ONLY module allowed to import ``pados.training.operator`` (static test), and no training service module imports
this one. It loads the operator registry, opens a signer for the named approver (bound OS account, interactive
terminal, no automation markers), asks the human to type the confirmation on the terminal, and writes the
signed decision record (bound fields and MAC only, never key material) to a NEW file for the operator to hand
to the training side. The approver's key is resolved through ``SecretBoundary`` from the operator's own
environment (``secretref://env/<NAME>``) and never written anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.lifecycle.minimization import assert_minimized
from pados.training.decisions import (
    MAX_DECISION_LIFETIME,
    RECORD_FIELDS,
    DecisionError,
    DecisionOperation,
    TrainingOperatorRegistry,
)
from pados.training.operator import open_operator_signer

EXIT_OK = 0
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pados training decide",
                                     description="Sign one training decision as a registered human approver.")
    parser.add_argument("--operators", required=True, type=Path, help="operator registry document (v3)")
    parser.add_argument("--approver", required=True)
    parser.add_argument("--operation", required=True, choices=[op.value for op in DecisionOperation])
    parser.add_argument("--subject", required=True, help="subject hash shown by the training side")
    parser.add_argument("--lifetime-minutes", type=int, default=60)
    parser.add_argument("--out", required=True, type=Path, help="new file for the signed decision record")
    return parser


def _write_new(path: Path, record: dict[str, Any]) -> None:
    assert_minimized(record, allowed_keys=RECORD_FIELDS)
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def main(argv: Sequence[str] | None = None) -> int:
    """Sign one decision; ``0`` on success, ``2`` with the refusal code on stderr otherwise."""
    args = _parser().parse_args(argv)
    lifetime = timedelta(minutes=args.lifetime_minutes)
    if not timedelta(0) < lifetime <= MAX_DECISION_LIFETIME:
        print("DECISION_LIFETIME_INVALID", file=sys.stderr)
        return EXIT_REFUSED
    try:
        registry = TrainingOperatorRegistry.from_file(args.operators)
        signer = open_operator_signer(approver=args.approver, registry=registry,
                                      boundary=SecretBoundary(EnvironmentSecretResolver()))
        record = signer.issue(operation=DecisionOperation(args.operation), subject_hash=args.subject,
                              issued_at=datetime.now(UTC), lifetime=lifetime)
        _write_new(args.out, record)
    except DecisionError as error:
        print(error.code.value, file=sys.stderr)
        return EXIT_REFUSED
    except FileExistsError:
        print("DECISION_OUTPUT_EXISTS", file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, ValueError):
        print("DECISION_INPUT_INVALID", file=sys.stderr)
        return EXIT_REFUSED
    print(f"DECISION_ISSUED {record['decision_id']} {record['operation']} {record['subject_hash']}")
    return EXIT_OK
