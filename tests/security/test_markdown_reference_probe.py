# SPDX-License-Identifier: AGPL-3.0-only
"""The registered markdown_reference_check command: a real, read-only probe whose declared outcome is derived from
what it checked. Native success alone is never semantic completion; a broken or escaping reference fails closed."""
from __future__ import annotations

import json
from pathlib import Path

from pados.loop.amendments import normalize_p6_10_capability_outcome
from pados.work_protocol.models import CoverageStatus
from pados.worker.command import COMMANDS, run_registered_command
from pados.worker.schemas import CommandInvocation

CONTRACT = {"pados_capability_outcome": "pados.capability-outcome.v1"}


def _run(root: Path) -> tuple[dict[str, object], CoverageStatus]:
    result = run_registered_command(
        CommandInvocation(command_id="markdown_reference_check", working_directory=root, timeout_seconds=60,
                          environment={"PYTHONPATH": str(root / "src")}),
        project_root=root,
    )
    outcome = normalize_p6_10_capability_outcome(
        CONTRACT, str(result["stdout"]).encode("utf-8"),
        execution_succeeded=result["status"] == "SUCCEEDED", execution_status=str(result["status"]),
    )
    return result, outcome


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("See [guide](docs/guide.md) and [site](https://example.org).\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("Back to [readme](../README.md#top) and [here](#local).\n",
                                            encoding="utf-8")
    return root


def test_command_is_registered_low_risk_without_arguments() -> None:
    spec = COMMANDS["markdown_reference_check"]
    assert spec.risk_level == "LOW" and spec.allowed_args == set()
    assert spec.arguments[0] == "-I"  # isolated: the project's own modules and PYTHON* variables are never loaded


def test_all_references_resolve_is_complete_and_stdout_is_exactly_the_contract(tmp_path: Path) -> None:
    result, outcome = _run(_project(tmp_path))
    assert result["status"] == "SUCCEEDED"
    assert json.loads(str(result["stdout"])) == {"schema_version": "pados.capability-outcome.v1",
                                                 "outcome": "COMPLETE"}
    assert outcome is CoverageStatus.COMPLETE
    report = json.loads(str(result["stderr"]))
    assert report["files_checked"] == 2 and report["references_checked"] == 2 and report["broken"] == []


def test_broken_reference_is_unresolved(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "docs" / "guide.md").write_text("Missing [x](missing.md).\n", encoding="utf-8")
    result, outcome = _run(root)
    assert outcome is CoverageStatus.UNRESOLVED
    assert json.loads(str(result["stderr"]))["broken"] == [{"file": "docs/guide.md", "target": "missing.md"}]


def test_reference_escaping_the_project_root_is_unresolved(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (tmp_path / "outside.md").write_text("outside\n", encoding="utf-8")
    (root / "README.md").write_text("[out](../outside.md)\n", encoding="utf-8")
    _, outcome = _run(root)
    assert outcome is CoverageStatus.UNRESOLVED


def test_no_markdown_is_unresolved_not_vacuously_complete(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    (root / "notes.txt").write_text("not markdown\n", encoding="utf-8")
    _, outcome = _run(root)
    assert outcome is CoverageStatus.UNRESOLVED


def test_project_modules_cannot_hijack_the_probe(tmp_path: Path) -> None:
    root = _project(tmp_path)
    for shadow in (root / "json.py", root / "src" / "json.py"):
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_text("raise SystemExit('project code ran')\n", encoding="utf-8")
    result, outcome = _run(root)
    assert result["status"] == "SUCCEEDED" and outcome is CoverageStatus.COMPLETE


def test_markdown_without_any_checkable_reference_is_unresolved_not_vacuously_complete(tmp_path: Path) -> None:
    root = tmp_path / "linkless"
    root.mkdir()
    (root / "README.md").write_text("No links. [external](https://example.org) and [anchor](#x) only.\n"
                                    "```\n[fenced](missing.md)\n```\n", encoding="utf-8")
    result, outcome = _run(root)
    assert outcome is CoverageStatus.UNRESOLVED
    assert json.loads(str(result["stderr"]))["references_checked"] == 0


def test_a_malformed_target_is_reported_not_a_crash(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "docs" / "guide.md").write_text("Bad [x](a%00b.md).\n", encoding="utf-8")
    result, outcome = _run(root)
    assert result["status"] == "SUCCEEDED" and outcome is CoverageStatus.UNRESOLVED
