# SPDX-License-Identifier: AGPL-3.0-only
"""Home Base pull-based verification of a fetched release candidate. Never deploys.

Usage:
  verify_release.py RELEASE_DIR --staging-root DIR
      (--expect-manifest-sha256 HEX | --expect-commit SHA --expect-tree SHA)
      [--expect-version V] [--attestation-bundle-sha256 HEX] [--repo LOCAL_GIT_CLONE]
      [--approval APPROVAL.json --target HOME_BASE_ID --operators OPERATORS.json
       --approval-key-ref secretref://env/NAME --ledger CONSUMED.jsonl [--consume]]

RELEASE_DIR is a local directory the operator fetched explicitly; nothing here
uses the network, SSH or GitHub. The directory is copied into a fresh read-only
directory under --staging-root and only the staged bytes are verified. Pins come
from the operator, out of band. STAGED_VERIFIED requires a manifest digest pin;
a commit/tree pin yields STAGED_VERIFIED_COMMIT_PIN_ONLY, which is never
eligible for deployment. With --approval, readiness additionally requires a
signed approval (HMAC key via SecretRef, registered approver, bound to manifest,
staged digest and target, unexpired, not consumed). --consume records the
approval as used. Inside GitHub Actions every approval is refused
(CI_CANNOT_AUTHORIZE_DEPLOYMENT). Exit 0 only for the requested positive outcome.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary  # noqa: E402
from pados.release.approval import ConsumedApprovalLedger, HmacApprovalAuthority, OperatorRegistry  # noqa: E402
from pados.release.deploy_verify import (  # noqa: E402
    OperatorPin,
    ReadinessStatus,
    VerificationStatus,
    consume_approval,
    deployment_readiness,
    verify_release,
)


def _git_tree_resolver(repo: Path) -> Callable[[str], str | None]:
    def resolve(commit: str) -> str | None:
        done = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}"], cwd=repo,
                              capture_output=True, text=True, check=False)
        return done.stdout.strip() if done.returncode == 0 else None
    return resolve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--expect-manifest-sha256")
    parser.add_argument("--expect-commit")
    parser.add_argument("--expect-tree")
    parser.add_argument("--expect-version")
    parser.add_argument("--attestation-bundle-sha256")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--operators", type=Path)
    parser.add_argument("--approval-key-ref")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--consume", action="store_true")
    args = parser.parse_args(argv)
    pin = OperatorPin(manifest_sha256=args.expect_manifest_sha256, git_commit=args.expect_commit,
                      git_tree=args.expect_tree, version=args.expect_version,
                      attestation_bundle_sha256=args.attestation_bundle_sha256)
    result = verify_release(args.release_dir, pin, staging_root=args.staging_root,
                            git_tree_of=_git_tree_resolver(args.repo) if args.repo else None)
    print(json.dumps(result.as_record(), indent=2, sort_keys=True))
    if result.status is VerificationStatus.REJECTED:
        return 1
    if args.approval is None:
        print(f"{result.status} (not authorized: a signed human approval bound to the manifest hash, "
              "staged digest and target is required)")
        return 0
    if not (args.target and args.operators and args.approval_key_ref and args.ledger):
        print("--approval requires --target, --operators, --approval-key-ref and --ledger", file=sys.stderr)
        return 2
    authority = HmacApprovalAuthority(key_ref=args.approval_key_ref,
                                      boundary=SecretBoundary(EnvironmentSecretResolver()),
                                      registry=OperatorRegistry.from_file(args.operators),
                                      ledger=ConsumedApprovalLedger(args.ledger))
    record = json.loads(args.approval.read_text(encoding="utf-8"))
    evaluate = consume_approval if args.consume else deployment_readiness
    readiness = evaluate(result, record, authority=authority, target=args.target)
    print(f"readiness={readiness.status} code={readiness.code} approval_id={readiness.approval_id} "
          f"deployment_performed={readiness.deployment_performed}")
    return 0 if readiness.status is ReadinessStatus.DEPLOYMENT_AUTHORIZED_BY_HUMAN else 1


if __name__ == "__main__":
    sys.exit(main())
