# SPDX-License-Identifier: AGPL-3.0-only
"""CI operability gate (OP-006). Usage: operability_gate.py OUT.json [--contracts FILE] [--lanes FILE]

Exit 0 (OPERABILITY_GATE_PASS) when every component's references resolve. Explicit gaps are listed and keep a
component INCOMPLETE, never COMPLETE. Exit 1 (OPERABILITY_GATE_FAIL) when any reference does not resolve. Exit 2 when
the registry is malformed. Read-only: it runs nothing and grants nothing (semantic_authority NONE).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.operability_gate import evaluate, lane_known_status, load  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--contracts", type=Path, default=ROOT / "scripts" / "release" / "operability_contracts.v1.json")
    parser.add_argument("--lanes", type=Path, default=ROOT / "scripts" / "release" / "runtime_lanes.v1.json")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        registry = load(args.contracts)
        lanes = lane_known_status(args.lanes)
    except (OSError, ValueError) as exc:
        print("OPERABILITY_CONTRACTS_INVALID")
        print(str(exc)[:2000], file=sys.stderr)
        return 2
    report = evaluate(args.root, registry, lanes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for component in report.components:
        lanes_text = " ".join(f"{lane}={status}" for lane, status in component.runtime_lanes.items())
        print(f"{component.component_id} {component.status:10} {component.name}"
              + (f" | gaps={len(component.gaps)}" if component.gaps else "")
              + (f" | lanes: {lanes_text}" if lanes_text else ""))
        for finding in component.findings:
            print(f"  FINDING {finding}")
    print(f"OPERABILITY_GATE_{report.status} complete={report.complete} incomplete={report.incomplete} "
          f"failed={report.failed}")
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
