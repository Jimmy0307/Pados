# SPDX-License-Identifier: AGPL-3.0-only
"""Read a lane's machine-written runner record (OP-002) under the rules the lane registry fixes for that lane.

The registry, not the evidence index or its spec, decides for each lane which record format counts and which checks
that record must contain. ``read_source`` is the one interpreter of those records; the index builder and the gate
both call it, so a PASS written into an index by hand never outranks what the record itself says.

Formats:

* ``gate-summary``: the Windows gate runner summary (``repo_head``, ``gates.<name>.exit_code``, ``finished``).
* ``vm-summary``: the VMware chain summary (``sha``, ``virt == "vmware"``, ``e2e``, ``gates.<name>``, ``finished``).
* ``e2e-result``: ``E2E_RESULT.json`` from ``local_dogfood_e2e.py`` (runner contract
  ``pados.local-dogfood-e2e.v1``: ``verdict``, ``commit``, ``platform``, ``run_mode``, ``finished``, ``steps``). The
  registry lists the complete set of required steps: each must appear exactly once and PASS, no other step may
  appear, and a run in a reduced mode (``--skip-acceptance-suite``) never speaks for the full lane.
* ``postgres-gate``: ``postgres-integration.json`` from ``postgres_integration_gate.py`` (schema v1): the commit,
  platform, finish time and, per suite, its status and the SHA-256 of its ``regression_gate`` report. Each suite
  report beside it is re-hashed and re-read: same commit, the suite's own gate name (``postgres-<suite>``), exactly
  the registry's test paths for that suite with none missing or empty, PASS, no skips, no problems, and at least the
  registry's minimum of passed tests.
* ``github-actions``: ``collect_github_actions_evidence.py`` output (schema ``pados.github-actions-evidence.v1``),
  which keeps four GitHub REST responses unmodified: the commit's check runs, the Actions job behind each check run,
  the workflow run behind each job, and each check run's annotations. Read by ``runtime_lane_github``.

Every format must carry its own finish time (``finished``, or ``completed_at`` per check run); the file's
modification time never stands in for it, so copying or touching a record cannot make it fresh. A gate summary must
name its platform.

What this proves is that the bytes the index points at say PASS for this commit, platform and complete check set.
A SHA-256 proves the bytes are the same bytes; it does not authenticate who wrote them or prove that what they say
happened. Content truth stays with review and the existing approval, acceptance and CAS paths.
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

SourceFormat = Literal["gate-summary", "vm-summary", "e2e-result", "postgres-gate", "github-actions"]
POSTGRES_GATE_SCHEMA = "pados.postgres-integration.v1"
E2E_RUNNER_CONTRACT = "pados.local-dogfood-e2e.v1"
E2E_FULL_MODE = "full"
_FORMAT_PLATFORMS: dict[str, frozenset[str]] = {
    "gate-summary": frozenset({"windows"}),
    "vm-summary": frozenset({"linux"}),
    "e2e-result": frozenset({"windows", "linux", "macos"}),
    "postgres-gate": frozenset({"any", "windows", "linux"}),
    "github-actions": frozenset({"github"}),
}
# Reasons that mean "this record does not speak for this lane, commit or check", not "it failed": another commit, an
# unfinished check, a reduced run, or a required check that did not actually execute (an environment blocker).
NOT_VALIDATED_CODES = frozenset({"SOURCE_COMMIT_MISMATCH", "SOURCE_CHECK_INCOMPLETE", "SOURCE_RUN_REDUCED",
                                 "SOURCE_CHECK_NOT_EXECUTED", "SOURCE_CHECK_ENVIRONMENT_BLOCKED"})
_WORKFLOW_PATH = r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$"


class GithubCheck(BaseModel):
    """Who must have produced one required check run: the workflow file, and the steps that must have executed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: str = Field(pattern=_WORKFLOW_PATH)
    required_steps: tuple[str, ...] = Field(default=(), max_length=32)
    # An annotation title the job writes when its tool could not run (for example a disabled dependency graph). Its
    # presence makes the check NOT_VALIDATED even though the job concluded success.
    blocker_annotation: str | None = Field(default=None, min_length=8, max_length=120)


class LaneSource(BaseModel):
    """The registry's binding of a lane to one record format and its complete set of required checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: SourceFormat
    # The complete set of checks the record must contain: gate names, E2E steps, PostgreSQL suites or check names.
    required_checks: tuple[str, ...] = Field(min_length=1, max_length=64)
    # github-actions only: the repository and the GitHub App (slug and numeric id) every required run must have,
    # and the workflow identity of each required check.
    app: str | None = Field(default=None, max_length=80)
    app_id: int | None = None
    repository: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    github_checks: dict[str, GithubCheck] = Field(default_factory=dict)
    # postgres-gate only: the minimum passed tests and the exact test paths per suite (the runner's own table).
    min_passed: dict[str, int] = Field(default_factory=dict)
    suite_paths: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks_fit_the_format(self) -> LaneSource:
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("SOURCE_REQUIRED_CHECKS_REPEAT")
        github = self.format == "github-actions"
        if any((value is not None) != github for value in (self.app, self.app_id, self.repository)):
            raise ValueError("SOURCE_APP_AND_REPOSITORY_ARE_FOR_GITHUB_ACTIONS_ONLY")
        if github and set(self.github_checks) != set(self.required_checks):
            raise ValueError("SOURCE_GITHUB_CHECKS_MUST_NAME_EVERY_REQUIRED_CHECK")
        if self.github_checks and not github:
            raise ValueError("SOURCE_GITHUB_CHECKS_ARE_FOR_GITHUB_ACTIONS_ONLY")
        postgres = self.format == "postgres-gate"
        if (self.min_passed or self.suite_paths) and not postgres:
            raise ValueError("SOURCE_SUITE_FIELDS_ARE_FOR_POSTGRES_GATE_ONLY")
        if postgres and (set(self.min_passed) != set(self.required_checks) or min(self.min_passed.values()) < 1):
            raise ValueError("SOURCE_MIN_PASSED_INVALID")
        if postgres and (set(self.suite_paths) != set(self.required_checks)
                         or not all(self.suite_paths.values())):
            raise ValueError("SOURCE_SUITE_PATHS_INVALID")
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
    finished: datetime    # when the oldest required part finished (staleness counts from here)
    platform: str         # windows | linux | macos | github
    newest: datetime | None = None  # when the newest required part finished, if it differs (future-time check)


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


def aware_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _finished(record: Mapping[str, Any]) -> datetime:
    moment = aware_time(record.get("finished"))
    if moment is None:
        raise SourceError("SOURCE_FORMAT_INVALID", "FINISHED_MISSING")
    return moment


def load_record(path: Path) -> Mapping[str, Any]:
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


def _e2e_result(source: LaneSource, record: Mapping[str, Any], path: Path, commit: str) -> SourceReading:
    steps = record.get("steps")
    if not isinstance(record.get("verdict"), str) or not isinstance(steps, list) or not all(
            isinstance(step, Mapping) and isinstance(step.get("step"), str) for step in steps):
        raise SourceError("SOURCE_FORMAT_INVALID")
    if record.get("runner_contract") != E2E_RUNNER_CONTRACT:
        raise SourceError("SOURCE_FORMAT_INVALID", "RUNNER_CONTRACT")
    _expect_commit(record.get("commit"), commit)
    finished = _finished(record)
    platform = _platform_family(record.get("platform"))
    if record["verdict"] != "PASS" or any(step.get("status") != "PASS" for step in steps):
        # A failure stays a failure, whatever mode it ran in and whatever steps it recorded.
        return SourceReading(1, finished, platform)
    if record.get("run_mode") != E2E_FULL_MODE:
        raise SourceError("SOURCE_RUN_REDUCED", str(record.get("run_mode"))[:40])
    names = [step["step"] for step in steps]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise SourceError("SOURCE_STEP_REPEATED", ",".join(repeated)[:200])
    missing = [name for name in source.required_checks if name not in names]
    if missing:
        raise SourceError("SOURCE_GATE_MISSING", ",".join(missing)[:200])
    unexpected = [name for name in names if name not in source.required_checks]
    if unexpected:
        raise SourceError("SOURCE_STEP_UNEXPECTED", ",".join(unexpected)[:200])
    return SourceReading(0, finished, platform)


def _suite_report(source: LaneSource, directory: Path, suite: str, entry: object, commit: str) -> bool:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("sha256"), str):
        raise SourceError("SOURCE_FORMAT_INVALID", suite)
    report_path = directory / f"postgres-{suite}.json"
    if not report_path.is_file():
        raise SourceError("SOURCE_SUITE_REPORT_MISSING", suite)
    if sha256_file(report_path) != entry["sha256"]:
        raise SourceError("SOURCE_SUITE_REPORT_HASH_MISMATCH", suite)
    report = load_record(report_path)
    _expect_commit(report.get("source_commit"), commit)
    # The hash only says these are the bytes the aggregate saw. The report must also name this suite and exactly this
    # suite's test paths, so a passing report copied into another suite's slot (and re-hashed) is still refused.
    if report.get("gate") != f"postgres-{suite}" or report.get("paths") != list(source.suite_paths[suite]):
        raise SourceError("SOURCE_SUITE_IDENTITY_MISMATCH", suite)
    counts = report.get("counts")
    passed = counts.get("passed") if isinstance(counts, Mapping) else None
    return (entry.get("status") == "PASS" and report.get("status") == "PASS" and report.get("skipped") == []
            and report.get("problems") == [] and report.get("missing_paths") == []
            and report.get("empty_paths") == [] and isinstance(passed, int) and not isinstance(passed, bool)
            and passed >= max(1, source.min_passed[suite]))


def _postgres_gate(source: LaneSource, record: Mapping[str, Any], path: Path, commit: str) -> SourceReading:
    suites = record.get("suite_reports")
    if record.get("schema_version") != POSTGRES_GATE_SCHEMA or not isinstance(suites, Mapping):
        raise SourceError("SOURCE_FORMAT_INVALID")
    _expect_commit(record.get("source_commit"), commit)
    missing = [suite for suite in source.required_checks if suite not in suites]
    if missing:
        raise SourceError("SOURCE_GATE_MISSING", ",".join(missing))
    passed = all([_suite_report(source, path.parent, suite, suites[suite], commit)
                  for suite in source.required_checks])
    return SourceReading(0 if passed else 1, _finished(record), _platform_family(record.get("platform")))


def read_source(source: LaneSource, path: Path, *, commit: str) -> SourceReading:
    """Interpret ``path`` as the lane's runner record for ``commit``; raises ``SourceError`` when it cannot."""
    record = load_record(path)
    if source.format == "gate-summary":
        return _gate_summary(source, record, path, commit)
    if source.format == "vm-summary":
        return _vm_summary(source, record, path, commit)
    if source.format == "e2e-result":
        return _e2e_result(source, record, path, commit)
    if source.format == "postgres-gate":
        return _postgres_gate(source, record, path, commit)
    from pados.release.runtime_lane_github import read_github_actions  # noqa: PLC0415 - it imports this module

    return read_github_actions(source, record, commit)
