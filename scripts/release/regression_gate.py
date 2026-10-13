# SPDX-License-Identifier: AGPL-3.0-only
"""Run one named regression gate (a fixed list of pytest paths) and fail closed.

Usage: regression_gate.py GATE OUT.json [--no-skips] [--min-tests N] -- PATH [PATH ...]

A gate fails when pytest fails, when any listed path collects no test, when fewer than ``--min-tests`` tests
pass, or (with ``--no-skips``) when any test skips that is not named by ``--allow-skip``. Skips are refused
because an environment-gated regression that silently skips is indistinguishable from one that passes; the
few legitimate ones are listed explicitly in the workflow and recorded in the report. The JSON report records the gate,
the paths, the outcome counts and the source commit/tree; it never contains environment values.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Same import root as ``python -m pytest`` from the repository root (tests import ``tests.*`` helpers).
sys.path.insert(0, str(ROOT))


class _Outcomes:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.per_path: Counter[str] = Counter()
        self.skipped: list[str] = []

    def pytest_runtest_logreport(self, report: Any) -> None:
        path = str(report.nodeid).split("::", 1)[0]
        if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
            self.counts[report.outcome] += 1
            self.per_path[path] += 1
            if report.outcome == "skipped":
                self.skipped.append(str(report.nodeid))
        elif report.when == "teardown" and report.outcome == "failed":
            self.counts["error"] += 1


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("gate")
    parser.add_argument("out")
    parser.add_argument("--no-skips", action="store_true")
    parser.add_argument("--min-tests", type=int, default=1)
    parser.add_argument("--allow-skip", action="append", default=[],
                        help="node id that may skip (each one is listed in the report as an allowed skip)")
    parser.add_argument("--basetemp", help="pytest --basetemp (isolates temporary directories per gate)")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    missing = [path for path in args.paths if not (ROOT / path).exists()]
    outcomes = _Outcomes()
    options = ["-q", "-p", "no:cacheprovider", "-o", "addopts=", "-rs"]
    if args.basetemp:
        options.append(f"--basetemp={args.basetemp}")
    try:
        exit_code = 4 if missing else int(pytest.main([*options, *args.paths], plugins=[outcomes]))
    except Exception as error:  # a crashed session is a failed gate, never a missing report
        print(f"GATE {args.gate} pytest session crashed: {type(error).__name__}")
        exit_code = 3
    passed = outcomes.counts["passed"]
    empty = sorted(path for path in args.paths if (ROOT / path).is_file()
                   and outcomes.per_path[Path(path).as_posix()] == 0)
    problems: list[str] = []
    if missing:
        problems.append("PATH_MISSING")
    if exit_code != 0:
        problems.append(f"PYTEST_EXIT_{exit_code}")
    if empty:
        problems.append("PATH_COLLECTED_NOTHING")
    if passed < args.min_tests:
        problems.append("TOO_FEW_TESTS")
    unexpected_skips = [node for node in outcomes.skipped if node not in set(args.allow_skip)]
    if args.no_skips and unexpected_skips:
        problems.append("SKIPPED_TESTS_NOT_ALLOWED")
    report = {
        "gate": args.gate,
        "status": "PASS" if not problems else "FAIL",
        "problems": problems,
        "paths": args.paths,
        "missing_paths": missing,
        "empty_paths": empty,
        "counts": dict(sorted(outcomes.counts.items())),
        "skipped": outcomes.skipped,
        "allowed_skips": sorted(set(args.allow_skip) & set(outcomes.skipped)),
        "source_commit": _git("rev-parse", "HEAD"),
        "source_tree": _git("rev-parse", "HEAD^{tree}"),
    }
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"GATE {args.gate} {report['status']} passed={passed} skipped={len(outcomes.skipped)} "
          f"problems={','.join(problems) or 'none'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
