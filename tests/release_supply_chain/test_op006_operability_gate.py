# SPDX-License-Identifier: AGPL-3.0-only
"""OP-006: the CI operability gate fails closed on unresolved references and never reports a gap as complete."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pados.release.operability_gate import Registry, evaluate, lane_known_status, load

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "scripts" / "release" / "operability_contracts.v1.json"
LANES = ROOT / "scripts" / "release" / "runtime_lanes.v1.json"


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pados").mkdir(parents=True)
    (tmp_path / "src" / "pados" / "codes.py").write_text('CODE = "WIDGET_OFFLINE"\n', encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "widget.md").write_text("# Widget\n\n## Recovery\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_widget.py").write_text("def test_widget_offline():\n    pass\n", encoding="utf-8")
    return tmp_path


def _registry(**component: Any) -> Registry:
    base: dict[str, Any] = {
        "name": "Widget",
        "contract": {"path": "docs/widget.md", "heading": "Widget"},
        "error_codes": ["WIDGET_OFFLINE"],
        "health": {"not_applicable": "a library without a serving endpoint"},
        "failure_injection": [{"path": "tests/test_widget.py", "names": ["test_widget_offline"]}],
        "runbook": {"path": "docs/widget.md", "heading": "Recovery"},
    }
    base.update(component)
    return Registry.model_validate({"schema_version": "pados.operability-contracts.v1", "components": {"W": base}})


def test_a_fully_resolved_component_is_complete(tmp_path: Path) -> None:
    report = evaluate(_tree(tmp_path), _registry())
    assert (report.status, report.components[0].status) == ("PASS", "COMPLETE")
    assert report.semantic_authority == "NONE"


@pytest.mark.parametrize(("change", "finding"), [
    ({"contract": {"path": "docs/absent.md", "heading": "Widget"}}, "CONTRACT_DOC_MISSING"),
    ({"runbook": {"path": "docs/widget.md", "heading": "Rollback"}}, "RUNBOOK_SECTION_MISSING"),
    ({"error_codes": ["WIDGET_MELTED"]}, "ERROR_CODE_NOT_IN_SOURCE"),
    ({"failure_injection": [{"path": "tests/test_widget.py", "names": ["test_widget_melts"]}]},
     "FAILURE_INJECTION_TEST_MISSING"),
    ({"failure_injection": [{"path": "tests/test_absent.py"}]}, "FAILURE_INJECTION_TESTS_MISSING"),
    ({"health": {"endpoint": "/health/widget", "source": "src/pados/codes.py"}}, "HEALTH_ENDPOINT_MISSING"),
])
def test_an_unresolved_reference_fails_the_gate(tmp_path: Path, change: dict[str, Any], finding: str) -> None:
    report = evaluate(_tree(tmp_path), _registry(**change))
    assert report.status == "FAIL" and report.components[0].status == "FAIL"
    assert any(item.startswith(finding) for item in report.components[0].findings)


def test_an_explicit_gap_is_listed_and_never_complete(tmp_path: Path) -> None:
    report = evaluate(_tree(tmp_path), _registry(runbook={"gap": "no runbook yet for this component"}))
    assert report.status == "PASS" and report.components[0].status == "INCOMPLETE"
    assert report.components[0].gaps == ("runbook: no runbook yet for this component",)


@pytest.mark.parametrize("bad", [{"gap": "short"}, {"not_applicable": "short"}])
def test_a_gap_or_not_applicable_needs_a_reason(bad: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _registry(health=bad)


def test_error_codes_must_be_stable_upper_snake_case() -> None:
    with pytest.raises(ValueError):
        _registry(error_codes=["widget offline"])


def test_runtime_lanes_are_reported_separately_and_unknown_lanes_fail(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    report = evaluate(root, _registry(runtime_lanes=["rea-runtime"]), {"rea-runtime": "NOT_VALIDATED"})
    assert report.components[0].status == "COMPLETE"  # the contract resolves; the lane status sits beside it
    assert report.components[0].runtime_lanes == {"rea-runtime": "NOT_VALIDATED"}
    unknown = evaluate(root, _registry(runtime_lanes=["no-such-lane"]), {"rea-runtime": "NOT_VALIDATED"})
    assert unknown.status == "FAIL"


def test_the_repository_registry_passes_and_covers_every_audited_component() -> None:
    registry = load(CONTRACTS)
    assert set(registry.components) == {f"C{n:02d}" for n in range(1, 18)}
    report = evaluate(ROOT, registry, lane_known_status(LANES))
    assert report.status == "PASS", [c.findings for c in report.components if c.findings]
    c06 = next(c for c in report.components if c.component_id == "C06")
    assert c06.runtime_lanes["rea-runtime"] == "NOT_VALIDATED"
    c16 = next(c for c in report.components if c.component_id == "C16")
    assert c16.runtime_lanes["github-ci"] == "SEE_RUNTIME_LANE_EVIDENCE"  # never a guessed PASS


def test_the_cli_fails_closed_on_a_malformed_registry(tmp_path: Path) -> None:
    bad = tmp_path / "contracts.json"
    bad.write_text(json.dumps({"schema_version": "pados.operability-contracts.v1", "components": {"X": {}}}),
                   encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "release" / "operability_gate.py"),
                             str(tmp_path / "out.json"), "--contracts", str(bad)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2 and "OPERABILITY_CONTRACTS_INVALID" in result.stdout
