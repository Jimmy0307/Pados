# SPDX-License-Identifier: AGPL-3.0-only
"""CI operability gate (OP-006): every audited component's operability contract resolves, or is an explicit gap.

For each component of the operability audit the registry (``scripts/release/operability_contracts.v1.json``) names:
* a contract record (document path and the heading that holds it);
* stable error codes, which must occur in the source;
* health, failure-injection tests and a runbook section.

Each field is either a reference, which must resolve, or an explicit ``gap`` / ``not_applicable`` with a reason.

* A reference that does not resolve is a finding, and the gate fails closed.
* A ``gap`` keeps the component ``INCOMPLETE`` and is listed; it is never reported as complete.
* Runtime lanes that are not validated are reported beside the component, separately from its contract status.

The gate reads files only. It runs nothing and grants no approval, acceptance or release decision.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA = "pados.operability-contracts.v1"
_CODE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
# Code files only (see _source_text): the contracts JSON must never satisfy its own error codes.
_SOURCE_ROOTS = ("src/pados", "packages/cli/lib", "web/src", "scripts/release")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Gap(_Frozen):
    gap: str = Field(min_length=10)


class NotApplicable(_Frozen):
    not_applicable: str = Field(min_length=10)


class DocRef(_Frozen):
    path: str
    heading: str = Field(min_length=2)


class TestRef(_Frozen):
    path: str
    names: tuple[str, ...] = ()


class HealthRef(_Frozen):
    endpoint: str = Field(pattern=r"^/")
    source: str


class Component(_Frozen):
    name: str
    contract: DocRef | Gap
    error_codes: tuple[str, ...] | Gap
    health: HealthRef | NotApplicable | Gap
    failure_injection: tuple[TestRef, ...] | NotApplicable | Gap
    runbook: DocRef | Gap
    runtime_lanes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _codes_are_stable(self) -> Component:
        if isinstance(self.error_codes, tuple):
            bad = [code for code in self.error_codes if not _CODE.fullmatch(code)]
            if bad or not self.error_codes:
                raise ValueError(f"ERROR_CODES_INVALID {bad}")
        return self


class Registry(_Frozen):
    schema_version: Literal["pados.operability-contracts.v1"]
    components: dict[str, Component]


class ComponentReport(_Frozen):
    component_id: str
    name: str
    status: Literal["COMPLETE", "INCOMPLETE", "FAIL"]
    findings: tuple[str, ...]
    gaps: tuple[str, ...]
    runtime_lanes: dict[str, str]


class GateReport(_Frozen):
    schema_version: Literal["pados.operability-gate.v1"] = "pados.operability-gate.v1"
    status: Literal["PASS", "FAIL"]
    components: tuple[ComponentReport, ...]
    complete: int
    incomplete: int
    failed: int
    semantic_authority: Literal["NONE"] = "NONE"


def load(path: Path) -> Registry:
    try:
        return Registry.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError(f"OPERABILITY_CONTRACTS_INVALID: {exc}") from exc


def _source_text(root: Path) -> str:
    parts: list[str] = []
    for base in _SOURCE_ROOTS:
        directory = root / base
        if directory.is_dir():
            parts.extend(path.read_text(encoding="utf-8", errors="replace")
                         for path in sorted(directory.rglob("*")) if path.suffix in {".py", ".js", ".ts", ".tsx"})
    return "\n".join(parts)


def _heading_present(path: Path, heading: str) -> bool:
    return any(line.lstrip("#").strip() == heading for line in path.read_text(encoding="utf-8").splitlines()
               if line.startswith("#"))


def _check_doc(root: Path, ref: DocRef, label: str) -> Iterable[str]:
    path = root / ref.path
    if not path.is_file():
        yield f"{label}_DOC_MISSING {ref.path}"
    elif not _heading_present(path, ref.heading):
        yield f"{label}_SECTION_MISSING {ref.path}#{ref.heading}"


def _check_tests(root: Path, refs: tuple[TestRef, ...]) -> Iterable[str]:
    for ref in refs:
        path = root / ref.path
        if path.is_dir():
            if not any(path.rglob("test_*.py")):
                yield f"FAILURE_INJECTION_TESTS_MISSING {ref.path}"
            continue
        if not path.is_file():
            yield f"FAILURE_INJECTION_TESTS_MISSING {ref.path}"
            continue
        text = path.read_text(encoding="utf-8")
        for name in ref.names:
            if not re.search(rf"\bdef {re.escape(name)}\b|\b{re.escape(name)}\(", text):
                yield f"FAILURE_INJECTION_TEST_MISSING {ref.path}::{name}"


def evaluate(root: Path, registry: Registry, lane_status: dict[str, str] | None = None) -> GateReport:
    source = _source_text(root)
    lanes = lane_status or {}
    reports: list[ComponentReport] = []
    for component_id in sorted(registry.components):
        component = registry.components[component_id]
        findings: list[str] = []
        gaps: list[str] = []
        for field, value in (("contract", component.contract), ("error_codes", component.error_codes),
                             ("health", component.health), ("failure_injection", component.failure_injection),
                             ("runbook", component.runbook)):
            if isinstance(value, Gap):
                gaps.append(f"{field}: {value.gap}")
        if isinstance(component.contract, DocRef):
            findings.extend(_check_doc(root, component.contract, "CONTRACT"))
        if isinstance(component.runbook, DocRef):
            findings.extend(_check_doc(root, component.runbook, "RUNBOOK"))
        if isinstance(component.error_codes, tuple):
            findings.extend(f"ERROR_CODE_NOT_IN_SOURCE {code}" for code in component.error_codes
                            if not re.search(rf"\b{code}\b", source))
        if isinstance(component.health, HealthRef):
            health_source = root / component.health.source
            if not health_source.is_file() or component.health.endpoint not in health_source.read_text(
                    encoding="utf-8"):
                findings.append(f"HEALTH_ENDPOINT_MISSING {component.health.endpoint}")
        if isinstance(component.failure_injection, tuple):
            findings.extend(_check_tests(root, component.failure_injection))
        unknown_lanes = [lane for lane in component.runtime_lanes if lanes and lane not in lanes]
        findings.extend(f"RUNTIME_LANE_UNKNOWN {lane}" for lane in unknown_lanes)
        status: Literal["COMPLETE", "INCOMPLETE", "FAIL"] = (
            "FAIL" if findings else "INCOMPLETE" if gaps else "COMPLETE")
        reports.append(ComponentReport(component_id=component_id, name=component.name, status=status,
                                       findings=tuple(findings), gaps=tuple(gaps),
                                       runtime_lanes={lane: lanes.get(lane, "UNKNOWN")
                                                      for lane in component.runtime_lanes}))
    failed = sum(report.status == "FAIL" for report in reports)
    return GateReport(status="FAIL" if failed else "PASS", components=tuple(reports),
                      complete=sum(report.status == "COMPLETE" for report in reports),
                      incomplete=sum(report.status == "INCOMPLETE" for report in reports), failed=failed)


def lane_known_status(registry_path: Path) -> dict[str, str]:
    """Each runtime lane's documented status (NOT_VALIDATED, FAIL, …) for display beside its components.

    A lane without a documented status is proven per commit by the runtime lane gate; this gate never reads that
    evidence, so it says where the status lives instead of guessing one.
    """
    data: dict[str, Any] = json.loads(registry_path.read_text(encoding="utf-8"))
    return {lane: str(spec.get("known_status") or "SEE_RUNTIME_LANE_EVIDENCE")
            for lane, spec in data.get("lanes", {}).items()}
