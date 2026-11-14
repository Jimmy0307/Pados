# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime lane evidence index tool (OP-002).

Usage:
  runtime_lane_index.py build SPEC.json --out INDEX.json [--root DIR] [--registry FILE]
  runtime_lane_index.py verify INDEX.json [--root DIR]

build derives each lane's status from its exit code, hashes its evidence file and stamps recorded_at from the file's
modification time; a spec can only downgrade a lane (SKIPPED / NOT_VALIDATED with a reason). verify re-hashes every
referenced file. The root defaults to the spec's or index's directory; evidence must sit inside it. Exit 0 on success,
1 when verify finds a mismatch, 2 when the input is malformed or the build is refused (fail closed). The index binds
evidence to a commit; it does not prove the evidence content is true (semantic_authority NONE). Feed it to
runtime_lane_gate.py --evidence for a per-target verdict.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.runtime_lane_index import build_index, load_spec, summary, verify_index  # noqa: E402
from pados.release.runtime_lanes import load_evidence, load_registry  # noqa: E402


def _code(exc: Exception) -> str:
    code = str(exc).split(":", 1)[0].split(" ", 1)[0]
    return code if code.startswith("RUNTIME_LANE_") else "RUNTIME_LANE_INPUT_INVALID"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build")
    build.add_argument("spec", type=Path)
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--root", type=Path, default=None)
    build.add_argument("--registry", type=Path, default=ROOT / "scripts" / "release" / "runtime_lanes.v1.json")
    verify = sub.add_parser("verify")
    verify.add_argument("index", type=Path)
    verify.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.action == "build":
            spec = load_spec(json.loads(args.spec.read_text(encoding="utf-8")))
            root = (args.root or args.spec.parent).resolve()
            index = build_index(spec, root=root, registry=load_registry(args.registry))
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(index.model_dump_json(indent=2) + "\n", encoding="utf-8")
            print("\n".join(summary(index)))
            print(f"RUNTIME_LANE_INDEX_BUILT records={len(index.records)} commit={spec.commit}")
            return 0
        index = load_evidence(json.loads(args.index.read_text(encoding="utf-8")))
        findings = verify_index(index, root=(args.root or args.index.parent).resolve())
    except (OSError, ValueError) as exc:
        print(_code(exc))
        print(str(exc)[:2000], file=sys.stderr)
        return 2
    for finding in findings:
        print(f"FINDING {finding}")
    print("RUNTIME_LANE_INDEX_" + ("MISMATCH" if findings else "VERIFIED") + f" records={len(index.records)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
