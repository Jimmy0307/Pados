# SPDX-License-Identifier: AGPL-3.0-only
"""Check .github workflows and dependabot config against the CI authority boundary.

Usage: ci_policy_check.py OUT.json
Exit 0 and print CI_POLICY_OK when there are no findings; exit 1 otherwise.
Read-only: the policy (src/pados/release/workflow_policy.py) parses the
workflows with a fail-closed YAML subset loader and never executes them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.workflow_policy import (  # noqa: E402
    HOME_BASE_SELF_HOSTED_RUNNER,
    PUBLIC_PR_RUNNER_POLICY,
    check_repository,
)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: ci_policy_check.py OUT.json", file=sys.stderr)
        return 2
    findings = check_repository(ROOT)
    report = {"public_pr_runner_policy": PUBLIC_PR_RUNNER_POLICY,
              "home_base_self_hosted_runner": HOME_BASE_SELF_HOSTED_RUNNER,
              "workflows": sorted(p.name for p in (ROOT / ".github" / "workflows").glob("*.y*ml")),
              "findings": [f.as_record() for f in findings],
              "status": "PASS" if not findings else "FAIL"}
    Path(args[0]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for finding in findings:
        print(f"FINDING {finding.workflow} {finding.code} {finding.location}")
    print("CI_POLICY_OK" if not findings else f"CI_POLICY_FINDINGS={len(findings)}")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
