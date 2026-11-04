# SPDX-License-Identifier: AGPL-3.0-only
"""Read the ``github-actions`` lane record (OP-002): required checks bound to their workflow, job, steps and time.

A check run name alone proves little: any workflow in the repository, including one added on the branch under
test, can create a job with the same name on the same commit through the same GitHub App. The record therefore keeps
four GitHub REST responses unmodified (``scripts/release/collect_github_actions_evidence.py`` writes it):

* ``check_runs``: ``GET /repos/{repo}/commits/{sha}/check-runs`` (one complete page);
* ``jobs``: ``GET /repos/{repo}/actions/jobs/{id}`` for each GitHub Actions check run, keyed by id;
* ``workflow_runs``: ``GET /repos/{repo}/actions/runs/{id}`` for each job's run, keyed by id;
* ``annotations``: ``GET /repos/{repo}/check-runs/{id}/annotations`` for each GitHub Actions check run, keyed by id.

For every check the registry requires, every run of that name by the expected app must:

* belong to the registry's repository and this commit;
* be backed by its own job (same id, name, commit and check-run URL) in a workflow run of the registry's workflow file
  for that check, in that repository, on that commit;
* have completed (otherwise ``SOURCE_CHECK_INCOMPLETE``) and concluded ``success`` (otherwise the record is a FAIL);
* show each registry-required step exactly once with conclusion ``success``. A required step that was skipped or
  ``continue-on-error`` failed makes the check ``SOURCE_CHECK_NOT_EXECUTED``;
* carry no annotation with the registry's blocker marker. A job that reports that its tool could not run (the
  dependency review with the dependency graph disabled) is ``SOURCE_CHECK_ENVIRONMENT_BLOCKED``, even when the job
  concluded success.

Both of those are NOT_VALIDATED, never PASS. Freshness counts from the oldest required run, so one stale check cannot
ride on newer ones.

Limits: the workflow identity is its path at this commit. What that file did is the commit's content, which stays with
code review. The record is saved bytes; it is not signed by GitHub, so it shows what the saved responses say, not who
saved them.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pados.release.runtime_lane_sources import GithubCheck, LaneSource, SourceError, SourceReading, aware_time

GITHUB_ACTIONS_SCHEMA = "pados.github-actions-evidence.v1"


def _mapping(record: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = record.get(key)
    if not isinstance(value, Mapping):
        raise SourceError("SOURCE_FORMAT_INVALID", key.upper())
    return value


def _check_runs(record: Mapping[str, Any], source: LaneSource, commit: str) -> list[Mapping[str, Any]]:
    page = _mapping(record, "check_runs")
    runs = page.get("check_runs")
    if not isinstance(runs, list) or not all(isinstance(run, Mapping) for run in runs) or not isinstance(
            page.get("total_count"), int):
        raise SourceError("SOURCE_FORMAT_INVALID")
    if page["total_count"] != len(runs):
        raise SourceError("SOURCE_FORMAT_INVALID", "TRUNCATED_PAGE")
    ids = [run.get("id") for run in runs]
    if len(set(map(str, ids))) != len(ids):
        raise SourceError("SOURCE_FORMAT_INVALID", "DUPLICATE_CHECK_RUN_ID")
    if any(run.get("head_sha") != commit for run in runs):
        raise SourceError("SOURCE_COMMIT_MISMATCH")
    prefix = f"https://api.github.com/repos/{source.repository}/check-runs/"
    if any(not str(run.get("url", "")).startswith(prefix) for run in runs):
        raise SourceError("SOURCE_REPOSITORY_MISMATCH")  # a fork shares the commit, not the repository
    return runs


def _by_app(runs: Sequence[Mapping[str, Any]], name: str, source: LaneSource) -> list[Mapping[str, Any]]:
    return [run for run in runs if run.get("name") == name and isinstance(run.get("app"), Mapping)
            and run["app"].get("slug") == source.app and run["app"].get("id") == source.app_id]


def _job_and_workflow(run: Mapping[str, Any], name: str, check: GithubCheck, record: Mapping[str, Any],
                      source: LaneSource, commit: str) -> Mapping[str, Any]:
    job = _mapping(record, "jobs").get(str(run.get("id")))
    if not isinstance(job, Mapping) or job.get("id") != run.get("id") or job.get("name") != name or job.get(
            "head_sha") != commit or job.get("check_run_url") != run.get("url"):
        raise SourceError("SOURCE_JOB_IDENTITY_MISMATCH", name)
    workflow_run = _mapping(record, "workflow_runs").get(str(job.get("run_id")))
    repository = workflow_run.get("repository") if isinstance(workflow_run, Mapping) else None
    if not isinstance(workflow_run, Mapping) or workflow_run.get("id") != job.get("run_id") or workflow_run.get(
            "path") != check.workflow or workflow_run.get("head_sha") != commit or not isinstance(
            repository, Mapping) or repository.get("full_name") != source.repository:
        raise SourceError("SOURCE_WORKFLOW_MISMATCH", name)
    return job


def _executed(job: Mapping[str, Any], run: Mapping[str, Any], name: str, check: GithubCheck,
              record: Mapping[str, Any]) -> None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise SourceError("SOURCE_FORMAT_INVALID", name)
    for required in check.required_steps:
        found = [step for step in steps if isinstance(step, Mapping) and step.get("name") == required]
        if len(found) != 1 or found[0].get("conclusion") != "success":
            raise SourceError("SOURCE_CHECK_NOT_EXECUTED", f"{name}: {required}")
    if check.blocker_annotation is None:
        return
    notes = _mapping(record, "annotations").get(str(run.get("id")))
    if not isinstance(notes, list):
        raise SourceError("SOURCE_FORMAT_INVALID", f"ANNOTATIONS {name}")
    marker = check.blocker_annotation
    if any(isinstance(note, Mapping) and (marker in str(note.get("title") or "") or marker in str(
            note.get("message") or "")) for note in notes):
        raise SourceError("SOURCE_CHECK_ENVIRONMENT_BLOCKED", name)


def _required_runs(source: LaneSource, runs: Sequence[Mapping[str, Any]]
                   ) -> list[tuple[str, GithubCheck, Mapping[str, Any]]]:
    required: list[tuple[str, GithubCheck, Mapping[str, Any]]] = []
    for name in source.required_checks:
        named = _by_app(runs, name, source)
        if not named:
            raise SourceError("SOURCE_GATE_MISSING", name)
        required.extend((name, source.github_checks[name], run) for run in named)
    return required


def read_github_actions(source: LaneSource, record: Mapping[str, Any], commit: str) -> SourceReading:
    if record.get("schema_version") != GITHUB_ACTIONS_SCHEMA:
        raise SourceError("SOURCE_FORMAT_INVALID")
    if record.get("repository") != source.repository:
        raise SourceError("SOURCE_REPOSITORY_MISMATCH")
    if record.get("commit") != commit:
        raise SourceError("SOURCE_COMMIT_MISMATCH")
    required = _required_runs(source, _check_runs(record, source, commit))
    completed = [run for _, _, run in required if run.get("status") == "completed"]
    moments = [aware_time(run.get("completed_at")) for run in completed]
    if any(moment is None for moment in moments):
        raise SourceError("SOURCE_FORMAT_INVALID", "COMPLETED_AT")
    times = [moment for moment in moments if moment is not None]
    # Every required run is looked at before any reason is chosen, so a completed failure is reported as a FAIL even
    # when another required check is still running or not bound to its workflow.
    if any(run.get("conclusion") != "success" for run in completed):
        return SourceReading(1, min(times), "github", newest=max(times))
    incomplete = sorted({name for name, _, run in required if run.get("status") != "completed"})
    if incomplete:
        raise SourceError("SOURCE_CHECK_INCOMPLETE", ",".join(incomplete))
    bound = [(_job_and_workflow(run, name, check, record, source, commit), run, name, check)
             for name, check, run in required]
    if any(job.get("conclusion") != "success" for job, _, _, _ in bound):
        return SourceReading(1, min(times), "github", newest=max(times))
    for job, run, name, check in bound:
        _executed(job, run, name, check, record)
    return SourceReading(0, min(times), "github", newest=max(times))
