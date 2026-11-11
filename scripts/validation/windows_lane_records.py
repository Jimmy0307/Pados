# SPDX-License-Identifier: AGPL-3.0-only
"""Write the lane records for one windows_validate.py run, and compare its full-suite failures with a baseline.

Usage: python scripts/validation/windows_lane_records.py <run dir> [--baseline <full_pytest.log of a baseline run>]

Outputs in <run dir>:
- gate-summary-lane.json   gate-summary format (repo_head, platform, gates.<name>.exit_code, finished) holding the
                           windows-ci-gates checks; ``frontend`` is the conjunction of the five web steps.
- full-suite-summary.json  gate-summary format with only ``full_pytest`` (the windows-full-suite lane's own record).
- full_pytest_baseline_compare.json  (with --baseline) failing test names against the baseline log's failure sections.
Exit codes are copied from gate-summary.json; nothing is re-judged or softened here.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # run as a script: make the repository importable
from scripts.validation.windows_validate import CI_STEPS, WEB_STEPS  # noqa: E402

# pytest pads a failure title with as few as one or two underscores when the name is long; requiring a letter keeps
# the "_ _ _" traceback separators from matching.
FAILURE_SECTION = re.compile(r"^_+ ([A-Za-z]\S*) _+$")


def _gate(steps: dict[str, Any], name: str) -> dict[str, Any]:
    step = steps.get(name)
    if step is None:
        return {"exit_code": None, "note": "step did not run"}
    return {"exit_code": step["exit_code"], "log": step["log"], "log_sha256": step["log_sha256"]}


def lane_gates(steps: dict[str, Any]) -> dict[str, Any]:
    """The windows-ci-gates record: every CI step, plus ``frontend`` as the first failing web step's code."""
    gates = {name: _gate(steps, name) for name in CI_STEPS}
    web_codes = [steps.get(name, {}).get("exit_code") for name in WEB_STEPS]
    frontend = None if None in web_codes else next((code for code in web_codes if code != 0), 0)
    gates["frontend"] = {"exit_code": frontend, "composed_of": dict(zip(WEB_STEPS, web_codes, strict=True))}
    return gates


def failure_names(log_text: str) -> set[str]:
    return {match.group(1).split("[")[0].split(".")[-1]
            for match in map(FAILURE_SECTION.match, log_text.splitlines()) if match}


def compare_with_baseline(run: Path, baseline_log: Path) -> dict[str, Any]:
    baseline = failure_names(baseline_log.read_text(encoding="utf-8", errors="replace"))
    root = ET.parse(run / "full-pytest.xml").getroot()
    suite = root if root.tag == "testsuite" else root[0]
    counts = {key: suite.get(key) for key in ("tests", "failures", "errors", "skipped", "time")}
    failed_ids = [f"{case.get('classname')}::{case.get('name')}" for case in suite.iter("testcase")
                  if case.find("failure") is not None or case.find("error") is not None]
    candidate = {test_id.split("::")[-1].split("[")[0] for test_id in failed_ids}
    return {"baseline_log": str(baseline_log), "baseline_count": len(baseline), "junit_counts": counts,
            "failed_cases": len(failed_ids), "candidate_count": len(candidate),
            "only_in_candidate": sorted(candidate - baseline), "only_in_baseline": sorted(baseline - candidate),
            "identical": candidate == baseline, "failed_ids": failed_ids,
            "note": "names are the test function names (pytest failure-section titles), parameters dropped"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    summary = json.loads((args.run / "gate-summary.json").read_text(encoding="utf-8"))
    steps = summary["steps"]
    base = {"repo_head": summary["repo_head"], "platform": summary["platform"], "finished": summary.get("finished"),
            "runner": "scripts/validation/windows_validate.py", "derived_from": "gate-summary.json"}
    gates = lane_gates(steps)
    (args.run / "gate-summary-lane.json").write_text(json.dumps(dict(base, gates=gates), indent=2), encoding="utf-8")
    (args.run / "full-suite-summary.json").write_text(
        json.dumps(dict(base, gates={"full_pytest": _gate(steps, "full_pytest")}), indent=2), encoding="utf-8")
    if args.baseline is not None:
        compare = compare_with_baseline(args.run, args.baseline)
        (args.run / "full_pytest_baseline_compare.json").write_text(json.dumps(compare, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in compare.items() if key != "failed_ids"}, indent=2))
    print(json.dumps({name: gate["exit_code"] for name, gate in gates.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
