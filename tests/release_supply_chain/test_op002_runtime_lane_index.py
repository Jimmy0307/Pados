# SPDX-License-Identifier: AGPL-3.0-only
"""OP-002: the evidence index takes a PASS only from the registry-bound runner record for the same commit."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.release.runtime_lane_index import IndexBuildError, build_index, load_spec, verify_index
from pados.release.runtime_lanes import LaneStatus, evaluate, load_registry

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = load_registry(ROOT / "scripts" / "release" / "runtime_lanes.v1.json")
SHA = "a" * 40
OTHER = "b" * 40
SCRIPT = ROOT / "scripts" / "release" / "runtime_lane_index.py"
WINDOWS_GATES = REGISTRY.lanes["windows-ci-gates"].source.required_checks  # type: ignore[union-attr]
VM_GATES = REGISTRY.lanes["vmware-linux-ci-gates"].source.required_checks  # type: ignore[union-attr]


def _spec(*lanes: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "pados.runtime-lane-spec.v1", "commit": SHA, "lanes": list(lanes)}


def _gates_lane(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"lane_id": "windows-ci-gates", "platform": "windows", "command": "run_gates.ps1",
                            "evidence_path": "gate-summary.json"}
    base.update(extra)
    return base


def _gate_summary(root: Path, *, commit: str = SHA, exits: dict[str, int | None] | None = None,
                  finished: datetime | None = None, name: str = "gate-summary.json") -> Path:
    codes = {gate: 0 for gate in WINDOWS_GATES} | (exits or {})
    gates = {gate: {"command": gate, "exit_code": code} for gate, code in codes.items() if code is not None}
    record = {"repo_head": commit, "gates": gates, "finished": (finished or datetime.now(UTC)).isoformat()}
    path = root / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _e2e(root: Path, *, platform: str = "Windows-11", verdict: str = "PASS", name: str = "E2E_RESULT.json") -> Path:
    path = root / name
    path.write_text(json.dumps({"verdict": verdict, "commit": SHA, "platform": platform,
                                "finished": datetime.now(UTC).isoformat(),
                                "steps": [{"step": "one", "status": verdict}]}), encoding="utf-8")
    return path


def _verdict(index: Any, root: Path, lane: str) -> tuple[LaneStatus, str]:
    report = evaluate(REGISTRY, index, target="production", commit=SHA, now=datetime.now(UTC), evidence_root=root)
    found = next(v for v in report.lanes if v.lane_id == lane)
    return found.status, found.reason


def test_a_pass_is_read_from_the_runner_record_and_the_gate_accepts_it(tmp_path: Path) -> None:
    _gate_summary(tmp_path)
    index = build_index(load_spec(_spec(_gates_lane())), root=tmp_path, registry=REGISTRY)
    record = index.records[0]
    assert (record.status, record.exit_code) == (LaneStatus.PASS, 0) and len(record.evidence_sha256 or "") == 64
    assert _verdict(index, tmp_path, "windows-ci-gates") == (LaneStatus.PASS, "VERIFIED")


def test_any_failing_required_gate_makes_the_lane_fail(tmp_path: Path) -> None:
    _gate_summary(tmp_path, exits={"mypy": 2})
    index = build_index(load_spec(_spec(_gates_lane())), root=tmp_path, registry=REGISTRY)
    assert (index.records[0].status, index.records[0].exit_code) == (LaneStatus.FAIL, 2)


def test_a_typed_zero_exit_code_is_refused_and_pass_cannot_be_declared(tmp_path: Path) -> None:
    (tmp_path / "log.txt").write_text("looks fine\n", encoding="utf-8")
    typed = {"lane_id": "windows-ci-gates", "platform": "windows", "command": "x", "exit_code": 0,
             "evidence_path": "log.txt"}
    with pytest.raises(IndexBuildError, match="RUNTIME_LANE_PASS_NEEDS_MACHINE_SOURCE"):
        build_index(load_spec(_spec(typed)), root=tmp_path, registry=REGISTRY)
    with pytest.raises(ValueError):
        load_spec(_spec({**typed, "declare": "PASS"}))
    assert build_index(load_spec(_spec({**typed, "exit_code": 3})), root=tmp_path,
                       registry=REGISTRY).records[0].status is LaneStatus.FAIL  # a FAIL is never an upgrade


@pytest.mark.parametrize("field", [{"source": "gate-summary"}, {"gates": ["ruff"]}])
def test_the_spec_cannot_name_the_source_or_the_checks(field: dict[str, Any]) -> None:
    with pytest.raises(IndexBuildError, match="RUNTIME_LANE_SPEC_INVALID"):
        load_spec(_spec(_gates_lane(**field)))


@pytest.mark.parametrize(("record", "code"), [
    ({"commit": OTHER}, "RUNTIME_LANE_SOURCE_COMMIT_MISMATCH"),
    ({"exits": {"secret_scan": None}}, "RUNTIME_LANE_SOURCE_GATE_MISSING"),
])
def test_a_record_that_does_not_match_the_registry_refuses_the_build(tmp_path: Path, record: dict[str, Any],
                                                                    code: str) -> None:
    _gate_summary(tmp_path, **record)
    with pytest.raises(IndexBuildError, match=code):
        build_index(load_spec(_spec(_gates_lane())), root=tmp_path, registry=REGISTRY)


def test_vmware_and_e2e_records_are_cross_checked_for_platform(tmp_path: Path) -> None:
    vm = {"sha": SHA, "virt": "vmware", "e2e": 0, "gates": dict.fromkeys(VM_GATES, 0),
          "finished": datetime.now(UTC).isoformat()}
    (tmp_path / "vm.json").write_text(json.dumps(vm), encoding="utf-8")
    vm_lane = {"lane_id": "vmware-linux-ci-gates", "platform": "linux", "command": "vm_online_chain.sh",
               "evidence_path": "vm.json"}
    assert build_index(load_spec(_spec(vm_lane)), root=tmp_path, registry=REGISTRY).records[0].status is LaneStatus.PASS
    (tmp_path / "vm.json").write_text(json.dumps({**vm, "virt": "wsl"}), encoding="utf-8")
    with pytest.raises(IndexBuildError, match="RUNTIME_LANE_SOURCE_PLATFORM_MISMATCH"):
        build_index(load_spec(_spec(vm_lane)), root=tmp_path, registry=REGISTRY)
    e2e_lane = {"lane_id": "windows-native-e2e", "platform": "windows", "command": "local_dogfood_e2e.py",
                "evidence_path": "E2E_RESULT.json"}
    _e2e(tmp_path, platform="Linux-6.8.0-x86_64")
    with pytest.raises(IndexBuildError, match="RUNTIME_LANE_SOURCE_PLATFORM_MISMATCH"):
        build_index(load_spec(_spec(e2e_lane)), root=tmp_path, registry=REGISTRY)
    _e2e(tmp_path, platform="Linux-5.15.153.1-microsoft-standard-WSL2-x86_64")
    linux_lane = {**e2e_lane, "lane_id": "vmware-linux-e2e", "platform": "linux"}
    with pytest.raises(IndexBuildError, match="RUNTIME_LANE_SOURCE_PLATFORM_MISMATCH"):
        build_index(load_spec(_spec(linux_lane)), root=tmp_path, registry=REGISTRY)
    _e2e(tmp_path, verdict="FAIL")
    assert build_index(load_spec(_spec(e2e_lane)), root=tmp_path, registry=REGISTRY).records[0].status is LaneStatus.FAIL


def test_no_exit_code_and_no_evidence_means_not_validated(tmp_path: Path) -> None:
    lane = {"lane_id": "github-ci", "platform": "github", "command": "ci.yml", "notes": "no run for this commit"}
    assert build_index(load_spec(_spec(lane)), root=tmp_path,
                       registry=REGISTRY).records[0].status is LaneStatus.NOT_VALIDATED


def test_a_lane_without_a_registry_source_cannot_pass(tmp_path: Path) -> None:
    (tmp_path / "rea.json").write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
    lane = {"lane_id": "rea-runtime", "platform": "any", "command": "rea", "evidence_path": "rea.json"}
    assert build_index(load_spec(_spec(lane)), root=tmp_path,
                       registry=REGISTRY).records[0].status is LaneStatus.NOT_VALIDATED


def test_a_declared_downgrade_keeps_the_evidence_but_needs_a_reason(tmp_path: Path) -> None:
    (tmp_path / "full.log").write_text("126 failed\n", encoding="utf-8")
    spec = {"lane_id": "windows-full-suite", "platform": "windows", "command": "pytest", "exit_code": 1,
            "evidence_path": "full.log", "declare": "NOT_VALIDATED",
            "notes": "126 failures, identical to the recorded Windows baseline"}
    record = build_index(load_spec(_spec(spec)), root=tmp_path, registry=REGISTRY).records[0]
    assert record.status is LaneStatus.NOT_VALIDATED and record.evidence_sha256
    with pytest.raises(IndexBuildError, match="DECLARATION_NEEDS_A_REASON"):
        build_index(load_spec(_spec({**spec, "notes": "baseline"})), root=tmp_path, registry=REGISTRY)


@pytest.mark.parametrize(("lane", "code"), [
    (_gates_lane(evidence_path="absent.json"), "RUNTIME_LANE_EVIDENCE_MISSING"),
    (_gates_lane(evidence_path="../outside.json"), "RUNTIME_LANE_EVIDENCE_OUTSIDE_ROOT"),
    (_gates_lane(evidence_path=None, exit_code=1), "RUNTIME_LANE_EVIDENCE_REQUIRED"),
    (_gates_lane(lane_id="no-such-lane"), "RUNTIME_LANE_UNKNOWN"),
])
def test_unusable_evidence_refuses_the_whole_build(tmp_path: Path, lane: dict[str, Any], code: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _gate_summary(root)
    _gate_summary(tmp_path, name="outside.json")
    with pytest.raises(IndexBuildError, match=code):
        build_index(load_spec(_spec(lane)), root=root, registry=REGISTRY)


def test_one_file_cannot_back_two_lanes(tmp_path: Path) -> None:
    _gate_summary(tmp_path, exits={"full_pytest": 0})
    spec = _spec(_gates_lane(), _gates_lane(lane_id="windows-full-suite"))
    with pytest.raises(IndexBuildError, match="RUNTIME_LANE_EVIDENCE_REUSED"):
        build_index(load_spec(spec), root=tmp_path, registry=REGISTRY)


def test_recorded_at_is_the_runners_finish_time_so_an_old_record_is_stale(tmp_path: Path) -> None:
    _gate_summary(tmp_path, finished=datetime.now(UTC) - timedelta(days=60))  # fresh mtime, old finish time
    index = build_index(load_spec(_spec(_gates_lane())), root=tmp_path, registry=REGISTRY)
    assert _verdict(index, tmp_path, "windows-ci-gates") == (LaneStatus.NOT_VALIDATED, "EVIDENCE_STALE")


def test_verify_detects_an_edited_or_removed_evidence_file(tmp_path: Path) -> None:
    path = _gate_summary(tmp_path)
    _e2e(tmp_path)
    e2e_lane = {"lane_id": "windows-native-e2e", "platform": "windows", "command": "e2e",
                "evidence_path": "E2E_RESULT.json"}
    index = build_index(load_spec(_spec(_gates_lane(), e2e_lane)), root=tmp_path, registry=REGISTRY)
    assert verify_index(index, root=tmp_path) == ()
    path.write_text("edited after indexing\n", encoding="utf-8")
    (tmp_path / "E2E_RESULT.json").unlink()
    assert verify_index(index, root=tmp_path) == (
        "windows-ci-gates:EVIDENCE_HASH_MISMATCH", "windows-native-e2e:EVIDENCE_FILE_MISSING")


def test_the_cli_builds_verifies_and_fails_closed(tmp_path: Path) -> None:
    _gate_summary(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_spec(_gates_lane())), encoding="utf-8")
    out = tmp_path / "index.json"
    run = [sys.executable, str(SCRIPT)]
    built = subprocess.run([*run, "build", str(spec), "--out", str(out)], capture_output=True, text=True, check=False)
    assert built.returncode == 0 and "RUNTIME_LANE_INDEX_BUILT records=1" in built.stdout
    verified = subprocess.run([*run, "verify", str(out)], capture_output=True, text=True, check=False)
    assert verified.returncode == 0 and "RUNTIME_LANE_INDEX_VERIFIED" in verified.stdout
    (tmp_path / "gate-summary.json").write_text("tampered\n", encoding="utf-8")
    tampered = subprocess.run([*run, "verify", str(out)], capture_output=True, text=True, check=False)
    assert tampered.returncode == 1 and "EVIDENCE_HASH_MISMATCH" in tampered.stdout
    _gate_summary(tmp_path, commit=OTHER)
    refused = subprocess.run([*run, "build", str(spec), "--out", str(out)], capture_output=True, text=True, check=False)
    assert refused.returncode == 2 and "RUNTIME_LANE_SOURCE_COMMIT_MISMATCH" in refused.stdout
