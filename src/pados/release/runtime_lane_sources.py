# SPDX-License-Identifier: AGPL-3.0-only
"""Read a lane's machine-written runner record (OP-002) under the rules the lane registry fixes for that lane.

The registry, not the evidence index or its spec, decides for each lane which record format counts and which checks
that record must contain. ``read_source`` is the one interpreter of those records; the index builder and the gate
both call it, so a PASS written into an index by hand never outranks what the record itself says.

Formats:

* ``gate-summary``: the Windows gate runner summary (``repo_head``, ``gates.<name>.exit_code``, ``finished``).
* ``vm-summary``: the VMware chain summary (``sha``, ``virt == "vmware"``, ``e2e``, ``gates.<name>``, ``finished``).
* ``e2e-result``: ``E2E_RESULT.json`` from ``local_dogfood_e2e.py`` (``verdict``, ``commit``, ``platform``,
  ``steps[].status``, optional ``finished``).
* ``postgres-gate``: ``postgres-integration.json`` from ``postgres_integration_gate.py`` (schema v1): the commit,
  platform, finish time and, per suite, its status and the SHA-256 of its ``regression_gate`` report. Each suite
  report beside it is re-hashed and re-read (same commit, PASS, no skips, no problems).
* ``github-check-runs``: the GitHub REST response for ``GET /repos/{owner}/{repo}/commits/{sha}/check-runs`` saved
  unmodified. Every run must belong to the registry's repository (its ``url``), every required check name must have
  a completed run by the expected app (slug and numeric id) on this commit, and every run of that name must have
  concluded ``success``. A truncated page (``total_count`` larger than the list) is refused.

Every format must carry its own finish time (``finished``, or ``completed_at`` per check run); the file's
modification time never stands in for it, so copying or touching a record cannot make it fresh. A gate summary must
name its platform. The PostgreSQL suites must each report at least the registry's minimum number of passed tests.

What this proves is that the bytes the index points at say PASS for this commit, platform and complete check set.
It does not authenticate who wrote the bytes; content truth stays with review and the existing acceptance paths.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceFormat = Literal["gate-summary", "vm-summary", "e2e-result", "postgres-gate", "github-check-runs"]
POSTGRES_GATE_SCHEMA = "pados.postgres-integration.v1"
_FORMAT_PLATFORMS: dict[str, frozenset[str]] = {
    "gate-summary": frozenset({"windows"}),
    "vm-summary": frozenset({"linux"}),
    "e2e-result": frozenset({"windows", "linux", "macos"}),
    "postgres-gate": frozenset({"any", "windows", "linux"}),
    "github-check-runs": frozenset({"github"}),
}
# Reasons that mean "this record does not speak for this commit or is not finished", not "it failed".
NOT_VALIDATED_CODES = frozenset({"SOURCE_COMMIT_MISMATCH", "SOURCE_CHECK_INCOMPLETE"})


class LaneSource(BaseModel):
    """The registry's binding of a lane to one record format and its complete set of required checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: SourceFormat
    required_checks: tuple[str, ...] = Field(default=(), max_length=64)
    # github-check-runs only: the repository and the GitHub App (slug and numeric id) every required run must have.
    app: str | None = Field(default=None, max_length=80)
    app_id: int | None = None
    repository: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    # postgres-gate only: the minimum passed tests per suite, rechecked by the reader (the runner enforces it too).
    min_passed: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks_fit_the_format(self) -> LaneSource:
        if self.format == "e2e-result":
            if self.required_checks:
                raise ValueError("E2E_RESULT_SOURCE_HAS_NO_NAMED_CHECKS")
        elif not self.required_checks:
            raise ValueError(f"SOURCE_NEEDS_REQUIRED_CHECKS {self.format}")
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("SOURCE_REQUIRED_CHECKS_REPEAT")
        github = self.format == "github-check-runs"
        if any((value is not None) != github for value in (self.app, self.app_id, self.repository)):
            raise ValueError("SOURCE_APP_AND_REPOSITORY_ARE_FOR_GITHUB_CHECK_RUNS_ONLY")
        if self.min_passed and (self.format != "postgres-gate" or set(self.min_passed) - set(self.required_checks)
                                or min(self.min_passed.values()) < 1):
            raise ValueError("SOURCE_MIN_PASSED_INVALID")
        return self

    def fits_platform(self, platform: str) -> bool:
        return platform in _FORMAT_PLATFORMS[self.format]


class SourceError(ValueError):
    """The record cannot back the lane; ``code`` is a stable reason such as ``SOURCE_COMMIT_MISMATCH``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code} {detail}".strip())
        self.code = code


@dataclass(frozen=True)
class SourceReading:
    exit_code: int        # 0 only when every required check passed
    finished: datetime    # when the runner finished (staleness counts from here)
    platform: str         # windows | linux | macos | github


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_family(value: object) -> str:
    text = str(value or "").lower()
    if "microsoft" in text or "wsl" in text:
        return "linux-wsl"  # a WSL kernel is never the Linux lane
    for family, prefixes in (("windows", ("windows", "win32", "nt")), ("linux", ("linux",)),
                             ("macos", ("macos", "darwin"))):
        if text.startswith(prefixes):
            return family
    return text or "unknown"


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _finished(record: Mapping[str, Any]) -> datetime:
    moment = _aware(record.get("finished"))
    if moment is None:
        raise SourceError("SOURCE_FORMAT_INVALID", "FINISHED_MISSING")
    return moment


def _load(path: Path) -> Mapping[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SourceError("SOURCE_UNREADABLE") from exc
    if not isinstance(record, Mapping):
        raise SourceError("SOURCE_FORMAT_INVALID")
    return record


def _exit_of(entry: object) -> int | None:
    code = entry.get("exit_code") if isinstance(entry, Mapping) else entry
    return None if isinstance(code, bool) or not isinstance(code, int) else code


def _first_failure(codes: Mapping[str, object], required: Sequence[str]) -> int:
    exits: list[int] = []
    for name in required:
        code = _exit_of(codes.get(name))
        if code is None:
            raise SourceError("SOURCE_GATE_MISSING", name)
        exits.append(code)
    return next((code for code in exits if code != 0), 0)


def _expect_commit(recorded: object, commit: str) -> None:
    if recorded != commit:
        raise SourceError("SOURCE_COMMIT_MISMATCH")


def _gate_summary(source: LaneSource, record: Mapping[str, Any], path: Path, commit: str) -> SourceReading:
    gates = record.get("gates")
    if not isinstance(record.get("repo_head"), str) or not isinstance(gates, Mapping) or not all(
            isinstance(entry, Mapping) for entry in gates.values()):
        raise SourceError("SOURCE_FORMAT_INVALID")
    _expect_commit(record["repo_head"], commit)
    if not isinstance(record.get("platform"), str) or not record["platform"].strip():
        raise SourceError("SOURCE_FORMAT_INVALID", "PLATFORM_MISSING")
    platform = _platform_family(record["platform"])
    return SourceReading(_first_failure(gates, source.required_checks), _finished(record), platform)


def _vm_summary(source: LaneSource, record: Mapping[str, Any], path: Path, commit: str) -> SourceReading:
    gates = record.get("gates")
    if not isinstance(record.get("sha"), str) or not isinstance(gates, Mapping):
        raise SourceError("SOURCE_FORMAT_INVALID")
    _expect_commit(record["sha"], commit)
    platform = "linux" if record.get("virt") == "vmware" else f"virt:{record.get('virt')}"
    available = {**gates, "e2e": record["e2e"]} if "e2e" in record else dict(gates)
    return SourceReading(_first_failure(available, source.required_checks), _finished(record), platform)


def _e2e_result(record: Mapping[str, Any], path: Path, commit: str) -> SourceReading:
    steps = record.get("steps")
    if not isinstance(record.get("verdict"), str) or not isinstance(steps, list) or not all(
            isinstance(step, Mapping) for step in steps):
        raise SourceError("SOURCE_FORMAT_INVALID")
    _expect_commit(record.get("commit"), commit)
    passed = record["verdict"] == "PASS" and bool(steps) and all(step.get("status") == "PASS" for step in steps)
    return SourceReading(0 if passed else 1, _finished(record), _platform_family(record.get("platform")))


def _suite_report(directory: Path, suite: str, entry: object, commit: str, minimum: int) -> bool:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("sha256"), str):
        raise SourceError("SOURCE_FORMAT_INVALID", suite)
    report_path = directory / f"postgres-{suite}.json"
    if not report_path.is_file():
        raise SourceError("SOURCE_SUITE_REPORT_MISSING", suite)
    if sha256_file(report_path) != entry["sha256"]:
        raise SourceError("SOURCE_SUITE_REPORT_HASH_MISMATCH", suite)
    report = _load(report_path)
    _expect_commit(report.get("source_commit"), commit)
    counts = report.get("counts")
    passed = counts.get("passed") if isinstance(counts, Mapping) else None
    return (entry.get("status") == "PASS" and report.get("status") == "PASS" and report.get("skipped") == []
            and report.get("problems") == [] and isinstance(passed, int) and not isinstance(passed, bool)
            and passed >= max(1, minimum))


def _postgres_gate(source: LaneSource, record: Mapping[str, Any], path: Path, commit: str) -> SourceReading:
    suites = record.get("suite_reports")
    if record.get("schema_version") != POSTGRES_GATE_SCHEMA or not isinstance(suites, Mapping):
        raise SourceError("SOURCE_FORMAT_INVALID")
    _expect_commit(record.get("source_commit"), commit)
    missing = [suite for suite in source.required_checks if suite not in suites]
    if missing:
        raise SourceError("SOURCE_GATE_MISSING", ",".join(missing))
    passed = all([_suite_report(path.parent, suite, suites[suite], commit, source.min_passed.get(suite, 1))
                  for suite in source.required_checks])
    return SourceReading(0 if passed else 1, _finished(record), _platform_family(record.get("platform")))


def _github_check_runs(source: LaneSource, record: Mapping[str, Any], commit: str) -> SourceReading:
    runs = record.get("check_runs")
    if not isinstance(runs, list) or not all(isinstance(run, Mapping) for run in runs) or not isinstance(
            record.get("total_count"), int):
        raise SourceError("SOURCE_FORMAT_INVALID")
    if record["total_count"] != len(runs):
        raise SourceError("SOURCE_FORMAT_INVALID", "TRUNCATED_PAGE")
    if any(run.get("head_sha") != commit for run in runs):
        raise SourceError("SOURCE_COMMIT_MISMATCH")
    prefix = f"https://api.github.com/repos/{source.repository}/check-runs/"
    if any(not str(run.get("url", "")).startswith(prefix) for run in runs):
        raise SourceError("SOURCE_REPOSITORY_MISMATCH")  # a fork shares the commit, not the repository
    finished: list[datetime] = []
    failed = False
    for name in source.required_checks:
        named = [run for run in runs if run.get("name") == name
                 and isinstance(run.get("app"), Mapping) and run["app"].get("slug") == source.app
                 and run["app"].get("id") == source.app_id]
        if not named:
            raise SourceError("SOURCE_GATE_MISSING", name)
        if any(run.get("status") != "completed" for run in named):
            raise SourceError("SOURCE_CHECK_INCOMPLETE", name)
        failed = failed or any(run.get("conclusion") != "success" for run in named)
        moments = [_aware(run.get("completed_at")) for run in named]
        if any(moment is None for moment in moments):
            raise SourceError("SOURCE_FORMAT_INVALID", name)
        finished.extend(moment for moment in moments if moment is not None)
    return SourceReading(1 if failed else 0, max(finished), "github")


def read_source(source: LaneSource, path: Path, *, commit: str) -> SourceReading:
    """Interpret ``path`` as the lane's runner record for ``commit``; raises ``SourceError`` when it cannot."""
    record = _load(path)
    if source.format == "gate-summary":
        return _gate_summary(source, record, path, commit)
    if source.format == "vm-summary":
        return _vm_summary(source, record, path, commit)
    if source.format == "e2e-result":
        return _e2e_result(record, path, commit)
    if source.format == "postgres-gate":
        return _postgres_gate(source, record, path, commit)
    return _github_check_runs(source, record, commit)
