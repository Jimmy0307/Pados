# SPDX-License-Identifier: AGPL-3.0-only
"""OP-002: per-lane runtime validation status, bound to evidence, and a readiness claim it cannot fake.

The gate only reads evidence. A required lane counts only with a PASS record for the exact target commit whose
evidence file exists and hashes to the recorded digest, recorded inside the lane's freshness window and with exit code
0. Anything else (missing, stale, wrong commit, hash mismatch, SKIPPED, NOT_VALIDATED, FAIL) blocks the target. The
gate grants no approval and no acceptance.
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.release.runtime_lanes import (
    LaneStatus,
    RuntimeLaneRegistry,
    evaluate,
    load_evidence,
    load_registry,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "scripts" / "release" / "runtime_lanes.v1.json"
COMMIT = "a" * 40
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _registry() -> RuntimeLaneRegistry:
    return RuntimeLaneRegistry.model_validate({
        "schema_version": "pados.runtime-lanes.v1",
        "targets": {"dogfood": {"description": "test target", "required_lanes": ["alpha", "beta"]}},
        "lanes": {
            "alpha": {"description": "alpha lane", "platform": "windows", "max_age_days": 7, "claims": ["A"]},
            "beta": {"description": "beta lane", "platform": "linux", "max_age_days": 7, "claims": ["B"]},
            "gamma": {"description": "optional lane", "platform": "linux", "max_age_days": 7, "claims": ["C"]},
        },
    })


def _record(tmp_path: Path, lane: str, **overrides: Any) -> dict[str, Any]:
    evidence = tmp_path / f"{lane}.log"
    evidence.write_text(f"{lane} evidence\n", encoding="utf-8")
    record = {"lane_id": lane, "status": "PASS", "commit": COMMIT, "platform": _registry().lanes[lane].platform,
              "command": f"run {lane}", "exit_code": 0, "evidence_path": evidence.name,
              "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
              "recorded_at": (NOW - timedelta(days=1)).isoformat()}
    record.update(overrides)
    return record


def _evaluate(tmp_path: Path, records: list[dict[str, Any]], *, commit: str = COMMIT) -> Any:
    return evaluate(_registry(), load_evidence({"schema_version": "pados.runtime-lane-evidence.v1",
                                                "records": records}),
                    target="dogfood", commit=commit, now=NOW, evidence_root=tmp_path)


def _verdict(report: Any, lane: str) -> Any:
    return next(v for v in report.lanes if v.lane_id == lane)


def test_all_required_lanes_with_valid_evidence_make_the_target_ready(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, [_record(tmp_path, "alpha"), _record(tmp_path, "beta")])
    assert report.ready is True and report.blockers == ()
    assert report.semantic_authority == "NONE"


def test_a_failing_optional_lane_never_blocks_but_stays_visible(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, [_record(tmp_path, "alpha"), _record(tmp_path, "beta"),
                                  _record(tmp_path, "gamma", status="FAIL", exit_code=1)])
    assert report.ready is True
    assert _verdict(report, "gamma").status is LaneStatus.FAIL and _verdict(report, "gamma").required is False


@pytest.mark.parametrize(("change", "reason", "status"), [
    ({"status": "SKIPPED", "exit_code": None}, "LANE_SKIPPED", LaneStatus.SKIPPED),
    ({"status": "NOT_VALIDATED", "exit_code": None}, "LANE_NOT_VALIDATED", LaneStatus.NOT_VALIDATED),
    ({"status": "FAIL", "exit_code": 1}, "LANE_FAILED", LaneStatus.FAIL),
    ({"exit_code": 1}, "PASS_WITH_NONZERO_EXIT", LaneStatus.FAIL),
    ({"commit": "b" * 40}, "EVIDENCE_COMMIT_MISMATCH", LaneStatus.NOT_VALIDATED),
    ({"recorded_at": (NOW - timedelta(days=30)).isoformat()}, "EVIDENCE_STALE", LaneStatus.NOT_VALIDATED),
    ({"recorded_at": (NOW + timedelta(days=1)).isoformat()}, "EVIDENCE_FROM_THE_FUTURE", LaneStatus.FAIL),
    ({"evidence_sha256": "0" * 64}, "EVIDENCE_HASH_MISMATCH", LaneStatus.FAIL),
    ({"evidence_path": "missing.log"}, "EVIDENCE_FILE_MISSING", LaneStatus.FAIL),
    ({"evidence_path": "../outside.log"}, "EVIDENCE_PATH_OUTSIDE_ROOT", LaneStatus.FAIL),
    ({"platform": "macos"}, "EVIDENCE_PLATFORM_MISMATCH", LaneStatus.FAIL),
])
def test_any_defect_in_required_evidence_blocks_the_target(tmp_path: Path, change: dict[str, Any], reason: str,
                                                          status: LaneStatus) -> None:
    report = _evaluate(tmp_path, [_record(tmp_path, "alpha"), _record(tmp_path, "beta", **change)])
    verdict = _verdict(report, "beta")
    assert report.ready is False
    assert (verdict.status, verdict.reason) == (status, reason)
    assert any(blocker.startswith("beta:") for blocker in report.blockers)


def test_a_missing_record_is_not_validated_and_blocks(tmp_path: Path) -> None:
    report = _evaluate(tmp_path, [_record(tmp_path, "alpha")])
    assert report.ready is False
    assert (_verdict(report, "beta").status, _verdict(report, "beta").reason) == (
        LaneStatus.NOT_VALIDATED, "NO_EVIDENCE_RECORD")


def test_evidence_for_one_lane_never_satisfies_another(tmp_path: Path) -> None:
    alpha = _record(tmp_path, "alpha")
    # The beta record cites alpha's evidence file: the digest matches, but one run cannot prove two lanes.
    report = _evaluate(tmp_path, [alpha, {**alpha, "lane_id": "beta", "platform": "linux"}])
    assert report.ready is False
    for lane in ("alpha", "beta"):
        assert (_verdict(report, lane).status, _verdict(report, lane).reason) == (
            LaneStatus.FAIL, "EVIDENCE_REUSED_ACROSS_LANES")
    with pytest.raises(ValueError, match="DUPLICATE_LANE_RECORD"):
        load_evidence({"schema_version": "pados.runtime-lane-evidence.v1", "records": [alpha, alpha]})


def test_an_unknown_lane_or_target_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="UNKNOWN_TARGET"):
        evaluate(_registry(), load_evidence({"schema_version": "pados.runtime-lane-evidence.v1", "records": []}),
                 target="production-everything", commit=COMMIT, now=NOW, evidence_root=tmp_path)
    report = _evaluate(tmp_path, [_record(tmp_path, "alpha"), _record(tmp_path, "beta"),
                                  {**_record(tmp_path, "alpha"), "lane_id": "not-a-lane"}])
    assert "not-a-lane:UNKNOWN_LANE" in report.blockers and report.ready is False


# --- the repository registry ------------------------------------------------------------------------------------

def test_the_repository_registry_keeps_unvalidated_runtimes_visible() -> None:
    registry = load_registry(REGISTRY)
    lanes = registry.lanes
    for lane in ("rea-runtime", "openmaic-runtime", "ai-scientist-v3-runtime", "local-model-governed-acceptance"):
        assert lanes[lane].known_status is LaneStatus.NOT_VALIDATED, lane
    measured = lanes["local-model-measured-execution"]
    assert "MODEL_OUTPUT_TO_RESULT_CANDIDATE_OR_CAS" in measured.does_not_cover
    assert {"REA_RUNTIME", "OPENMAIC_RUNTIME", "AI_SCIENTIST_V3_RUNTIME"} <= set(measured.does_not_cover)
    production = registry.targets["production"].required_lanes
    assert {"rea-runtime", "openmaic-runtime", "ai-scientist-v3-runtime", "local-model-governed-acceptance",
            "github-ci"} <= set(production)


def test_production_is_not_ready_without_evidence_for_every_required_lane(tmp_path: Path) -> None:
    registry = load_registry(REGISTRY)
    report = evaluate(registry, load_evidence({"schema_version": "pados.runtime-lane-evidence.v1", "records": []}),
                      target="production", commit=COMMIT, now=NOW, evidence_root=tmp_path)
    assert report.ready is False
    assert {verdict.status for verdict in report.lanes if verdict.required} == {LaneStatus.NOT_VALIDATED}


def test_the_gate_has_no_authority_imports() -> None:
    tree = ast.parse((ROOT / "src" / "pados" / "release" / "runtime_lanes.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    forbidden = ("pados.control", "pados.loop", "pados.state_proof", "pados.remote", "pados.capability", "pados.db")
    assert not [name for name in imported if name.startswith(forbidden)]


# --- the CLI ----------------------------------------------------------------------------------------------------

def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "release" / "runtime_lane_gate.py"), *args],
                          capture_output=True, text=True, check=False, cwd=ROOT)


def test_report_mode_records_status_without_claiming_and_claim_mode_refuses(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    report = _cli(tmp_path, "--target", "production", "--commit", COMMIT, "--out", str(out))
    assert report.returncode == 0
    body = json.loads(out.read_text(encoding="utf-8"))
    assert body["ready"] is False and body["semantic_authority"] == "NONE"
    assert body["target"] == "production" and body["commit"] == COMMIT
    claim = _cli(tmp_path, "--target", "production", "--commit", COMMIT, "--claim", "--out", str(out))
    assert claim.returncode == 1 and "RUNTIME_READINESS_NOT_PROVEN" in claim.stdout


def test_malformed_evidence_fails_closed(tmp_path: Path) -> None:
    bad = tmp_path / "evidence.json"
    bad.write_text(json.dumps({"schema_version": "pados.runtime-lane-evidence.v1",
                               "records": [{"lane_id": "github-ci", "status": "GREEN"}]}), encoding="utf-8")
    result = _cli(tmp_path, "--target", "production", "--commit", COMMIT, "--evidence", str(bad),
                  "--out", str(tmp_path / "r.json"))
    assert result.returncode == 2 and "RUNTIME_LANE_EVIDENCE_INVALID" in result.stdout
