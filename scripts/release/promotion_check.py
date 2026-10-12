# SPDX-License-Identifier: AGPL-3.0-only
"""Release promotion pre-check: RC CI evidence + live acceptance of the same commit. Never publishes.

Usage: promotion_check.py --rc-dir DIR --live-summary live-acceptance-summary.json

Eligible only when the RC artifact in DIR re-verifies (rc_manifest.py: hashes, provenance, commit/tree) AND the
live acceptance summary is PASS for exactly that RC commit and tree, with every required live gate (d1, d2, d4)
present and passing, run against an installed runtime that is not dirty. PR CI success is not an input: a
green pull-request run never makes a commit eligible. The output is advice for a human release decision; this
script has no publish, deploy or authorization capability.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from package_provenance import ProvenanceError  # noqa: E402
from rc_manifest import check  # noqa: E402

REQUIRED_LIVE_GATES = ("d1", "d2", "d4")


def problems(rc_dir: Path, live_summary: Path) -> list[str]:
    try:
        manifest = check(rc_dir, None, None)
    except (ProvenanceError, OSError, ValueError, KeyError) as error:
        return [f"RC_ARTIFACT_INVALID:{getattr(error, 'code', type(error).__name__)}"]
    summary = json.loads(live_summary.read_text(encoding="utf-8"))
    found: list[str] = []
    if summary.get("status") != "PASS":
        found.append("LIVE_ACCEPTANCE_NOT_PASS")
    gates = summary.get("gates") or {}
    found.extend(f"LIVE_GATE_MISSING_OR_FAILED:{gate}" for gate in REQUIRED_LIVE_GATES if gates.get(gate) != "PASS")
    rc = summary.get("rc") or {}
    if rc.get("RC_COMMIT") != manifest["RC_COMMIT"] or rc.get("RC_TREE") != manifest["RC_TREE"]:
        found.append("LIVE_ACCEPTANCE_FOR_ANOTHER_RC")
    source = (summary.get("runtime") or {}).get("source") or {}
    if source.get("commit") != manifest["RC_COMMIT"] or source.get("dirty") is not False:
        found.append("LIVE_RUNTIME_NOT_RC_COMMIT")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rc-dir", required=True, type=Path)
    parser.add_argument("--live-summary", required=True, type=Path)
    args = parser.parse_args(argv)
    found = problems(args.rc_dir, args.live_summary)
    for item in found:
        print(f"PROMOTION_BLOCKED {item}")
    print("PROMOTION_ELIGIBLE (human release decision required; nothing was published)" if not found
          else f"PROMOTION_NOT_ELIGIBLE findings={len(found)}")
    return 0 if not found else 1


if __name__ == "__main__":
    sys.exit(main())
