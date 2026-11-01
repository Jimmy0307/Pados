# SPDX-License-Identifier: AGPL-3.0-only
"""OP-002: the evidence index builder derives status and hashes itself and never upgrades a lane to PASS."""
from __future__ import annotations

import json
import os
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
SCRIPT = ROOT / "scripts" / "release" / "runtime_lane_index.py"


def _spec(*lanes: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "pados.runtime-lane-spec.v1", "commit": SHA, "lanes": list(lanes)}


def _lane(lane_id: str = "windows-ci-gates", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"lane_id": lane_id, "platform": "windows", "command": "run_gates.ps1",
                            "exit_code": 0, "evidence_path": f"{lane_id}.log"}
    base.update(extra)
    return base


def _evidence(root: Path, name: str, text: str = "gate output\n") -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def test_status_and_hash_are_derived_and_the_gate_accepts_the_result(tmp_path: Path) -> None:
    _evidence(tmp_path, "windows-ci-gates.log")
    index = build_index(load_spec(_spec(_lane())), root=tmp_path, registry=REGISTRY)
    record = index.records[0]
    assert (record.status, record.exit_code) == (LaneStatus.PASS, 0) and len(record.evidence_sha256 or "") == 64
    report = evaluate(REGISTRY, index, target="production", commit=SHA, now=datetime.now(UTC), evidence_root=tmp_path)
    verdict = next(v for v in report.lanes if v.lane_id == "windows-ci-gates")
    assert (verdict.status, verdict.reason) == (LaneStatus.PASS, "VERIFIED")
    assert report.ready is False  # one lane never makes a target ready


def test_a_nonzero_exit_is_a_fail_and_cannot_be_declared_away_into_a_pass(tmp_path: Path) -> None:
    _evidence(tmp_path, "windows-ci-gates.log")
    index = build_index(load_spec(_spec(_lane(exit_code=3))), root=tmp_path, registry=REGISTRY)
    assert index.records[0].status is LaneStatus.FAIL
    with pytest.raises(ValueError):
        load_spec(_spec(_lane(declare="PASS")))


def test_no_exit_code_means_not_validated(tmp_path: Path) -> None:
    index = build_index(load_spec(_spec(_lane(exit_code=None, evidence_path=None))), root=tmp_path, registry=REGISTRY)
    assert index.records[0].status is LaneStatus.NOT_VALIDATED


def test_a_declared_downgrade_keeps_the_evidence_but_needs_a_reason(tmp_path: Path) -> None:
    _evidence(tmp_path, "windows-full-suite.log")
    spec = _lane("windows-full-suite", exit_code=1, declare="NOT_VALIDATED",
                 notes="126 failures, identical to the recorded Windows baseline")
    record = build_index(load_spec(_spec(spec)), root=tmp_path, registry=REGISTRY).records[0]
    assert record.status is LaneStatus.NOT_VALIDATED and record.evidence_sha256
    with pytest.raises(IndexBuildError, match="DECLARATION_NEEDS_A_REASON"):
        build_index(load_spec(_spec({**spec, "notes": "baseline"})), root=tmp_path, registry=REGISTRY)


@pytest.mark.parametrize(("lane", "code"), [
    (_lane(evidence_path="absent.log"), "RUNTIME_LANE_EVIDENCE_MISSING"),
    (_lane(evidence_path="../outside.log"), "RUNTIME_LANE_EVIDENCE_OUTSIDE_ROOT"),
    (_lane(evidence_path=None), "RUNTIME_LANE_EVIDENCE_REQUIRED"),
    (_lane("no-such-lane"), "RUNTIME_LANE_UNKNOWN"),
])
def test_unusable_evidence_refuses_the_whole_build(tmp_path: Path, lane: dict[str, Any], code: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _evidence(tmp_path, "outside.log")
    with pytest.raises(IndexBuildError, match=code):
        build_index(load_spec(_spec(lane)), root=root, registry=REGISTRY)


def test_one_file_cannot_back_two_lanes(tmp_path: Path) -> None:
    _evidence(tmp_path, "shared.log")
    spec = _spec(_lane(evidence_path="shared.log"), _lane("windows-native-e2e", evidence_path="shared.log"))
    with pytest.raises(IndexBuildError, match="RUNTIME_LANE_EVIDENCE_REUSED"):
        build_index(load_spec(spec), root=tmp_path, registry=REGISTRY)


def test_recorded_at_comes_from_the_evidence_so_old_evidence_is_stale(tmp_path: Path) -> None:
    path = _evidence(tmp_path, "windows-ci-gates.log")
    old = (datetime.now(UTC) - timedelta(days=60)).timestamp()
    os.utime(path, (old, old))
    index = build_index(load_spec(_spec(_lane())), root=tmp_path, registry=REGISTRY)
    report = evaluate(REGISTRY, index, target="production", commit=SHA, now=datetime.now(UTC), evidence_root=tmp_path)
    verdict = next(v for v in report.lanes if v.lane_id == "windows-ci-gates")
    assert (verdict.status, verdict.reason) == (LaneStatus.NOT_VALIDATED, "EVIDENCE_STALE")


def test_verify_detects_an_edited_or_removed_evidence_file(tmp_path: Path) -> None:
    path = _evidence(tmp_path, "windows-ci-gates.log")
    _evidence(tmp_path, "windows-native-e2e.log")
    index = build_index(load_spec(_spec(_lane(), _lane("windows-native-e2e"))), root=tmp_path, registry=REGISTRY)
    assert verify_index(index, root=tmp_path) == ()
    path.write_text("edited after indexing\n", encoding="utf-8")
    (tmp_path / "windows-native-e2e.log").unlink()
    assert verify_index(index, root=tmp_path) == (
        "windows-ci-gates:EVIDENCE_HASH_MISMATCH", "windows-native-e2e:EVIDENCE_FILE_MISSING")


def test_the_cli_builds_verifies_and_fails_closed(tmp_path: Path) -> None:
    _evidence(tmp_path, "windows-ci-gates.log")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_spec(_lane())), encoding="utf-8")
    out = tmp_path / "index.json"
    run = [sys.executable, str(SCRIPT)]
    built = subprocess.run([*run, "build", str(spec), "--out", str(out)], capture_output=True, text=True, check=False)
    assert built.returncode == 0 and "RUNTIME_LANE_INDEX_BUILT records=1" in built.stdout
    verified = subprocess.run([*run, "verify", str(out)], capture_output=True, text=True, check=False)
    assert verified.returncode == 0 and "RUNTIME_LANE_INDEX_VERIFIED" in verified.stdout
    (tmp_path / "windows-ci-gates.log").write_text("tampered\n", encoding="utf-8")
    tampered = subprocess.run([*run, "verify", str(out)], capture_output=True, text=True, check=False)
    assert tampered.returncode == 1 and "EVIDENCE_HASH_MISMATCH" in tampered.stdout
    spec.write_text(json.dumps(_spec(_lane(evidence_path="absent.log"))), encoding="utf-8")
    refused = subprocess.run([*run, "build", str(spec), "--out", str(out)], capture_output=True, text=True, check=False)
    assert refused.returncode == 2 and "RUNTIME_LANE_EVIDENCE_MISSING" in refused.stdout
