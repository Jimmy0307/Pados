# SPDX-License-Identifier: AGPL-3.0-only
"""OP-002: the PostgreSQL gate and GitHub check-run records as lane sources, and the wrong, partial or stale ones."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.release.runtime_lane_sources import SourceError, read_source
from pados.release.runtime_lanes import LaneStatus, evaluate, load_evidence, load_registry

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = load_registry(ROOT / "scripts" / "release" / "runtime_lanes.v1.json")
SHA = "c" * 40
PG = REGISTRY.lanes["postgres-integration"].source
GH = REGISTRY.lanes["github-ci"].source
assert PG is not None and GH is not None


def _now() -> datetime:
    return datetime.now(UTC)


def _suite_report(root: Path, suite: str, **change: Any) -> str:
    body = {"gate": f"postgres-{suite}", "status": "PASS", "counts": {"passed": 40}, "skipped": [], "problems": [],
            "source_commit": SHA} | change
    path = root / f"postgres-{suite}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pg(root: Path, *, suites: tuple[str, ...] | None = None, finished: datetime | None = None,
        **change: Any) -> Path:
    reports = {suite: {"status": "PASS", "sha256": _suite_report(root, suite)}
               for suite in (suites if suites is not None else PG.required_checks)}  # type: ignore[union-attr]
    body = {"schema_version": "pados.postgres-integration.v1", "status": "PASS", "suite_reports": reports,
            "source_commit": SHA, "platform": "linux", "finished": (finished or _now()).isoformat()} | change
    path = root / "postgres-integration.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _code(path: Path, source: Any) -> str:
    with pytest.raises(SourceError) as caught:
        read_source(source, path, commit=SHA)
    return caught.value.code


def test_a_complete_postgres_gate_record_reads_as_pass(tmp_path: Path) -> None:
    reading = read_source(PG, _pg(tmp_path), commit=SHA)  # type: ignore[arg-type]
    assert (reading.exit_code, reading.platform) == (0, "linux")


def test_postgres_gate_defects(tmp_path: Path) -> None:
    assert _code(_pg(tmp_path, suites=("d1-binding",)), PG) == "SOURCE_GATE_MISSING"  # a --suite subset
    assert _code(_pg(tmp_path, source_commit="d" * 40), PG) == "SOURCE_COMMIT_MISMATCH"
    assert _code(_pg(tmp_path, schema_version=None), PG) == "SOURCE_FORMAT_INVALID"  # the pre-v1 aggregate
    path = _pg(tmp_path)
    _suite_report(tmp_path, "residual", status="FAIL")  # rewritten after the aggregate hashed it
    assert _code(path, PG) == "SOURCE_SUITE_REPORT_HASH_MISMATCH"
    (tmp_path / "postgres-residual.json").unlink()
    assert _code(path, PG) == "SOURCE_SUITE_REPORT_MISSING"


@pytest.mark.parametrize("change", [{"skipped": ["tests/x.py::t"]}, {"status": "FAIL"}, {"problems": ["MIN_TESTS"]},
                                    {"counts": {"passed": 0}}])
def test_a_suite_with_skips_failures_or_no_tests_is_not_a_pass(tmp_path: Path, change: dict[str, Any]) -> None:
    path = _pg(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["suite_reports"]["migration"]["sha256"] = _suite_report(tmp_path, "migration", **change)
    path.write_text(json.dumps(body), encoding="utf-8")
    assert read_source(PG, path, commit=SHA).exit_code == 1  # type: ignore[arg-type]


def test_the_gate_writes_the_record_the_reader_accepts(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("pg_gate", ROOT / "scripts" / "release" /
                                                  "postgres_integration_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    head = module._summary(tmp_path, {})["source_commit"]  # the checkout's own HEAD
    for suite in PG.required_checks:  # type: ignore[union-attr]
        _suite_report(tmp_path, suite, source_commit=head)
    summary = module._summary(tmp_path, dict.fromkeys(PG.required_checks, 0))  # type: ignore[union-attr]
    path = tmp_path / "postgres-integration.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    assert read_source(PG, path, commit=head).exit_code == 0  # type: ignore[arg-type]
    assert set(summary) >= {"status", "suites"}  # the existing fields stay for current readers


# --- GitHub check runs -------------------------------------------------------------------------------------------


def _run(name: str, **change: Any) -> dict[str, Any]:
    return {"name": name, "head_sha": SHA, "status": "completed", "conclusion": "success",
            "url": "https://api.github.com/repos/Jimmy0307/Pados/check-runs/1",
            "completed_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "app": {"slug": "github-actions", "id": 15368}} | change


def _gh(root: Path, runs: list[dict[str, Any]] | None = None, total: int | None = None) -> Path:
    runs = runs if runs is not None else [_run(name) for name in GH.required_checks]  # type: ignore[union-attr]
    path = root / "check-runs.json"
    path.write_text(json.dumps({"total_count": len(runs) if total is None else total, "check_runs": runs}),
                    encoding="utf-8")
    return path


def test_complete_successful_check_runs_read_as_pass(tmp_path: Path) -> None:
    extra = _run(GH.required_checks[0])  # type: ignore[union-attr]  # the push and pull_request runs of one job
    assert read_source(GH, _gh(tmp_path, [*[_run(n) for n in GH.required_checks], extra]),  # type: ignore[arg-type,union-attr]
                       commit=SHA).exit_code == 0


def test_check_run_defects(tmp_path: Path) -> None:
    names = list(GH.required_checks)  # type: ignore[union-attr]
    assert _code(_gh(tmp_path, [_run(n) for n in names[1:]]), GH) == "SOURCE_GATE_MISSING"
    assert _code(_gh(tmp_path, [_run(n, app={"slug": "someone-else"}) for n in names]), GH) == "SOURCE_GATE_MISSING"
    assert _code(_gh(tmp_path, [_run(n, head_sha="e" * 40) for n in names]), GH) == "SOURCE_COMMIT_MISMATCH"
    assert _code(_gh(tmp_path, [_run(names[0], status="in_progress", conclusion=None),
                                *[_run(n) for n in names[1:]]]), GH) == "SOURCE_CHECK_INCOMPLETE"
    assert _code(_gh(tmp_path, total=len(names) + 30), GH) == "SOURCE_FORMAT_INVALID"  # a truncated first page
    fork = "https://api.github.com/repos/someone/Pados/check-runs/9"  # a fork has the same commit SHA
    assert _code(_gh(tmp_path, [_run(n, url=fork) for n in names]), GH) == "SOURCE_REPOSITORY_MISMATCH"
    other_app = {"slug": "github-actions", "id": 1}  # the right slug from another app id
    assert _code(_gh(tmp_path, [_run(n, app=other_app) for n in names]), GH) == "SOURCE_GATE_MISSING"
    failed = [_run(names[0], conclusion="failure"), *[_run(n) for n in names]]  # one run of a name failed
    assert read_source(GH, _gh(tmp_path, failed), commit=SHA).exit_code == 1  # type: ignore[arg-type]


def test_check_runs_at_the_gate_bind_commit_and_freshness(tmp_path: Path) -> None:
    old = (_now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    names = GH.required_checks  # type: ignore[union-attr]

    def verdict(path: Path) -> tuple[LaneStatus, str]:
        record = {"lane_id": "github-ci", "status": "PASS", "commit": SHA, "platform": "github", "command": "gh api",
                  "exit_code": 0, "evidence_path": path.name,
                  "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "recorded_at": _now().isoformat()}
        index = load_evidence({"schema_version": "pados.runtime-lane-evidence.v1", "records": [record]})
        report = evaluate(REGISTRY, index, target="production", commit=SHA, now=_now(), evidence_root=tmp_path)
        found = next(v for v in report.lanes if v.lane_id == "github-ci")
        return found.status, found.reason

    assert verdict(_gh(tmp_path)) == (LaneStatus.PASS, "VERIFIED")
    assert verdict(_gh(tmp_path, [_run(n, completed_at=old) for n in names])) == (LaneStatus.NOT_VALIDATED,
                                                                                  "EVIDENCE_STALE")
    assert verdict(_gh(tmp_path, [_run(n, conclusion="failure") for n in names])) == (LaneStatus.FAIL,
                                                                                      "SOURCE_RECORD_NOT_PASS")


def test_a_suite_below_the_registry_minimum_is_not_a_pass(tmp_path: Path) -> None:
    path = _pg(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["suite_reports"]["d1-binding"]["sha256"] = _suite_report(tmp_path, "d1-binding", counts={"passed": 3})
    path.write_text(json.dumps(body), encoding="utf-8")
    assert read_source(PG, path, commit=SHA).exit_code == 1  # type: ignore[arg-type]


def test_the_registry_minimums_match_the_gate_runner() -> None:
    spec = importlib.util.spec_from_file_location("pg_gate_min", ROOT / "scripts" / "release" /
                                                  "postgres_integration_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert PG.min_passed == module.MIN_TESTS  # type: ignore[union-attr]


def test_a_wsl_postgres_record_is_not_a_linux_record(tmp_path: Path) -> None:
    path = _pg(tmp_path, platform="Linux-5.15.153.1-microsoft-standard-WSL2-x86_64-with-glibc2.35")
    assert read_source(PG, path, commit=SHA).platform == "linux-wsl"  # type: ignore[arg-type]


@pytest.mark.parametrize("drop", ["finished", "platform"])
def test_a_gate_summary_without_its_finish_time_or_platform_is_refused(tmp_path: Path, drop: str) -> None:
    source = REGISTRY.lanes["windows-full-suite"].source
    body = {"repo_head": SHA, "platform": "Win32NT", "finished": _now().isoformat(),
            "gates": {"full_pytest": {"exit_code": 0}}}
    del body[drop]
    path = tmp_path / "gate-summary.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    assert _code(path, source) == "SOURCE_FORMAT_INVALID"  # never the file's mtime, never a default platform
