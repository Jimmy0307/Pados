# SPDX-License-Identifier: AGPL-3.0-only
"""The Windows and VMware validation runners live in the repository and record exactly the registry's checks (L5).

The runtime lane gate reads the records these runners write. Before they were in the repository nothing tied their
gate names to ``scripts/release/runtime_lanes.v1.json``: a renamed or dropped gate would only surface as a missing
check at gate time. Here the runners' own step lists are compared with the registry's required checks.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.validation import windows_lane_records, windows_validate

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = json.loads((ROOT / "scripts" / "release" / "runtime_lanes.v1.json").read_text(encoding="utf-8"))
VM_CHAIN = (ROOT / "scripts" / "validation" / "vm_online_chain.sh").read_text(encoding="utf-8")


def _required(lane: str) -> list[str]:
    return list(REGISTRY["lanes"][lane]["source"]["required_checks"])


def test_the_windows_runner_records_every_windows_ci_gate(tmp_path: Path) -> None:
    run = windows_validate.Run(worktree=tmp_path, out=tmp_path, port=1, pg_bin=tmp_path, profile="core")
    names = [name for name, _ in windows_validate.ci_steps(run)]
    assert len(names) == len(set(names))
    assert sorted(names) == sorted([*windows_validate.CI_STEPS, "wheel_build", *windows_validate.WEB_STEPS])
    assert sorted([*windows_validate.CI_STEPS, "frontend"]) == sorted(_required("windows-ci-gates"))
    assert _required("windows-full-suite") == ["full_pytest"]


def test_the_lane_record_composes_frontend_from_the_web_steps_without_softening() -> None:
    passing = {name: {"exit_code": 0, "log": f"{name}.log", "log_sha256": "0" * 64}
               for name in (*windows_validate.CI_STEPS, *windows_validate.WEB_STEPS)}
    assert windows_lane_records.lane_gates(passing)["frontend"]["exit_code"] == 0
    failing = dict(passing, web_lint={"exit_code": 1, "log": "web_lint.log", "log_sha256": "0" * 64})
    assert windows_lane_records.lane_gates(failing)["frontend"]["exit_code"] == 1
    missing = {name: step for name, step in passing.items() if name != "web_build"}
    assert windows_lane_records.lane_gates(missing)["frontend"]["exit_code"] is None
    gates = windows_lane_records.lane_gates({name: step for name, step in passing.items() if name != "d2"})
    assert gates["d2"] == {"exit_code": None, "note": "step did not run"}


def test_failure_titles_are_read_however_pytest_pads_them() -> None:
    log = "\n".join([
        "______________________ test_short[param] ______________________",
        "_ test_a_very_long_name_that_pytest_pads_with_a_single_underscore _",
        "_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _",
        "____ TestClass.test_method ____",
    ])
    assert windows_lane_records.failure_names(log) == {
        "test_short", "test_a_very_long_name_that_pytest_pads_with_a_single_underscore", "test_method"}


def test_the_vm_chain_runs_and_summarizes_exactly_the_linux_ci_gates() -> None:
    run_calls = re.findall(r"^run (\w+) ", VM_CHAIN, re.MULTILINE)
    summarized = re.search(r"for name in ([\w ]+); do", VM_CHAIN)
    assert summarized is not None
    required = _required("vmware-linux-ci-gates")
    assert run_calls == required
    assert summarized.group(1).split() == required


@pytest.mark.parametrize("script", ["scripts/validation/vm_online_chain.sh", "scripts/validation/windows_validate.py",
                                    "scripts/validation/windows_lane_records.py"])
def test_the_runners_carry_no_personal_paths_and_never_reuse_evidence(script: str) -> None:
    text = (ROOT / script).read_text(encoding="utf-8")
    assert not re.search(r"/home/|[A-Za-z]:[\\/]Users[\\/]", text)
    if script.endswith(".sh"):
        assert "REFUSE_EXISTING_RUN" in text and "rm -rf" not in text and "docker rm -fv" in text
    if script.endswith("windows_validate.py"):
        assert "exist_ok=False" in text and "TRACKED_CHANGES_PRESENT" in text
