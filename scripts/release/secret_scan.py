# SPDX-License-Identifier: AGPL-3.0-only
"""Repository secret scan over tracked files (high-confidence token shapes).

Usage: secret_scan.py OUT.json
Exit 0 and print SECRET_SCAN_OK when every finding is classified by
scripts/release/secret_scan_allowlist.json; exit 1 on an unclassified finding.
Reports record path, line and rule only; matched values are never printed or stored.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.secret_scan import load_allowlist, scan_files, scan_report  # noqa: E402

ALLOWLIST = ROOT / "scripts" / "release" / "secret_scan_allowlist.json"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: secret_scan.py OUT.json", file=sys.stderr)
        return 2
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    paths = [p for p in tracked.decode("utf-8").split("\0") if p]
    allowlist = load_allowlist(json.loads(ALLOWLIST.read_text(encoding="utf-8")))
    findings = scan_files(ROOT, paths, allowlist)
    report = scan_report(findings, scanned_files=len(paths))
    Path(args[0]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for finding in report["unclassified"]:
        print(f"UNCLASSIFIED {finding['path']}:{finding['line']} {finding['rule']}")
    print(f"scanned_files={len(paths)} findings={len(findings)} unclassified={len(report['unclassified'])}")
    print("SECRET_SCAN_OK" if report["status"] == "PASS" else "SECRET_SCAN_UNCLASSIFIED_FINDINGS")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
