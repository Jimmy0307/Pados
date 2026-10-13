# SPDX-License-Identifier: AGPL-3.0-only
"""CI authority boundary policy for GitHub Actions workflows.

CI IS NOT PADOS AUTHORITY. CI SUCCESS != DEPLOYMENT AUTHORIZATION. BUILD SUCCESS
!= TRUST. This module checks, statically and fail-closed, that every workflow
stays a build/test/attest producer: least-privilege token permissions, every
third-party action pinned to a full commit SHA, hosted ephemeral runners for
untrusted (pull request) events, no secrets in pull-request workflows, no deploy
or remote-shell steps, no PADOS authority writes (``pados`` CLI, non-disposable
databases), locked build tooling, and the ``id-token`` write scope only in the
minimal release attestation job.

Dependencies are followed, not trusted: local composite actions
(``./path/action.yml``) and local reusable workflows (``./.github/workflows/*.yml``)
are inspected recursively with the caller's trigger context; remote reusable
workflows and non-composite local actions cannot be validated and are refused.
Runner labels are resolved through the full strategy matrix (axes x include,
minus exclude); anything not statically resolvable is refused. The release
guard must be the exact approved expression plus the exact ancestry step.

The checker reads YAML through ``pados.release.yaml_subset``; a file outside
that subset is reported as ``WORKFLOW_PARSE_ERROR``, never skipped.
"""
from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.release.yaml_subset import YamlSubsetError, load

PUBLIC_PR_RUNNER_POLICY = "HOSTED_EPHEMERAL_ONLY"
HOME_BASE_SELF_HOSTED_RUNNER = "PROHIBITED_FOR_UNTRUSTED_WORKLOADS"
HOSTED_RUNNERS = frozenset({"ubuntu-latest", "windows-latest", "macos-latest"})
UNTRUSTED_TRIGGERS = frozenset({
    "pull_request", "pull_request_target", "pull_request_review", "pull_request_review_comment",
    "issue_comment", "issues", "discussion", "discussion_comment", "fork", "watch", "workflow_run",
})
PROHIBITED_TRIGGERS = frozenset({"pull_request_target", "workflow_run"})
RELEASE_WORKFLOW = "release-candidate.yml"
RELEASE_BUILD_JOB = "build-release-candidate"
RELEASE_ATTEST_JOB = "attest-release-candidate"
# The only accepted release guard expression(s), compared as exact strings.
RELEASE_GUARD = "github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v')"
APPROVED_RELEASE_GUARDS = frozenset({RELEASE_GUARD})
# The build job's second step, compared as an exact mapping (no if/continue-on-error).
RELEASE_ANCESTRY_STEP: dict[str, Any] = {
    "name": "Require a commit reachable from main",
    "run": 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main',
}
RELEASE_ATTEST_PERMISSIONS = {"contents": "read", "id-token": "write", "attestations": "write"}
RELEASE_ATTEST_ACTIONS = frozenset({
    "actions/download-artifact", "actions/attest-build-provenance", "actions/upload-artifact",
})
RELEASE_ATTEST_RUNS = frozenset({"sha256sum release-out/*"})
REQUIRED_WORKFLOWS = ("ci.yml", "security.yml", RELEASE_WORKFLOW)
PERMISSION_SCOPES = frozenset({
    "actions", "attestations", "checks", "contents", "deployments", "discussions", "id-token", "issues",
    "models", "packages", "pages", "pull-requests", "repository-projects", "security-events", "statuses",
})
# (workflow file, job id) -> the only scopes that job may hold with "write".
WRITE_ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    ("security.yml", "codeql"): frozenset({"security-events"}),
    (RELEASE_WORKFLOW, RELEASE_ATTEST_JOB): frozenset({"id-token", "attestations"}),
}
DISPOSABLE_DB_HOSTS = frozenset({"127.0.0.1", "localhost", "postgres"})
MAX_DEPENDENCY_DEPTH = 8

_SHA_PIN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}")
_VERSION_COMMENT = re.compile(r"#\s*v\d+\.\d+\.\d+\s*$")
_USES_LINE = re.compile(r"^\s*(?:-\s+)?['\"]?uses['\"]?:\s*['\"]?([^'\"\s#]+)['\"]?\s*(.*)$")
_DOCKER_PIN = re.compile(r"docker://[^@\s]+@sha256:[0-9a-f]{64}")
_LOCAL_WORKFLOW = re.compile(r"\./\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")
_LOCAL_ACTION = re.compile(r"\./[A-Za-z0-9_./-]+")
_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_SECRET_REF = re.compile(r"\bsecrets\s*(?:\.[A-Za-z_]|\[)|^\s*secrets:\s*inherit\b", re.MULTILINE)
_UNTRUSTED_EXPR = re.compile(
    r"\$\{\{[^}]*\b(?:github\.event\.|github\.head_ref|inputs\.|github\.event_path)[^}]*\}\}"
)
_DEPLOY_COMMAND = re.compile(
    r"(?<![\w./-])(?:ssh|scp|sftp|rsync|ansible(?:-playbook)?|kubectl\s+(?:apply|create|replace|rollout|set|delete)"
    r"|helm\s+(?:install|upgrade)|terraform\s+(?:apply|destroy)|pulumi\s+up|docker\s+(?:push|login)"
    r"|twine\s+upload|npm\s+publish|gh\s+(?:release|workflow\s+run|api)|git\s+push|aws|az|gcloud)(?![\w-])"
)
_DEPLOY_NAME = re.compile(r"deploy|ssh|publish", re.IGNORECASE)
_PADOS_CLI = re.compile(r"(?:^|[\s;&|(`])(?:pados(?:\.exe)?|python3?\s+-m\s+pados(?:\.cli(?:\.main)?)?)(?=\s|$)(?!\s+--help)")
_DB_URL = re.compile(r"postgres(?:ql)?(?:\+[a-z0-9]+)?://(?:[^@\s/'\"]*@)?([^:/\s'\"?]+)", re.IGNORECASE)
_DB_HOST_ENV = re.compile(r"(?:^|_)(?:DB|DATABASE|PG|POSTGRES)_?HOST$")
_COMMAND_SPLIT = re.compile(r"&&|\|\||;|\n")
_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b")
_PYTHON_BUILD = re.compile(r"\bpython3?\s+-m\s+build\b")
_NPM_INSTALL = re.compile(r"\bnpm\s+(?:install|i|add)\b")


class PolicyCode(StrEnum):
    WORKFLOW_PARSE_ERROR = "WORKFLOW_PARSE_ERROR"
    REQUIRED_WORKFLOW_MISSING = "REQUIRED_WORKFLOW_MISSING"
    PROHIBITED_TRIGGER = "PROHIBITED_TRIGGER"
    TOP_LEVEL_PERMISSIONS_NOT_MINIMAL = "TOP_LEVEL_PERMISSIONS_NOT_MINIMAL"
    JOB_PERMISSIONS_MISSING = "JOB_PERMISSIONS_MISSING"
    PERMISSION_SCOPE_UNKNOWN = "PERMISSION_SCOPE_UNKNOWN"
    WRITE_PERMISSION_NOT_ALLOWED = "WRITE_PERMISSION_NOT_ALLOWED"
    ID_TOKEN_WRITE_OUTSIDE_RELEASE = "ID_TOKEN_WRITE_OUTSIDE_RELEASE"
    ACTION_NOT_PINNED = "ACTION_NOT_PINNED"
    ACTION_PIN_MISSING_VERSION_COMMENT = "ACTION_PIN_MISSING_VERSION_COMMENT"
    LOCAL_ACTION_MISSING = "LOCAL_ACTION_MISSING"
    LOCAL_ACTION_NOT_COMPOSITE = "LOCAL_ACTION_NOT_COMPOSITE"
    LOCAL_DEPENDENCY_UNVERIFIABLE = "LOCAL_DEPENDENCY_UNVERIFIABLE"
    DEPENDENCY_CYCLE_OR_TOO_DEEP = "DEPENDENCY_CYCLE_OR_TOO_DEEP"
    REUSABLE_WORKFLOW_REMOTE_FORBIDDEN = "REUSABLE_WORKFLOW_REMOTE_FORBIDDEN"
    REUSABLE_WORKFLOW_INVALID = "REUSABLE_WORKFLOW_INVALID"
    CHECKOUT_PERSISTS_CREDENTIALS = "CHECKOUT_PERSISTS_CREDENTIALS"
    CHECKOUT_UNTRUSTED_REF = "CHECKOUT_UNTRUSTED_REF"
    SELF_HOSTED_ON_UNTRUSTED_TRIGGER = "SELF_HOSTED_ON_UNTRUSTED_TRIGGER"
    RUNNER_NOT_HOSTED_EPHEMERAL = "RUNNER_NOT_HOSTED_EPHEMERAL"
    RUNNER_UNRESOLVABLE = "RUNNER_UNRESOLVABLE"
    SECRET_IN_UNTRUSTED_WORKFLOW = "SECRET_IN_UNTRUSTED_WORKFLOW"
    SECRET_REFERENCE_NOT_ALLOWED = "SECRET_REFERENCE_NOT_ALLOWED"
    UNTRUSTED_EXPRESSION_IN_RUN = "UNTRUSTED_EXPRESSION_IN_RUN"
    DEPLOY_STEP_PROHIBITED = "DEPLOY_STEP_PROHIBITED"
    DEPLOYMENT_ENVIRONMENT_PROHIBITED = "DEPLOYMENT_ENVIRONMENT_PROHIBITED"
    PADOS_AUTHORITY_COMMAND_PROHIBITED = "PADOS_AUTHORITY_COMMAND_PROHIBITED"
    NON_DISPOSABLE_DATABASE = "NON_DISPOSABLE_DATABASE"
    DATABASE_WITHOUT_DISPOSABLE_SERVICE = "DATABASE_WITHOUT_DISPOSABLE_SERVICE"
    UNPINNED_TOOL_INSTALL = "UNPINNED_TOOL_INSTALL"
    JOB_TIMEOUT_MISSING = "JOB_TIMEOUT_MISSING"
    RELEASE_TRIGGER_UNTRUSTED = "RELEASE_TRIGGER_UNTRUSTED"
    RELEASE_GUARD_INVALID = "RELEASE_GUARD_INVALID"
    RELEASE_JOB_MISSING_ATTESTATION = "RELEASE_JOB_MISSING_ATTESTATION"
    RELEASE_ATTEST_JOB_NOT_MINIMAL = "RELEASE_ATTEST_JOB_NOT_MINIMAL"
    RELEASE_BUILD_JOB_PRIVILEGED = "RELEASE_BUILD_JOB_PRIVILEGED"
    RELEASE_WORKFLOW_UNEXPECTED_JOB = "RELEASE_WORKFLOW_UNEXPECTED_JOB"
    REQUIRED_CONTROL_MISSING = "REQUIRED_CONTROL_MISSING"
    DEPENDABOT_ECOSYSTEM_MISSING = "DEPENDABOT_ECOSYSTEM_MISSING"


@dataclass(frozen=True)
class PolicyFinding:
    workflow: str
    code: PolicyCode
    location: str = ""

    def as_record(self) -> dict[str, str]:
        return {"workflow": self.workflow, "code": self.code.value, "location": self.location}


@dataclass
class _Context:
    root: Path | None
    findings: list[PolicyFinding] = field(default_factory=list)
    stack: tuple[str, ...] = ()

    def add(self, label: str, code: PolicyCode, location: str = "") -> None:
        self.findings.append(PolicyFinding(label, code, location))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def triggers(document: Mapping[str, Any]) -> dict[str, Any]:
    raw = document.get("on")
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(item): None for item in raw}
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _jobs(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    return {str(name): job for name, job in jobs.items() if isinstance(job, dict)}


def _steps(container: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = container.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def matrix_combinations(matrix: Any) -> list[dict[str, Any]] | None:
    """Every effective matrix combination (axes x include, minus exclude); ``None`` if unresolvable."""
    if not isinstance(matrix, dict):
        return None
    include, exclude = matrix.get("include", []), matrix.get("exclude", [])
    axes = {k: v for k, v in matrix.items() if k not in {"include", "exclude"}}
    if not isinstance(include, list) or not isinstance(exclude, list):
        return None
    if not all(isinstance(e, dict) for e in [*include, *exclude]):
        return None
    if any(not isinstance(v, list) or not v or any(isinstance(i, (dict, list)) for i in v) for v in axes.values()):
        return None
    keys = sorted(axes)
    combos = ([dict(zip(keys, values, strict=True)) for values in itertools.product(*(axes[k] for k in keys))]
              if keys else [])
    combos = [c for c in combos if not any(all(c.get(k) == v for k, v in e.items()) for e in exclude)]
    originals = [dict(c) for c in combos]
    extra: list[dict[str, Any]] = []
    for entry in include:
        matched = False
        for combo, original in zip(combos, originals, strict=True):
            if all(original[k] == v for k, v in entry.items() if k in original):
                combo.update(entry)
                matched = True
        if not matched:
            extra.append(dict(entry))
    result = combos + extra
    return result or None


def _resolve_label(value: Any, combos: list[dict[str, Any]] | None) -> list[str] | None:
    if not isinstance(value, str):
        return None
    match = _MATRIX_REF.fullmatch(value.strip())
    if match is None:
        return None if "${{" in value else [value]
    if combos is None:
        return None
    labels: list[str] = []
    for combo in combos:
        label = combo.get(match.group(1))
        if not isinstance(label, str) or "${{" in label:
            return None
        labels.append(label)
    return labels


def runner_labels(job: Mapping[str, Any]) -> list[str] | None:
    """All runner labels any expansion of ``job`` can request; ``None`` when unresolvable."""
    raw = job.get("runs-on")
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else None
    if not values:
        return None  # dict form (runner groups) and anything else is not statically hosted
    uses_matrix = any(isinstance(v, str) and "matrix." in v for v in values)
    combos = matrix_combinations(_dict(job.get("strategy")).get("matrix")) if uses_matrix else None
    labels: list[str] = []
    for value in values:
        resolved = _resolve_label(value, combos)
        if resolved is None:
            return None
        labels.extend(resolved)
    return labels


def _check_pins(label: str, text: str, ctx: _Context) -> set[str]:
    raw_uses: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        match = _USES_LINE.match(line)
        if match is None:
            continue
        value, rest = match.groups()
        raw_uses.add(value)
        if value.startswith("./"):
            continue
        if value.startswith("docker://"):
            if not _DOCKER_PIN.fullmatch(value):
                ctx.add(label, PolicyCode.ACTION_NOT_PINNED, f"line {number}")
            continue
        if not _SHA_PIN.fullmatch(value):
            ctx.add(label, PolicyCode.ACTION_NOT_PINNED, f"line {number}")
        elif not _VERSION_COMMENT.search(rest):
            ctx.add(label, PolicyCode.ACTION_PIN_MISSING_VERSION_COMMENT, f"line {number}")
    return raw_uses


def _seen(value: Any, raw_uses: set[str]) -> bool:
    """Every parsed ``uses`` must be one the line-level pin check saw."""
    return isinstance(value, str) and value in raw_uses


def _check_permissions(label: str, name: str, document: Mapping[str, Any], ctx: _Context) -> None:
    top = document.get("permissions", "<missing>")
    if not (isinstance(top, dict) and (top == {} or top == {"contents": "read"})):
        ctx.add(label, PolicyCode.TOP_LEVEL_PERMISSIONS_NOT_MINIMAL)
    for job_id, job in _jobs(document).items():
        permissions = job.get("permissions")
        if not isinstance(permissions, dict):
            ctx.add(label, PolicyCode.JOB_PERMISSIONS_MISSING, job_id)
            continue
        allowed = WRITE_ALLOWLIST.get((name, job_id), frozenset())
        for scope, level in permissions.items():
            if scope not in PERMISSION_SCOPES or level not in {"read", "write", "none"}:
                ctx.add(label, PolicyCode.PERMISSION_SCOPE_UNKNOWN, f"{job_id}.{scope}")
            elif level == "write" and scope == "id-token" and scope not in allowed:
                ctx.add(label, PolicyCode.ID_TOKEN_WRITE_OUTSIDE_RELEASE, job_id)
            elif level == "write" and scope not in allowed:
                ctx.add(label, PolicyCode.WRITE_PERMISSION_NOT_ALLOWED, f"{job_id}.{scope}")


def _check_run(label: str, where: str, run: str, ctx: _Context) -> bool:
    """Scan one ``run`` script; returns whether it references a database URL."""
    if _DEPLOY_COMMAND.search(run):
        ctx.add(label, PolicyCode.DEPLOY_STEP_PROHIBITED, where)
    if _PADOS_CLI.search(run):
        ctx.add(label, PolicyCode.PADOS_AUTHORITY_COMMAND_PROHIBITED, where)
    if _UNTRUSTED_EXPR.search(run):
        ctx.add(label, PolicyCode.UNTRUSTED_EXPRESSION_IN_RUN, where)
    for command in _COMMAND_SPLIT.split(run):
        unpinned_pip = _PIP_INSTALL.search(command) and "-c requirements.lock" not in command
        isolated_build = _PYTHON_BUILD.search(command) and "--no-isolation" not in command
        if unpinned_pip or isolated_build or _NPM_INSTALL.search(command):
            ctx.add(label, PolicyCode.UNPINNED_TOOL_INSTALL, where)
    hosts = _DB_URL.findall(run)
    for host in hosts:
        if host not in DISPOSABLE_DB_HOSTS:
            ctx.add(label, PolicyCode.NON_DISPOSABLE_DATABASE, where)
    return bool(hosts)


def _env_items(*scopes: Any) -> Iterable[tuple[str, str]]:
    for scope in scopes:
        env = scope.get("env") if isinstance(scope, dict) else None
        if isinstance(env, dict):
            for key, value in env.items():
                yield str(key), str(value)


def _check_db_env(label: str, where: str, scopes: Iterable[Any], ctx: _Context) -> bool:
    uses_database = False
    for key, value in _env_items(*scopes):
        if _DB_HOST_ENV.search(key.upper()):
            uses_database = True
            if value not in DISPOSABLE_DB_HOSTS:
                ctx.add(label, PolicyCode.NON_DISPOSABLE_DATABASE, f"{where}.{key}")
    return uses_database


def _relative_target(ctx: _Context, value: str) -> Path | None:
    if ctx.root is None or ".." in Path(value).parts:
        return None
    root = ctx.root.resolve()
    target = (root / value[2:]).resolve()
    return target if target.is_relative_to(root) else None


def _check_step(label: str, where: str, step: Mapping[str, Any], untrusted: bool, ctx: _Context) -> bool:
    uses = step.get("uses")
    run = step.get("run")
    uses_database = _check_db_env(label, where, [step], ctx)
    if isinstance(uses, str):
        if _DEPLOY_NAME.search(uses.split("@")[0]):
            ctx.add(label, PolicyCode.DEPLOY_STEP_PROHIBITED, where)
        if uses.startswith("./"):
            uses_database |= _check_local_action(label, where, uses, untrusted, ctx)
        if uses.startswith("actions/checkout@"):
            with_block = _dict(step.get("with"))
            if with_block.get("persist-credentials") is not False:
                ctx.add(label, PolicyCode.CHECKOUT_PERSISTS_CREDENTIALS, where)
            ref = str(with_block.get("ref", ""))
            if "github.event" in ref or "head_ref" in ref:
                ctx.add(label, PolicyCode.CHECKOUT_UNTRUSTED_REF, where)
    if run is not None:
        uses_database |= _check_run(label, where, str(run), ctx)
    return uses_database


def _enter(ctx: _Context, key: str, label: str, where: str) -> bool:
    if key in ctx.stack or len(ctx.stack) >= MAX_DEPENDENCY_DEPTH:
        ctx.add(label, PolicyCode.DEPENDENCY_CYCLE_OR_TOO_DEEP, where)
        return False
    return True


def _check_local_action(label: str, where: str, uses: str, untrusted: bool, ctx: _Context) -> bool:
    """Inspect a local action; only composite actions with checkable steps are accepted."""
    directory = _relative_target(ctx, uses) if _LOCAL_ACTION.fullmatch(uses) else None
    if directory is None:
        ctx.add(label, PolicyCode.LOCAL_DEPENDENCY_UNVERIFIABLE, f"{where}:{uses}")
        return False
    path = next((directory / n for n in ("action.yml", "action.yaml") if (directory / n).is_file()), None)
    if path is None or path.is_symlink():
        ctx.add(label, PolicyCode.LOCAL_ACTION_MISSING, f"{where}:{uses}")
        return False
    key = str(path)
    if not _enter(ctx, key, label, where):
        return False
    child = f"{label}>{uses}"
    text = path.read_text(encoding="utf-8")
    try:
        document = load(text)
    except YamlSubsetError as error:
        ctx.add(child, PolicyCode.WORKFLOW_PARSE_ERROR, error.code)
        return False
    runs = _dict(_dict(document).get("runs"))
    if runs.get("using") != "composite":
        ctx.add(child, PolicyCode.LOCAL_ACTION_NOT_COMPOSITE, str(runs.get("using", "")))
        return False
    saved = ctx.stack
    ctx.stack = (*saved, key)
    try:
        raw_uses = _check_pins(child, text, ctx)
        uses_database = False
        for index, step in enumerate(_steps(runs)):
            if "uses" in step and not _seen(step["uses"], raw_uses):
                ctx.add(child, PolicyCode.ACTION_NOT_PINNED, f"steps[{index}]")
            uses_database |= _check_step(child, f"steps[{index}]", step, untrusted, ctx)
        if _SECRET_REF.search(text):
            ctx.add(child, PolicyCode.SECRET_IN_UNTRUSTED_WORKFLOW if untrusted
                    else PolicyCode.SECRET_REFERENCE_NOT_ALLOWED)
        return uses_database
    finally:
        ctx.stack = saved


def _check_reusable_call(label: str, job_id: str, uses: Any, untrusted: bool, ctx: _Context) -> None:
    if not isinstance(uses, str) or not uses.startswith("./") or "@" in uses:
        ctx.add(label, PolicyCode.REUSABLE_WORKFLOW_REMOTE_FORBIDDEN, job_id)
        return
    target = _relative_target(ctx, uses) if _LOCAL_WORKFLOW.fullmatch(uses) else None
    if target is None:
        ctx.add(label, PolicyCode.LOCAL_DEPENDENCY_UNVERIFIABLE, f"{job_id}:{uses}")
        return
    if not target.is_file() or target.is_symlink():
        ctx.add(label, PolicyCode.REUSABLE_WORKFLOW_INVALID, f"{job_id}:{uses}")
        return
    if not _enter(ctx, str(target), label, job_id):
        return
    saved = ctx.stack
    ctx.stack = (*saved, str(target))
    try:
        _check_document(target.name, f"{label}>{uses}", target.read_text(encoding="utf-8"), ctx,
                        inherited_untrusted=untrusted, reusable=True)
    finally:
        ctx.stack = saved


def _check_jobs(label: str, document: Mapping[str, Any], raw_uses: set[str], untrusted: bool,
                ctx: _Context) -> None:
    for job_id, job in _jobs(document).items():
        if _DEPLOY_NAME.search(job_id) or _DEPLOY_NAME.search(str(job.get("name", ""))):
            ctx.add(label, PolicyCode.DEPLOY_STEP_PROHIBITED, job_id)
        if "environment" in job:
            ctx.add(label, PolicyCode.DEPLOYMENT_ENVIRONMENT_PROHIBITED, job_id)
        if "uses" in job:
            if not _seen(job["uses"], raw_uses):
                ctx.add(label, PolicyCode.ACTION_NOT_PINNED, job_id)
            _check_reusable_call(label, job_id, job["uses"], untrusted, ctx)
            continue
        if "timeout-minutes" not in job:
            ctx.add(label, PolicyCode.JOB_TIMEOUT_MISSING, job_id)
        labels = runner_labels(job)
        if labels is None:
            ctx.add(label, PolicyCode.RUNNER_UNRESOLVABLE, job_id)
        else:
            for runner in labels:
                if runner not in HOSTED_RUNNERS:
                    code = (PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER if untrusted and "self-hosted" in labels
                            else PolicyCode.RUNNER_NOT_HOSTED_EPHEMERAL)
                    ctx.add(label, code, f"{job_id}:{runner}")
        uses_database = _check_db_env(label, job_id, [document, job], ctx)
        for index, step in enumerate(_steps(job)):
            if "uses" in step and not _seen(step["uses"], raw_uses):
                ctx.add(label, PolicyCode.ACTION_NOT_PINNED, f"{job_id}.steps[{index}]")
            uses_database |= _check_step(label, f"{job_id}.steps[{index}]", step, untrusted, ctx)
        if uses_database and not _dict(job.get("services")):
            ctx.add(label, PolicyCode.DATABASE_WITHOUT_DISPOSABLE_SERVICE, job_id)


# The privileged attest job has a fixed shape: no env, defaults, container, services or strategy.
RELEASE_ATTEST_JOB_KEYS = frozenset({"name", "needs", "if", "runs-on", "timeout-minutes", "permissions", "steps"})
RELEASE_WORKFLOW_FORBIDDEN_KEYS = ("env", "defaults")


def _guard_ok(job: Mapping[str, Any]) -> bool:
    guard = job.get("if")
    return isinstance(guard, str) and guard.strip() in APPROVED_RELEASE_GUARDS


def _check_release(label: str, document: Mapping[str, Any], ctx: _Context) -> None:
    events = triggers(document)
    push = events.get("push")
    push_tags_only = isinstance(push, dict) and set(push) == {"tags"}
    if (set(events) - {"workflow_dispatch", "push"} or ("push" in events and not push_tags_only)
            or events.get("workflow_dispatch") is not None):
        ctx.add(label, PolicyCode.RELEASE_TRIGGER_UNTRUSTED)
    jobs = _jobs(document)
    for extra in sorted(set(jobs) - {RELEASE_BUILD_JOB, RELEASE_ATTEST_JOB}):
        ctx.add(label, PolicyCode.RELEASE_WORKFLOW_UNEXPECTED_JOB, extra)
    build, attest = jobs.get(RELEASE_BUILD_JOB), jobs.get(RELEASE_ATTEST_JOB)
    if build is None or attest is None:
        ctx.add(label, PolicyCode.REQUIRED_CONTROL_MISSING, f"{RELEASE_BUILD_JOB}+{RELEASE_ATTEST_JOB}")
        return
    steps = _steps(build)
    checkout = steps[0] if steps else {}
    checkout_ok = (str(checkout.get("uses", "")).startswith("actions/checkout@")
                   and _dict(checkout.get("with")).get("fetch-depth") == 0 and "if" not in checkout)
    ancestry_ok = len(steps) > 1 and steps[1] == RELEASE_ANCESTRY_STEP
    if not _guard_ok(build) or not checkout_ok or not ancestry_ok:
        ctx.add(label, PolicyCode.RELEASE_GUARD_INVALID, RELEASE_BUILD_JOB)
    if _dict(build.get("permissions")) != {"contents": "read"} or any(
            str(s.get("uses", "")).startswith("actions/attest-build-provenance@") for s in steps):
        ctx.add(label, PolicyCode.RELEASE_BUILD_JOB_PRIVILEGED, RELEASE_BUILD_JOB)
    if not _guard_ok(attest) or attest.get("needs") not in (RELEASE_BUILD_JOB, [RELEASE_BUILD_JOB]):
        ctx.add(label, PolicyCode.RELEASE_GUARD_INVALID, RELEASE_ATTEST_JOB)
    attest_steps = _steps(attest)
    actions = {str(s.get("uses", "")).split("@")[0] for s in attest_steps if "uses" in s}
    runs = {str(s.get("run", "")).strip() for s in attest_steps if "run" in s}
    if (_dict(attest.get("permissions")) != RELEASE_ATTEST_PERMISSIONS or not actions <= RELEASE_ATTEST_ACTIONS
            or not runs <= RELEASE_ATTEST_RUNS or any("uses" in s and "run" in s for s in attest_steps)
            or any(set(s) - {"name", "id", "uses", "with", "run"} for s in attest_steps)
            or set(attest) - RELEASE_ATTEST_JOB_KEYS
            or any(key in document for key in RELEASE_WORKFLOW_FORBIDDEN_KEYS)):
        # CR04-N01: env (BASH_ENV, ENV, LD_PRELOAD), defaults (shell, working-directory), container
        # or services on the OIDC job would execute downloaded content with its permissions.
        ctx.add(label, PolicyCode.RELEASE_ATTEST_JOB_NOT_MINIMAL, RELEASE_ATTEST_JOB)
    if "actions/attest-build-provenance" not in actions:
        ctx.add(label, PolicyCode.RELEASE_JOB_MISSING_ATTESTATION, RELEASE_ATTEST_JOB)


def _check_security_controls(label: str, document: Mapping[str, Any], ctx: _Context) -> None:
    jobs = _jobs(document)
    codeql = jobs.get("codeql", {})
    matrix = _dict(_dict(codeql.get("strategy")).get("matrix"))
    languages = matrix.get("language")
    languages = languages if isinstance(languages, list) else []
    uses = {str(step.get("uses", "")).split("@")[0] for job in jobs.values() for step in _steps(job)}
    runs = " ".join(str(step.get("run", "")) for job in jobs.values() for step in _steps(job))
    required = {
        "codeql:python": "python" in languages,
        "codeql:javascript-typescript": "javascript-typescript" in languages,
        "codeql:init": "github/codeql-action/init" in uses,
        "codeql:analyze": "github/codeql-action/analyze" in uses,
        "dependency-review": "actions/dependency-review-action" in uses
        and "pull_request" in str(jobs.get("dependency-review", {}).get("if", "")),
        "secret-scan": "scripts/release/secret_scan.py" in runs,
        "ci-policy": "scripts/release/ci_policy_check.py" in runs,
    }
    for control, present in required.items():
        if not present:
            ctx.add(label, PolicyCode.REQUIRED_CONTROL_MISSING, control)


def _check_document(name: str, label: str, text: str, ctx: _Context, *, inherited_untrusted: bool = False,
                    reusable: bool = False) -> None:
    try:
        document = load(text)
    except YamlSubsetError as error:
        ctx.add(label, PolicyCode.WORKFLOW_PARSE_ERROR, error.code)
        return
    if not isinstance(document, dict) or not _jobs(document):
        ctx.add(label, PolicyCode.WORKFLOW_PARSE_ERROR, "NOT_A_WORKFLOW")
        return
    events = triggers(document)
    if reusable and "workflow_call" not in events:
        ctx.add(label, PolicyCode.REUSABLE_WORKFLOW_INVALID, "missing workflow_call")
    untrusted = inherited_untrusted or bool(set(events) & UNTRUSTED_TRIGGERS)
    for event in sorted(set(events) & PROHIBITED_TRIGGERS):
        ctx.add(label, PolicyCode.PROHIBITED_TRIGGER, event)
    _check_permissions(label, name, document, ctx)
    raw_uses = _check_pins(label, text, ctx)
    _check_jobs(label, document, raw_uses, untrusted, ctx)
    if _SECRET_REF.search(text):
        ctx.add(label, PolicyCode.SECRET_IN_UNTRUSTED_WORKFLOW if untrusted
                else PolicyCode.SECRET_REFERENCE_NOT_ALLOWED)
    if name == RELEASE_WORKFLOW and not reusable:
        _check_release(label, document, ctx)
    if name == "security.yml" and not reusable:
        _check_security_controls(label, document, ctx)


def check_workflow(name: str, text: str, *, root: Path | None = None) -> list[PolicyFinding]:
    """Policy findings for one workflow (``name`` is its file name).

    ``root`` is the repository root used to inspect local actions and local
    reusable workflows; without it such dependencies are unverifiable (refused).
    """
    ctx = _Context(root)
    _check_document(name, name, text, ctx)
    return ctx.findings


def check_dependabot(text: str, name: str = "dependabot.yml") -> list[PolicyFinding]:
    try:
        document = load(text)
    except YamlSubsetError as error:
        return [PolicyFinding(name, PolicyCode.WORKFLOW_PARSE_ERROR, error.code)]
    updates = document.get("updates") if isinstance(document, dict) else None
    present = {(str(u.get("package-ecosystem")), str(u.get("directory")))
               for u in updates or [] if isinstance(u, dict)}
    required = {("pip", "/"), ("npm", "/web"), ("github-actions", "/")}
    return [PolicyFinding(name, PolicyCode.DEPENDABOT_ECOSYSTEM_MISSING, f"{eco}:{directory}")
            for eco, directory in sorted(required - present)]


def check_repository(root: Path) -> list[PolicyFinding]:
    """Check every workflow under ``.github/workflows`` plus ``.github/dependabot.yml``."""
    workflow_dir = root / ".github" / "workflows"
    files = sorted(p for p in workflow_dir.glob("*") if p.suffix in {".yml", ".yaml"}) if workflow_dir.is_dir() else []
    findings: list[PolicyFinding] = []
    names = {path.name for path in files}
    findings.extend(PolicyFinding(required, PolicyCode.REQUIRED_WORKFLOW_MISSING)
                    for required in REQUIRED_WORKFLOWS if required not in names)
    for path in files:
        findings.extend(check_workflow(path.name, path.read_text(encoding="utf-8"), root=root))
    dependabot = root / ".github" / "dependabot.yml"
    if dependabot.is_file():
        findings.extend(check_dependabot(dependabot.read_text(encoding="utf-8")))
    else:
        findings.append(PolicyFinding("dependabot.yml", PolicyCode.REQUIRED_WORKFLOW_MISSING))
    return findings
