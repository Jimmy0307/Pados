# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime lane gate (OP-002): per-lane status for a release target, bound to evidence. Never publishes.

Usage: runtime_lane_gate.py --target NAME --out REPORT.json [--evidence INDEX.json] [--evidence-root DIR]
                            [--commit SHA] [--registry FILE] [--claim]

Report mode (default) writes the per-lane status and exits 0 whatever the lanes say: it records and never claims.
``--claim`` exits 1 (RUNTIME_READINESS_NOT_PROVEN) unless every lane the target requires has a verified PASS record
for exactly this commit. Malformed registry or evidence exits 2 (fail closed). Evidence paths are resolved inside the
evidence root, which defaults to the evidence index's directory. The report grants no approval, acceptance or release
decision (semantic_authority NONE); promotion_check.py and the existing approval and acceptance paths stay
authoritative.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.runtime_lanes import EVIDENCE_SCHEMA, evaluate, load_evidence, load_registry  # noqa: E402


def _head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,  # noqa: S603,S607
                          check=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, default=None)
    parser.add_argument("--commit", default=None)
    parser.add_argument("--registry", type=Path, default=ROOT / "scripts" / "release" / "runtime_lanes.v1.json")
    parser.add_argument("--claim", action="store_true")
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.registry)
        raw = (json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence is not None
               else {"schema_version": EVIDENCE_SCHEMA, "records": []})
        evidence = load_evidence(raw)
        root = (args.evidence_root or (args.evidence.parent if args.evidence else ROOT)).resolve()
        report = evaluate(registry, evidence, target=args.target, commit=args.commit or _head(),
                          now=datetime.now(UTC), evidence_root=root)
    except (OSError, ValueError) as exc:
        code = str(exc).split(":", 1)[0].split(" ", 1)[0]
        print(code if code.startswith("RUNTIME_LANE_") or code == "UNKNOWN_TARGET"
              else "RUNTIME_LANE_EVIDENCE_INVALID")
        print(str(exc)[:2000], file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for verdict in report.lanes:
        marker = "REQUIRED" if verdict.required else "optional"
        print(f"{verdict.lane_id:34} {marker:8} {verdict.status.value:13} {verdict.reason}")
    print(f"RUNTIME_READINESS={'READY' if report.ready else 'NOT_READY'} target={report.target} "
          f"commit={report.commit}")
    if args.claim and not report.ready:
        print("RUNTIME_READINESS_NOT_PROVEN " + " ".join(report.blockers))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
