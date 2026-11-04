# SPDX-License-Identifier: AGPL-3.0-only
"""Runner records shaped like the real ones, for the OP-002 lane source tests (synthetic, no network)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pados.release.runtime_lane_sources import LaneSource

ACTIONS_APP = {"slug": "github-actions", "id": 15368}


def now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def e2e_record(source: LaneSource, *, commit: str, platform: str = "Windows-11-10.0.26200-SP0",
               finished: datetime | None = None, steps: list[str] | None = None, **change: Any) -> dict[str, Any]:
    """A full ``local_dogfood_e2e.py`` result: every registry step once, all PASS."""
    names = list(source.required_checks) if steps is None else steps
    return {"runner_contract": "pados.local-dogfood-e2e.v1", "run_mode": "full", "skipped_steps": [],
            "verdict": "PASS", "commit": commit, "platform": platform,
            "finished": (finished or now()).isoformat(),
            "steps": [{"step": name, "status": "PASS", "detail": {}} for name in names]} | change


def suite_report(source: LaneSource, suite: str, *, commit: str, **change: Any) -> dict[str, Any]:
    """A ``regression_gate.py`` report for one PostgreSQL suite, as the gate writes it."""
    return {"gate": f"postgres-{suite}", "status": "PASS", "counts": {"passed": source.min_passed[suite] + 5},
            "skipped": [], "problems": [], "allowed_skips": [], "missing_paths": [], "empty_paths": [],
            "paths": list(source.suite_paths[suite]), "source_commit": commit, "source_tree": "f" * 40} | change


class GithubBundle:
    """The four unmodified GitHub responses the collector saves, with one passing run per required check."""

    def __init__(self, source: LaneSource, commit: str, *, completed: datetime | None = None) -> None:
        self.source = source
        self.commit = commit
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.workflow_runs: dict[str, dict[str, Any]] = {}
        self.annotations: dict[str, list[dict[str, Any]]] = {}
        for name in source.required_checks:
            self.add(name, completed=completed)

    def _workflow_run(self, workflow: str) -> int:
        for key, run in self.workflow_runs.items():
            if run["path"] == workflow:
                return int(key)
        run_id = 4000 + len(self.workflow_runs)
        self.workflow_runs[str(run_id)] = {"id": run_id, "name": workflow.rsplit("/", 1)[-1], "path": workflow,
                                           "head_sha": self.commit, "event": "pull_request",
                                           "repository": {"full_name": self.source.repository}}
        return run_id

    def add(self, name: str, *, completed: datetime | None = None, workflow: str | None = None,
            conclusion: str = "success", step_conclusions: dict[str, str] | None = None) -> dict[str, Any]:
        check = self.source.github_checks.get(name)
        workflow = workflow or (check.workflow if check else ".github/workflows/ci.yml")
        run_id = self._workflow_run(workflow)
        check_id = 9000 + len(self.runs)
        url = f"https://api.github.com/repos/{self.source.repository}/check-runs/{check_id}"
        done = stamp(completed or now())
        run = {"id": check_id, "name": name, "head_sha": self.commit, "status": "completed",
               "conclusion": conclusion, "url": url, "completed_at": done, "app": dict(ACTIONS_APP)}
        steps = [{"name": "Set up job", "conclusion": "success", "number": 1}]
        for number, step in enumerate(check.required_steps if check else (), start=2):
            steps.append({"name": step, "conclusion": (step_conclusions or {}).get(step, "success"),
                          "number": number})
        self.runs.append(run)
        self.jobs[str(check_id)] = {"id": check_id, "run_id": run_id, "name": name, "head_sha": self.commit,
                                    "check_run_url": url, "status": "completed", "conclusion": conclusion,
                                    "completed_at": done, "workflow_name": workflow, "steps": steps}
        self.annotations[str(check_id)] = []
        return run

    def named(self, name: str) -> list[dict[str, Any]]:
        return [run for run in self.runs if run["name"] == name]

    def record(self, **change: Any) -> dict[str, Any]:
        return {"schema_version": "pados.github-actions-evidence.v1", "repository": self.source.repository,
                "commit": self.commit, "collected_at": now().isoformat(),
                "check_runs": {"total_count": len(self.runs), "check_runs": self.runs},
                "jobs": self.jobs, "workflow_runs": self.workflow_runs, "annotations": self.annotations} | change
