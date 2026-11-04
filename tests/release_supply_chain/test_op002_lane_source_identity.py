# SPDX-License-Identifier: AGPL-3.0-only
"""OP-002: a lane record must be the complete, right run, not just a passing one.

* E2E: the registry's complete step list, each step once, a ``full`` run (a cut-down or ``--skip-acceptance-suite``
  record never stands in for the full lane).
* PostgreSQL: each suite report must be that suite's own report (gate name and exact test paths), not another suite's
  passing report copied into its slot and re-hashed.
* GitHub: freshness counts per required check, and each check must come from its registry workflow, job and steps; a
  dependency review that reports an environment blocker is NOT_VALIDATED even though its job concluded success.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.release.runtime_lane_sources import LaneSource, SourceError, read_source
from pados.release.runtime_lanes import LaneStatus, evaluate, load_evidence, load_registry
from tests.release_supply_chain.lane_fixtures import GithubBundle, e2e_record, now, stamp, suite_report

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = load_registry(ROOT / "scripts" / "release" / "runtime_lanes.v1.json")
SHA = "b" * 40
E2E = REGISTRY.lanes["windows-native-e2e"].source
PG = REGISTRY.lanes["postgres-integration"].source
GH = REGISTRY.lanes["github-ci"].source
assert E2E is not None and PG is not None and GH is not None


def _write(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _code(source: LaneSource, path: Path) -> str:
    with pytest.raises(SourceError) as caught:
        read_source(source, path, commit=SHA)
    return caught.value.code


def _gate(root: Path, lane: str, path: Path, platform: str) -> tuple[LaneStatus, str]:
    record = {"lane_id": lane, "status": "PASS", "commit": SHA, "platform": platform, "command": "runner",
              "exit_code": 0, "evidence_path": path.name, "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "recorded_at": now().isoformat()}
    index = load_evidence({"schema_version": "pados.runtime-lane-evidence.v1", "records": [record]})
    report = evaluate(REGISTRY, index, target="production", commit=SHA, now=now(), evidence_root=root)
    verdict = next(v for v in report.lanes if v.lane_id == lane)
    assert report.semantic_authority == "NONE"
    return verdict.status, verdict.reason


def _module(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- E2E: the registry's step list ---------------------------------------------------------------------------------


def test_a_complete_full_e2e_run_passes(tmp_path: Path) -> None:
    path = _write(tmp_path / "E2E_RESULT.json", e2e_record(E2E, commit=SHA))  # type: ignore[arg-type]
    assert _gate(tmp_path, "windows-native-e2e", path, "windows") == (LaneStatus.PASS, "VERIFIED")


def test_an_e2e_record_cut_to_one_step_is_not_the_lane(tmp_path: Path) -> None:
    # The review's case: the real 24-step record cut to its first step, verdict still PASS.
    cut = e2e_record(E2E, commit=SHA, steps=[E2E.required_checks[0]])  # type: ignore[arg-type,union-attr]
    path = _write(tmp_path / "E2E_RESULT.json", cut)
    assert _gate(tmp_path, "windows-native-e2e", path, "windows") == (LaneStatus.FAIL, "SOURCE_GATE_MISSING")


def test_a_skip_acceptance_suite_run_is_not_validated_for_the_full_lane(tmp_path: Path) -> None:
    steps = [name for name in E2E.required_checks if name != "governed_acceptance_suite"]  # type: ignore[union-attr]
    reduced = e2e_record(E2E, commit=SHA, steps=steps, run_mode="skip-acceptance-suite",  # type: ignore[arg-type]
                         skipped_steps=["governed_acceptance_suite"])
    path = _write(tmp_path / "E2E_RESULT.json", reduced)
    assert _gate(tmp_path, "windows-native-e2e", path, "windows") == (LaneStatus.NOT_VALIDATED, "SOURCE_RUN_REDUCED")
    # The same reduced step list relabelled as a full run is still short of the registry's list.
    path = _write(tmp_path / "E2E_RESULT.json", reduced | {"run_mode": "full", "skipped_steps": []})
    assert _gate(tmp_path, "windows-native-e2e", path, "windows") == (LaneStatus.FAIL, "SOURCE_GATE_MISSING")


@pytest.mark.parametrize(("steps", "code"), [
    (["postgres_ready", "postgres_ready"], "SOURCE_STEP_REPEATED"),  # extended below to the full list
    (["renamed_governed_acceptance"], "SOURCE_STEP_UNEXPECTED"),
])
def test_repeated_or_unexpected_steps_are_refused(tmp_path: Path, steps: list[str], code: str) -> None:
    full = list(E2E.required_checks)  # type: ignore[union-attr]
    names = full + steps[1:] if code == "SOURCE_STEP_REPEATED" else full + steps
    path = _write(tmp_path / "E2E_RESULT.json", e2e_record(E2E, commit=SHA, steps=names))  # type: ignore[arg-type]
    assert _code(E2E, path) == code  # type: ignore[arg-type]


def test_a_renamed_step_is_both_missing_and_refused(tmp_path: Path) -> None:
    names = [("acceptance_suite" if name == "governed_acceptance_suite" else name)
             for name in E2E.required_checks]  # type: ignore[union-attr]
    path = _write(tmp_path / "E2E_RESULT.json", e2e_record(E2E, commit=SHA, steps=names))  # type: ignore[arg-type]
    assert _code(E2E, path) == "SOURCE_GATE_MISSING"  # type: ignore[arg-type]


def test_an_e2e_record_without_the_runner_contract_or_mode_is_refused(tmp_path: Path) -> None:
    record = e2e_record(E2E, commit=SHA)  # type: ignore[arg-type]
    del record["runner_contract"]
    assert _code(E2E, _write(tmp_path / "a.json", record)) == "SOURCE_FORMAT_INVALID"  # type: ignore[arg-type]
    record = e2e_record(E2E, commit=SHA)  # type: ignore[arg-type]
    del record["run_mode"]
    assert _code(E2E, _write(tmp_path / "b.json", record)) == "SOURCE_RUN_REDUCED"  # type: ignore[arg-type]


def test_a_failed_step_is_a_failure_whatever_else_is_missing(tmp_path: Path) -> None:
    record = e2e_record(E2E, commit=SHA, steps=["postgres_ready", "unexpected_failure"])  # type: ignore[arg-type]
    record["steps"][1]["status"] = "FAIL"
    record["verdict"] = "FAIL"
    assert read_source(E2E, _write(tmp_path / "f.json", record), commit=SHA).exit_code == 1  # type: ignore[arg-type]


def _recorded_step_names(script: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "record"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            names.add(node.args[0].value)
    return names


def test_the_registry_step_list_is_exactly_what_the_runner_records() -> None:
    recorded = _recorded_step_names(ROOT / "scripts" / "dogfood" / "local_dogfood_e2e.py")
    assert recorded - {"unexpected_failure"} == set(E2E.required_checks)  # type: ignore[union-attr]
    assert REGISTRY.lanes["vmware-linux-e2e"].source == E2E  # the same runner contract on both platforms
    text = (ROOT / "scripts" / "dogfood" / "local_dogfood_e2e.py").read_text(encoding="utf-8")
    assert '"runner_contract": "pados.local-dogfood-e2e.v1"' in text and '"run_mode":' in text


# --- PostgreSQL: each suite report is that suite's own ------------------------------------------------------------


def _pg(root: Path, reports: dict[str, dict[str, Any]] | None = None) -> Path:
    shas = {}
    for suite in PG.required_checks:  # type: ignore[union-attr]
        body = (reports or {}).get(suite) or suite_report(PG, suite, commit=SHA)  # type: ignore[arg-type]
        path = _write(root / f"postgres-{suite}.json", body)
        shas[suite] = {"status": "PASS", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return _write(root / "postgres-integration.json", {
        "schema_version": "pados.postgres-integration.v1", "status": "PASS", "source_commit": SHA,
        "platform": "Windows-11-10.0.26200-SP0", "finished": now().isoformat(), "suite_reports": shas,
        "suites": dict.fromkeys(shas, "PASS")})


def test_a_complete_postgres_record_passes(tmp_path: Path) -> None:
    assert _gate(tmp_path, "postgres-integration", _pg(tmp_path), "windows") == (LaneStatus.PASS, "VERIFIED")


def test_a_passing_report_copied_into_another_suite_slot_is_refused(tmp_path: Path) -> None:
    residual = suite_report(PG, "residual", commit=SHA)  # type: ignore[arg-type]
    path = _pg(tmp_path, {"migration": residual})  # copied and re-hashed in the aggregate
    assert _gate(tmp_path, "postgres-integration", path, "windows") == (LaneStatus.FAIL,
                                                                         "SOURCE_SUITE_IDENTITY_MISMATCH")
    # Renaming the gate is not enough either: the test paths are still the residual suite's.
    path = _pg(tmp_path, {"migration": residual | {"gate": "postgres-migration"}})
    assert _code(PG, path) == "SOURCE_SUITE_IDENTITY_MISMATCH"  # type: ignore[arg-type]


@pytest.mark.parametrize("paths", ["drop_one", "add_one", "reorder"])
def test_a_suite_report_with_other_test_paths_is_refused(tmp_path: Path, paths: str) -> None:
    own = list(PG.suite_paths["residual"])  # type: ignore[union-attr]
    changed = {"drop_one": own[1:], "add_one": [*own, "tests/integration/test_other.py"],
               "reorder": list(reversed(own))}[paths]
    body = suite_report(PG, "residual", commit=SHA, paths=changed)  # type: ignore[arg-type]
    assert _code(PG, _pg(tmp_path, {"residual": body})) == "SOURCE_SUITE_IDENTITY_MISMATCH"  # type: ignore[arg-type]


@pytest.mark.parametrize("change", [{"missing_paths": ["tests/integration/test_x.py"]},
                                    {"empty_paths": ["tests/integration/test_x.py"]},
                                    {"counts": {"passed": 1}}])
def test_missing_empty_or_too_few_tests_is_not_a_pass(tmp_path: Path, change: dict[str, Any]) -> None:
    body = suite_report(PG, "d4-handoff", commit=SHA, **change)  # type: ignore[arg-type]
    assert read_source(PG, _pg(tmp_path, {"d4-handoff": body}), commit=SHA).exit_code == 1  # type: ignore[arg-type]


def test_the_hash_binds_bytes_not_authorship(tmp_path: Path) -> None:
    # The stated limit: a report written by hand with the right identity and re-hashed into the aggregate reads as
    # PASS. The SHA-256 proves byte identity with what the aggregate names, not who wrote it or that tests ran.
    forged = suite_report(PG, "residual", commit=SHA)  # type: ignore[arg-type]
    assert read_source(PG, _pg(tmp_path, {"residual": forged}), commit=SHA).exit_code == 0  # type: ignore[arg-type]
    doc = (ROOT / "src" / "pados" / "release" / "runtime_lane_sources.py").read_text(encoding="utf-8")
    assert "does not authenticate who wrote them" in doc


def test_the_registry_suite_paths_match_the_gate_runner() -> None:
    module = _module("pg_gate_paths", "scripts/release/postgres_integration_gate.py")
    assert {name: tuple(paths) for name, paths in module.SUITE_PATHS.items()} == PG.suite_paths  # type: ignore[union-attr]
    assert module.MIN_TESTS == PG.min_passed  # type: ignore[union-attr]


# --- GitHub: per-check freshness and workflow identity ------------------------------------------------------------


def test_a_complete_bundle_passes(tmp_path: Path) -> None:
    path = _write(tmp_path / "gh.json", GithubBundle(GH, SHA).record())  # type: ignore[arg-type]
    assert _gate(tmp_path, "github-ci", path, "github") == (LaneStatus.PASS, "VERIFIED")


def test_one_stale_required_check_makes_the_lane_stale(tmp_path: Path) -> None:
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    old = stamp(now() - timedelta(days=40))
    for run in bundle.named("dependency review"):
        run["completed_at"] = old
    path = _write(tmp_path / "gh.json", bundle.record())
    assert _gate(tmp_path, "github-ci", path, "github") == (LaneStatus.NOT_VALIDATED, "EVIDENCE_STALE")


def test_one_future_check_is_refused_even_when_the_others_are_fresh(tmp_path: Path) -> None:
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    for run in bundle.named("codeql (python)"):
        run["completed_at"] = stamp(now() + timedelta(days=2))
    path = _write(tmp_path / "gh.json", bundle.record())
    assert _gate(tmp_path, "github-ci", path, "github") == (LaneStatus.FAIL, "EVIDENCE_FROM_THE_FUTURE")


def test_a_same_named_check_from_another_workflow_is_refused(tmp_path: Path) -> None:
    # A workflow added on the branch can name its job like a required check, on the same commit and app.
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    bundle.add("linux (lint, types, tests, build)", workflow=".github/workflows/lookalike.yml")
    path = _write(tmp_path / "gh.json", bundle.record())
    assert _gate(tmp_path, "github-ci", path, "github") == (LaneStatus.FAIL, "SOURCE_WORKFLOW_MISMATCH")


def test_job_and_workflow_identity_defects(tmp_path: Path) -> None:
    def code(change: Any) -> str:
        bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
        change(bundle)
        return _code(GH, _write(tmp_path / "gh.json", bundle.record()))  # type: ignore[arg-type]

    first = "windows (native suites)"
    assert code(lambda b: b.jobs.pop(str(b.named(first)[0]["id"]))) == "SOURCE_JOB_IDENTITY_MISMATCH"
    assert code(lambda b: b.jobs[str(b.named(first)[0]["id"])].update(name="other")) == "SOURCE_JOB_IDENTITY_MISMATCH"
    assert code(lambda b: b.jobs[str(b.named(first)[0]["id"])].update(
        check_run_url="https://api.github.com/repos/Jimmy0307/Pados/check-runs/1")) == "SOURCE_JOB_IDENTITY_MISMATCH"
    assert code(lambda b: b.jobs[str(b.named(first)[0]["id"])].update(head_sha="e" * 40)) == (
        "SOURCE_JOB_IDENTITY_MISMATCH")

    def other_repository(bundle: GithubBundle) -> None:
        for run in bundle.workflow_runs.values():
            run["repository"] = {"full_name": "someone/Pados"}

    assert code(other_repository) == "SOURCE_WORKFLOW_MISMATCH"
    assert code(lambda b: b.workflow_runs.clear()) == "SOURCE_WORKFLOW_MISMATCH"


def test_record_level_defects(tmp_path: Path) -> None:
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    assert _code(GH, _write(tmp_path / "a.json", bundle.record(commit="d" * 40))) == "SOURCE_COMMIT_MISMATCH"  # type: ignore[arg-type]
    assert _code(GH, _write(tmp_path / "b.json", bundle.record(repository="someone/Pados"))) == (  # type: ignore[arg-type]
        "SOURCE_REPOSITORY_MISMATCH")
    # The previous format, the bare check-runs response, no longer carries the workflow identity.
    bare = bundle.record()["check_runs"]
    assert _code(GH, _write(tmp_path / "c.json", bare)) == "SOURCE_FORMAT_INVALID"  # type: ignore[arg-type]


def test_a_dependency_review_environment_blocker_is_not_validated(tmp_path: Path) -> None:
    # The job concluded success, but its classifier reported that the review did not run.
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    for run in bundle.named("dependency review"):
        bundle.annotations[str(run["id"])] = [{"annotation_level": "warning",
                                               "title": "DEPENDENCY_REVIEW=ENVIRONMENT_BLOCKER",
                                               "message": "Dependency graph is disabled for this repository"}]
    path = _write(tmp_path / "gh.json", bundle.record())
    assert _gate(tmp_path, "github-ci", path, "github") == (LaneStatus.NOT_VALIDATED,
                                                             "SOURCE_CHECK_ENVIRONMENT_BLOCKED")


def test_an_actual_dependency_review_success_still_passes(tmp_path: Path) -> None:
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    for run in bundle.named("dependency review"):  # an unrelated notice, as GitHub adds to every ubuntu job
        bundle.annotations[str(run["id"])] = [{"annotation_level": "notice", "title": "",
                                               "message": "The ubuntu-latest label will migrate to Ubuntu 26"}]
    assert read_source(GH, _write(tmp_path / "gh.json", bundle.record()), commit=SHA).exit_code == 0  # type: ignore[arg-type]


@pytest.mark.parametrize("conclusion", ["skipped", "failure", None])
def test_a_required_step_that_did_not_succeed_is_not_validated(tmp_path: Path, conclusion: str | None) -> None:
    # "failure" with the job still green is what continue-on-error looks like.
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    for run in bundle.named("dependency review"):
        steps = bundle.jobs[str(run["id"])]["steps"]
        next(step for step in steps if step["name"] == "Dependency review")["conclusion"] = conclusion
    path = _write(tmp_path / "gh.json", bundle.record())
    assert _gate(tmp_path, "github-ci", path, "github") == (LaneStatus.NOT_VALIDATED, "SOURCE_CHECK_NOT_EXECUTED")


def test_a_failed_check_is_a_failure_even_beside_a_blocker(tmp_path: Path) -> None:
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    bundle.add("windows (native suites)", conclusion="failure")
    for run in bundle.named("dependency review"):
        bundle.annotations[str(run["id"])] = [{"title": "DEPENDENCY_REVIEW=ENVIRONMENT_BLOCKER", "message": ""}]
    path = _write(tmp_path / "gh.json", bundle.record())
    assert _gate(tmp_path, "github-ci", path, "github") == (LaneStatus.FAIL, "SOURCE_RECORD_NOT_PASS")


def test_the_registry_names_the_blocker_the_workflow_writes() -> None:
    check = GH.github_checks["dependency review"]  # type: ignore[union-attr]
    workflow = (ROOT / check.workflow).read_text(encoding="utf-8")
    assert check.blocker_annotation is not None and f"title={check.blocker_annotation}::" in workflow
    for name, spec in GH.github_checks.items():  # type: ignore[union-attr]
        text = (ROOT / spec.workflow).read_text(encoding="utf-8")
        assert f"name: {name.split(' (')[0]}" in text or f"name: {name}" in text, name
        for step in spec.required_steps:
            assert f"- name: {step}" in text, (name, step)


# --- the collector --------------------------------------------------------------------------------------------------


def _fake_api(bundle: GithubBundle) -> Any:
    record = bundle.record()

    def fetch(endpoint: str) -> Any:
        if "/commits/" in endpoint:
            return record["check_runs"]
        if "/actions/jobs/" in endpoint:
            return record["jobs"][endpoint.rsplit("/", 1)[-1]]
        if "/actions/runs/" in endpoint:
            return record["workflow_runs"][endpoint.rsplit("/", 1)[-1]]
        return record["annotations"][endpoint.split("/check-runs/")[1].split("/")[0]]

    return fetch


def test_the_collector_writes_the_record_the_reader_accepts(tmp_path: Path) -> None:
    collector = _module("collect_gh", "scripts/release/collect_github_actions_evidence.py")
    record = collector.collect("Jimmy0307/Pados", SHA, fetch=_fake_api(GithubBundle(GH, SHA)))  # type: ignore[arg-type]
    assert read_source(GH, _write(tmp_path / "gh.json", record), commit=SHA).exit_code == 0  # type: ignore[arg-type]


def test_the_collector_refuses_partial_pages() -> None:
    collector = _module("collect_gh_pages", "scripts/release/collect_github_actions_evidence.py")
    bundle = GithubBundle(GH, SHA)  # type: ignore[arg-type]
    fetch = _fake_api(bundle)
    with pytest.raises(SystemExit, match="DO_NOT_FIT_ONE_PAGE"):
        collector.collect("Jimmy0307/Pados", SHA, fetch=lambda e: {**fetch(e), "total_count": 500}
                          if "/commits/" in e else fetch(e))
    with pytest.raises(SystemExit, match="ANNOTATIONS_DO_NOT_FIT_ONE_PAGE"):
        collector.collect("Jimmy0307/Pados", SHA, fetch=lambda e: [{}] * 100 if "/annotations" in e else fetch(e))
    with pytest.raises(SystemExit, match="BAD_ARGUMENTS"):
        collector.collect("Jimmy0307/Pados; rm", SHA, fetch=fetch)
