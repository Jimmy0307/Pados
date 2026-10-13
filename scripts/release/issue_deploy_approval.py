# SPDX-License-Identifier: AGPL-3.0-only
"""Operator tool on the Home Base: sign a single-use deployment approval.

Usage: issue_deploy_approval.py OUT.json --manifest-sha256 HEX --staged-digest HEX --target HOME_BASE_ID
           --approver OPERATOR_ID --operators OPERATORS.json --approval-key-ref secretref://env/NAME
           --ledger CONSUMED.jsonl [--lifetime-hours H]   (default 1, maximum 24)
The HMAC key is resolved through SecretBoundary from the SecretRef and never
printed or written. The approver must be an enabled entry of the operator
registry. Refused inside GitHub Actions (CI_CANNOT_AUTHORIZE_DEPLOYMENT).
Issuing an approval deploys nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary  # noqa: E402
from pados.release.approval import (  # noqa: E402
    ApprovalError,
    ConsumedApprovalLedger,
    HmacApprovalAuthority,
    OperatorRegistry,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    for name in ("--manifest-sha256", "--staged-digest", "--target", "--approver", "--approval-key-ref"):
        parser.add_argument(name, required=True)
    parser.add_argument("--operators", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--lifetime-hours", type=float, default=1.0)
    args = parser.parse_args(argv)
    authority = HmacApprovalAuthority(key_ref=args.approval_key_ref,
                                      boundary=SecretBoundary(EnvironmentSecretResolver()),
                                      registry=OperatorRegistry.from_file(args.operators),
                                      ledger=ConsumedApprovalLedger(args.ledger))
    try:
        record = authority.issue(manifest_sha256=args.manifest_sha256, staged_digest=args.staged_digest,
                                 target=args.target, approver=args.approver, issued_at=datetime.now(UTC),
                                 lifetime=timedelta(hours=args.lifetime_hours))
    except ApprovalError as error:
        print(f"APPROVAL_REFUSED {error.code}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"approval_id={record['approval_id']} expires_at={record['expires_at']} target={record['target']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
