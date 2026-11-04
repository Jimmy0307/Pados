# SPDX-License-Identifier: AGPL-3.0-only
"""OP-002: the readiness verdict is derived from the lane's own runner record, never from the index's word.

Two bypasses this pins closed:

* A hand-edited evidence index (status PASS, exit code 0) that still points at an unchanged, correctly hashed runner
  record that says FAIL. A hash only proves the bytes are the same; the gate must re-read what the bytes say.
* An index spec that names only the gates that passed (``ruff``) for a lane whose required check failed
  (``full_pytest``). The set of required checks belongs to the controlled lane registry, not to the spec.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.release.runtime_lane_index import IndexBuildError, build_index, load_spec
from pados.release.runtime_lanes import LaneStatus, evaluate, load_evidence, load_registry
from tests.release_supply_chain.lane_fixtures import e2e_record

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "scripts" / "release" / "runtime_lanes.v1.json"
REGISTRY = load_registry(REGISTRY_PATH)
GATE = ROOT / "scripts" / "release" / "runtime_lane_gate.py"
SHA = "a" * 40
OTHER = "b" * 40
WINDOWS_CI = REGISTRY.lanes["windows-ci-gates"].source
assert WINDOWS_CI is not None
WINDOWS_GATES = WINDOWS_CI.required_checks


def _now() -> datetime:
    return datetime.now(UTC)


def _write(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _gate_summary(root: Path, *, commit: str = SHA, exits: dict[str, int] | None = None,
                  finished: datetime | None = None, name: str = "gate-summary.json") -> Path:
    codes = {gate: 0 for gate in WINDOWS_GATES} | {"full_pytest": 1, "pack_focused": 1} | (exits or {})
    gates = {gate: {"command": gate, "exit_code": code} for gate, code in codes.items()}
    return _write(root / name, {"repo_head": commit, "platform": "Win32NT", "gates": gates,
                                "finished": (finished or _now()).isoformat()})


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hand_record(lane: str, path: Path, root: Path, **extra: Any) -> dict[str, Any]:
    record = {"lane_id": lane, "status": "PASS", "commit": SHA, "platform": REGISTRY.lanes[lane].platform,
              "command": "hand-written", "exit_code": 0, "evidence_path": path.relative_to(root).as_posix(),
              "evidence_sha256": _sha(path), "recorded_at": _now().isoformat()}
    record.update(extra)
    return record


def _verdict(root: Path, *records: dict[str, Any], lane: str) -> tuple[LaneStatus, str]:
    index = load_evidence({"schema_version": "pados.runtime-lane-evidence.v1", "records": list(records)})
    report = evaluate(REGISTRY, index, target="production", commit=SHA, now=_now(), evidence_root=root)
    found = next(v for v in report.lanes if v.lane_id == lane)
    return found.status, found.reason


# --- the gate re-reads the runner record -------------------------------------------------------------------------


def test_a_hand_edited_pass_over_a_failing_full_suite_record_is_not_verified(tmp_path: Path) -> None:
    summary = _gate_summary(tmp_path)  # full_pytest exit 1
    record = _hand_record("windows-full-suite", summary, tmp_path)  # PASS / 0 typed over it, hash intact
    status, reason = _verdict(tmp_path, record, lane="windows-full-suite")
    assert (status, reason) == (LaneStatus.FAIL, "SOURCE_RECORD_NOT_PASS")


def test_a_hand_written_pass_citing_a_failed_required_gate_fails(tmp_path: Path) -> None:
    summary = _gate_summary(tmp_path, exits={"mypy": 1})
    assert _verdict(tmp_path, _hand_record("windows-ci-gates", summary, tmp_path),
                    lane="windows-ci-gates") == (LaneStatus.FAIL, "SOURCE_RECORD_NOT_PASS")


def test_a_record_missing_a_required_gate_fails(tmp_path: Path) -> None:
    summary = _gate_summary(tmp_path)
    data = json.loads(summary.read_text(encoding="utf-8"))
    del data["gates"]["secret_scan"]
    _write(summary, data)
    assert _verdict(tmp_path, _hand_record("windows-ci-gates", summary, tmp_path),
                    lane="windows-ci-gates") == (LaneStatus.FAIL, "SOURCE_GATE_MISSING")


def test_a_record_for_another_commit_is_not_validated(tmp_path: Path) -> None:
    summary = _gate_summary(tmp_path, commit=OTHER)
    assert _verdict(tmp_path, _hand_record("windows-ci-gates", summary, tmp_path),
                    lane="windows-ci-gates") == (LaneStatus.NOT_VALIDATED, "SOURCE_COMMIT_MISMATCH")


def test_a_source_of_the_wrong_format_or_platform_fails(tmp_path: Path) -> None:
    e2e = _write(tmp_path / "E2E_RESULT.json", e2e_record(REGISTRY.lanes["windows-native-e2e"].source,  # type: ignore[arg-type]
                                                          commit=SHA, platform="Linux-6.8"))
    assert _verdict(tmp_path, _hand_record("windows-native-e2e", e2e, tmp_path),
                    lane="windows-native-e2e") == (LaneStatus.FAIL, "SOURCE_PLATFORM_MISMATCH")
    # An E2E result offered as the Windows gate summary is not a gate summary.
    assert _verdict(tmp_path, _hand_record("windows-ci-gates", e2e, tmp_path),
                    lane="windows-ci-gates") == (LaneStatus.FAIL, "SOURCE_FORMAT_INVALID")
    vm = _write(tmp_path / "vm.json", {"sha": SHA, "virt": "wsl", "e2e": 0, "finished": _now().isoformat(),
                                       "gates": {gate: 0 for gate in REGISTRY.lanes["vmware-linux-ci-gates"]
                                                 .source.required_checks}})  # type: ignore[union-attr]
    assert _verdict(tmp_path, _hand_record("vmware-linux-ci-gates", vm, tmp_path),
                    lane="vmware-linux-ci-gates") == (LaneStatus.FAIL, "SOURCE_PLATFORM_MISMATCH")


def test_staleness_counts_from_the_runner_record_not_the_index(tmp_path: Path) -> None:
    summary = _gate_summary(tmp_path, finished=_now() - timedelta(days=60))
    record = _hand_record("windows-ci-gates", summary, tmp_path)  # recorded_at "now" in the index
    assert _verdict(tmp_path, record, lane="windows-ci-gates") == (LaneStatus.NOT_VALIDATED, "EVIDENCE_STALE")


def test_missing_or_rehashed_evidence_still_fails(tmp_path: Path) -> None:
    summary = _gate_summary(tmp_path)
    record = _hand_record("windows-ci-gates", summary, tmp_path)
    # Rewritten with the same verdict but different bytes (an earlier finish time, so never byte-identical).
    _gate_summary(tmp_path, finished=_now() - timedelta(minutes=1))
    assert _verdict(tmp_path, record, lane="windows-ci-gates") == (LaneStatus.FAIL, "EVIDENCE_HASH_MISMATCH")
    summary.unlink()
    assert _verdict(tmp_path, record, lane="windows-ci-gates") == (LaneStatus.FAIL, "EVIDENCE_FILE_MISSING")


def test_a_lane_without_a_machine_source_never_passes(tmp_path: Path) -> None:
    log = tmp_path / "rea.log"
    log.write_text("REA ran fine\n", encoding="utf-8")
    assert _verdict(tmp_path, _hand_record("rea-runtime", log, tmp_path),
                    lane="rea-runtime") == (LaneStatus.NOT_VALIDATED, "LANE_HAS_NO_MACHINE_SOURCE")


def test_the_claim_cli_refuses_a_hand_edited_index(tmp_path: Path) -> None:
    summary = _gate_summary(tmp_path)
    index = _write(tmp_path / "index.json", {"schema_version": "pados.runtime-lane-evidence.v1",
                                             "records": [_hand_record("windows-full-suite", summary, tmp_path)]})
    result = subprocess.run([sys.executable, str(GATE), "--target", "production", "--out", str(tmp_path / "r.json"),
                             "--evidence", str(index), "--commit", SHA, "--claim"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "windows-full-suite:SOURCE_RECORD_NOT_PASS" in result.stdout
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert report["semantic_authority"] == "NONE" and report["ready"] is False


# --- the spec cannot choose the checks ---------------------------------------------------------------------------


def _spec(*lanes: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "pados.runtime-lane-spec.v1", "commit": SHA, "lanes": list(lanes)}


def test_the_spec_cannot_narrow_the_checks_to_the_ones_that_passed(tmp_path: Path) -> None:
    _gate_summary(tmp_path)
    narrowed = {"lane_id": "windows-full-suite", "platform": "windows", "command": "run_gates.ps1",
                "source": "gate-summary", "gates": ["ruff"], "evidence_path": "gate-summary.json"}
    with pytest.raises(IndexBuildError, match="RUNTIME_LANE_SPEC_INVALID"):
        build_index(load_spec(_spec(narrowed)), root=tmp_path, registry=REGISTRY)


def test_the_registry_checks_decide_the_built_status(tmp_path: Path) -> None:
    _gate_summary(tmp_path)
    lane = {"lane_id": "windows-full-suite", "platform": "windows", "command": "run_gates.ps1",
            "evidence_path": "gate-summary.json"}
    record = build_index(load_spec(_spec(lane)), root=tmp_path, registry=REGISTRY).records[0]
    assert (record.status, record.exit_code) == (LaneStatus.FAIL, 1)


def test_a_built_pass_still_verifies_at_the_gate(tmp_path: Path) -> None:
    _gate_summary(tmp_path)
    lane = {"lane_id": "windows-ci-gates", "platform": "windows", "command": "run_gates.ps1",
            "evidence_path": "gate-summary.json"}
    index = build_index(load_spec(_spec(lane)), root=tmp_path, registry=REGISTRY)
    report = evaluate(REGISTRY, index, target="production", commit=SHA, now=_now(), evidence_root=tmp_path)
    found = next(v for v in report.lanes if v.lane_id == "windows-ci-gates")
    assert (found.status, found.reason) == (LaneStatus.PASS, "VERIFIED")
