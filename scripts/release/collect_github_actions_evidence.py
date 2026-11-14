# SPDX-License-Identifier: AGPL-3.0-only
"""Collect the ``github-ci`` runtime lane record for one commit (OP-002).

Usage: collect_github_actions_evidence.py --repository OWNER/REPO --commit SHA --out FILE

Uses the GitHub CLI (``gh api``), already signed in; this script never reads or prints a token. It saves four REST
responses unmodified inside one JSON file (schema ``pados.github-actions-evidence.v1``):

* ``check_runs``: ``GET /repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100`` (the latest attempt of
  each run; refused if it does not fit one page);
* ``jobs``: ``GET /repos/{repo}/actions/jobs/{id}`` for every GitHub Actions check run;
* ``workflow_runs``: ``GET /repos/{repo}/actions/runs/{run_id}`` for every job's workflow run;
* ``annotations``: ``GET /repos/{repo}/check-runs/{id}/annotations?per_page=100`` for every GitHub Actions check
  run (refused if a page is full, since a marker could be on the next page).

``pados.release.runtime_lane_github`` reads it. The file is evidence for the gate to interpret, not a verdict, and is
written only to ``--out`` (keep it outside the repository; it is machine-local evidence).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "pados.github-actions-evidence.v1"
ACTIONS_APP_ID = 15368  # the GitHub Actions app; only its check runs have jobs behind them
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
Fetch = Callable[[str], Any]
_PAGE = 100


def gh_api(endpoint: str) -> Any:
    """``gh api ENDPOINT`` as parsed JSON. The CLI holds the sign-in; nothing here touches it."""
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, encoding="utf-8",  # noqa: S603,S607
                            check=False, timeout=120)
    if result.returncode != 0:
        raise SystemExit(f"GITHUB_API_FAILED {endpoint} (exit {result.returncode})")
    return json.loads(result.stdout)


def collect(repository: str, commit: str, fetch: Fetch = gh_api) -> dict[str, Any]:
    if not _REPOSITORY.match(repository) or not _COMMIT.match(commit):
        raise SystemExit("GITHUB_EVIDENCE_BAD_ARGUMENTS")
    # filter=latest (the API default, stated): a re-run attempt replaces the run it supersedes.
    page = fetch(f"repos/{repository}/commits/{commit}/check-runs?filter=latest&per_page=100")
    runs = page.get("check_runs") if isinstance(page, dict) else None
    if not isinstance(runs, list) or page.get("total_count") != len(runs):
        raise SystemExit("GITHUB_EVIDENCE_CHECK_RUNS_DO_NOT_FIT_ONE_PAGE")
    actions = [run for run in runs if isinstance(run, dict) and (run.get("app") or {}).get("id") == ACTIONS_APP_ID]
    jobs = {str(run["id"]): fetch(f"repos/{repository}/actions/jobs/{int(run['id'])}") for run in actions}
    run_ids = sorted({int(job["run_id"]) for job in jobs.values() if isinstance(job, dict) and "run_id" in job})
    return {
        "schema_version": SCHEMA,
        "repository": repository,
        "commit": commit,
        "collected_at": datetime.now(UTC).isoformat(),
        "check_runs": page,
        "jobs": jobs,
        "workflow_runs": {str(run_id): fetch(f"repos/{repository}/actions/runs/{run_id}") for run_id in run_ids},
        "annotations": {str(run["id"]): _annotations(fetch, repository, int(run["id"])) for run in actions},
    }


def _annotations(fetch: Fetch, repository: str, run_id: int) -> Any:
    notes = fetch(f"repos/{repository}/check-runs/{run_id}/annotations?per_page={_PAGE}")
    if not isinstance(notes, list) or len(notes) >= _PAGE:  # a blocker marker could sit on a page not fetched
        raise SystemExit(f"GITHUB_EVIDENCE_ANNOTATIONS_DO_NOT_FIT_ONE_PAGE {run_id}")
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    record = collect(args.repository, args.commit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"GITHUB_ACTIONS_EVIDENCE {len(record['jobs'])} jobs, {len(record['workflow_runs'])} runs -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
