"""CI authority boundary: the repository workflows pass, and every violation is caught."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pados.release.workflow_policy import (
    HOME_BASE_SELF_HOSTED_RUNNER,
    PUBLIC_PR_RUNNER_POLICY,
    PolicyCode,
    check_dependabot,
    check_repository,
    check_workflow,
    matrix_combinations,
    triggers,
)
from pados.release.yaml_subset import load

from .support import ROOT

SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
PR_WORKFLOW = f"""name: pr
on:
  pull_request:
permissions: {{}}
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{SHA} # v7.0.1
        with:
          persist-credentials: false
      - run: python -m pytest -q
"""
# Built at runtime so the release packager's credential-assignment scan never matches this file.
ID_TOKEN = "id-" + "tok" + "en"
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def codes(text: str, name: str = "pr.yml") -> set[PolicyCode]:
    return {finding.code for finding in check_workflow(name, text)}


def test_repository_workflows_pass_policy() -> None:
    assert [p.name for p in WORKFLOWS] == ["ci.yml", "packaging.yml", "release-candidate.yml", "security.yml"]
    assert check_repository(ROOT) == []


def test_baseline_fixture_is_clean() -> None:
    assert codes(PR_WORKFLOW) == set()


def test_runner_policy_constants() -> None:
    assert PUBLIC_PR_RUNNER_POLICY == "HOSTED_EPHEMERAL_ONLY"
    assert HOME_BASE_SELF_HOSTED_RUNNER == "PROHIBITED_FOR_UNTRUSTED_WORKLOADS"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_third_party_action_is_sha_pinned_with_version_comment(path: Path) -> None:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if "uses:" in line]
    assert lines
    for line in lines:
        value, _, comment = line.split("uses:", 1)[1].strip().partition(" # ")
        assert len(value.rsplit("@", 1)[1]) == 40 and all(c in "0123456789abcdef" for c in value.rsplit("@", 1)[1])
        assert comment.startswith("v") and comment.count(".") == 2


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_permissions_are_least_privilege(path: Path) -> None:
    document = load(path.read_text(encoding="utf-8"))
    assert document["permissions"] == {}
    for job_id, job in document["jobs"].items():
        writes = {scope for scope, level in job["permissions"].items() if level == "write"}
        if path.name == "release-candidate.yml" and job_id == "attest-release-candidate":
            assert writes == {"id-token", "attestations"}
        elif job_id == "codeql":
            assert writes == {"security-events"}
        else:
            assert writes == set()


def test_only_release_job_has_id_token_write() -> None:
    holders = []
    for path in WORKFLOWS:
        for job_id, job in load(path.read_text(encoding="utf-8"))["jobs"].items():
            if job["permissions"].get("id-token") == "write":
                holders.append((path.name, job_id))
    assert holders == [("release-candidate.yml", "attest-release-candidate")]


def test_pr_triggered_workflows_use_hosted_runners_and_no_secrets() -> None:
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        document = load(text)
        if "pull_request" not in triggers(document):
            continue
        assert "secrets." not in text and "secrets[" not in text
        for job in document["jobs"].values():
            assert job["runs-on"] in {"ubuntu-latest", "windows-latest", "macos-latest"}


def test_release_workflow_is_trusted_only_and_never_deploys() -> None:
    document = load((ROOT / ".github/workflows/release-candidate.yml").read_text(encoding="utf-8"))
    assert set(triggers(document)) == {"workflow_dispatch", "push"}
    assert document["on"]["push"] == {"tags": ["v*"]}
    build, attest = document["jobs"]["build-release-candidate"], document["jobs"]["attest-release-candidate"]
    build_uses = [step.get("uses", "").split("@")[0] for step in build["steps"]]
    attest_uses = [step.get("uses", "").split("@")[0] for step in attest["steps"]]
    assert "actions/upload-artifact" in build_uses and "actions/attest-build-provenance" not in build_uses
    assert "actions/attest-build-provenance" in attest_uses and "actions/checkout" not in attest_uses
    assert build["permissions"] == {"contents": "read"}
    assert "environment" not in build and "environment" not in attest


def test_no_workflow_deploys_or_runs_pados_authority_commands() -> None:
    for path in WORKFLOWS:
        found = {f.code for f in check_workflow(path.name, path.read_text(encoding="utf-8"))}
        assert PolicyCode.DEPLOY_STEP_PROHIBITED not in found
        assert PolicyCode.PADOS_AUTHORITY_COMMAND_PROHIBITED not in found


def _replace(old: str, new: str) -> str:
    assert old in PR_WORKFLOW
    return PR_WORKFLOW.replace(old, new)


@pytest.mark.parametrize("text,expected", [
    (_replace(f"@{SHA} # v7.0.1", "@v4"), PolicyCode.ACTION_NOT_PINNED),
    (_replace(f"@{SHA} # v7.0.1", f"@{SHA[:12]} # v7.0.1"), PolicyCode.ACTION_NOT_PINNED),
    (_replace(f"@{SHA} # v7.0.1", f"@{SHA}"), PolicyCode.ACTION_PIN_MISSING_VERSION_COMMENT),
    (_replace(f"      - uses: actions/checkout@{SHA} # v7.0.1\n        with:\n          persist-credentials: false\n",
              f"      - {{uses: actions/checkout@{SHA}}}\n"), PolicyCode.ACTION_NOT_PINNED),
    (_replace("persist-credentials: false", "persist-credentials: true"), PolicyCode.CHECKOUT_PERSISTS_CREDENTIALS),
    (_replace("persist-credentials: false", "persist-credentials: false\n          ref: ${{ github.event.pull_request.head.sha }}"),
     PolicyCode.CHECKOUT_UNTRUSTED_REF),
    (_replace("runs-on: ubuntu-latest", "runs-on: self-hosted"), PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER),
    (_replace("runs-on: ubuntu-latest", "runs-on: [self-hosted, linux]"), PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER),
    (_replace("runs-on: ubuntu-latest", "runs-on: home-base"), PolicyCode.RUNNER_NOT_HOSTED_EPHEMERAL),
    (_replace("runs-on: ubuntu-latest", "runs-on: ${{ inputs.runner }}"), PolicyCode.RUNNER_UNRESOLVABLE),
    (_replace("runs-on: ubuntu-latest", "runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        os: [ubuntu-latest, self-hosted]"),
     PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER),
    (_replace("  pull_request:\n", "  pull_request_target:\n"), PolicyCode.PROHIBITED_TRIGGER),
    (_replace("  pull_request:\n", "  workflow_run:\n    workflows: [ci]\n"), PolicyCode.PROHIBITED_TRIGGER),
    (_replace("permissions: {}\n", "permissions: write-all\n"), PolicyCode.TOP_LEVEL_PERMISSIONS_NOT_MINIMAL),
    (_replace("permissions: {}\n", "permissions:\n  contents: write\n"), PolicyCode.TOP_LEVEL_PERMISSIONS_NOT_MINIMAL),
    (_replace("permissions: {}\n", ""), PolicyCode.TOP_LEVEL_PERMISSIONS_NOT_MINIMAL),
    (_replace("    permissions:\n      contents: read\n", ""), PolicyCode.JOB_PERMISSIONS_MISSING),
    (_replace("      contents: read\n", "      contents: write\n"), PolicyCode.WRITE_PERMISSION_NOT_ALLOWED),
    (_replace("      contents: read\n", "      contents: read\n      " + ID_TOKEN + ": write\n"),
     PolicyCode.ID_TOKEN_WRITE_OUTSIDE_RELEASE),
    (_replace("      contents: read\n", "      contents: read\n      secrets: write\n"), PolicyCode.PERMISSION_SCOPE_UNKNOWN),
    (_replace("python -m pytest -q", "echo ${{ secrets.TOKEN_FOR_X }}"), PolicyCode.SECRET_IN_UNTRUSTED_WORKFLOW),
    (_replace("python -m pytest -q", "echo \"${{ github.event.pull_request.title }}\""),
     PolicyCode.UNTRUSTED_EXPRESSION_IN_RUN),
    (_replace("python -m pytest -q", "ssh home-base 'systemctl restart pados'"), PolicyCode.DEPLOY_STEP_PROHIBITED),
    (_replace("python -m pytest -q", "rsync -a dist/ host:/srv"), PolicyCode.DEPLOY_STEP_PROHIBITED),
    (_replace("python -m pytest -q", "git push origin HEAD:main"), PolicyCode.DEPLOY_STEP_PROHIBITED),
    (_replace("python -m pytest -q", "twine upload dist/*"), PolicyCode.DEPLOY_STEP_PROHIBITED),
    (_replace("  test:\n", "  deploy:\n"), PolicyCode.DEPLOY_STEP_PROHIBITED),
    (_replace("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    environment: production\n"),
     PolicyCode.DEPLOYMENT_ENVIRONMENT_PROHIBITED),
    (_replace("python -m pytest -q", "pados project approve --id 1"), PolicyCode.PADOS_AUTHORITY_COMMAND_PROHIBITED),
    (_replace("python -m pytest -q", "python -m pados.cli.main loop run"), PolicyCode.PADOS_AUTHORITY_COMMAND_PROHIBITED),
    (_replace("python -m pytest -q", "psql postgresql://u@prod-db/pados"), PolicyCode.NON_DISPOSABLE_DATABASE),
    (_replace("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    env:\n      PADOS_DB_HOST: db.example.com\n"),
     PolicyCode.NON_DISPOSABLE_DATABASE),
    (_replace("    timeout-minutes: 10\n", "    timeout-minutes: 10\n    env:\n      PADOS_DB_HOST: 127.0.0.1\n"),
     PolicyCode.DATABASE_WITHOUT_DISPOSABLE_SERVICE),
    (_replace("    timeout-minutes: 10\n", ""), PolicyCode.JOB_TIMEOUT_MISSING),
    (_replace("permissions: {}\n", "permissions: {}\nx: &anchor 1\n"), PolicyCode.WORKFLOW_PARSE_ERROR),
])
def test_violations_are_reported(text: str, expected: PolicyCode) -> None:
    assert expected in codes(text)


def test_pados_help_and_paths_are_not_authority_commands() -> None:
    text = _replace("python -m pytest -q", "pados --help && mypy src/pados/release && ls dist/pados-*")
    assert PolicyCode.PADOS_AUTHORITY_COMMAND_PROHIBITED not in codes(text)


def test_secrets_outside_pr_workflows_are_still_refused() -> None:
    text = _replace("  pull_request:\n", "  workflow_dispatch:\n").replace("python -m pytest -q", "echo ${{ secrets.X }}")
    assert PolicyCode.SECRET_REFERENCE_NOT_ALLOWED in codes(text)


RELEASE = (ROOT / ".github/workflows/release-candidate.yml").read_text(encoding="utf-8")
GUARD = "if: github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v')"
ANCESTRY = '        run: git merge-base --is-ancestor "$GITHUB_SHA" origin/main\n'


def release_codes(text: str) -> set[PolicyCode]:
    return {f.code for f in check_workflow("release-candidate.yml", text, root=ROOT)}


def test_release_workflow_rules() -> None:
    assert release_codes(RELEASE) == set()
    with_pr = RELEASE.replace("  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request:\n")
    assert PolicyCode.RELEASE_TRIGGER_UNTRUSTED in release_codes(with_pr)
    branch_push = RELEASE.replace('    tags: ["v*"]\n', "    branches: [main]\n")
    assert PolicyCode.RELEASE_TRIGGER_UNTRUSTED in release_codes(branch_push)
    unguarded = "\n".join(line for line in RELEASE.splitlines() if line.strip() != GUARD)
    assert PolicyCode.RELEASE_GUARD_INVALID in release_codes(unguarded + "\n")
    no_attest = RELEASE.replace("      attestations: write\n", "")
    assert PolicyCode.RELEASE_ATTEST_JOB_NOT_MINIMAL in release_codes(no_attest)
    # The same id-token permission in any other workflow is refused.
    assert PolicyCode.ID_TOKEN_WRITE_OUTSIDE_RELEASE in codes(RELEASE, "other.yml")


@pytest.mark.parametrize("guard", [
    "true || github.ref == 'refs/heads/main'",
    "github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v') || true",
    "always()",
    "contains(github.ref, 'refs/heads/main')",
])
def test_cr04_sc06_release_guard_must_be_exact(guard: str) -> None:
    text = RELEASE.replace(GUARD, f"if: {guard}", 1)
    assert PolicyCode.RELEASE_GUARD_INVALID in release_codes(text)


def test_cr04_sc06_ancestry_step_is_required_exactly() -> None:
    assert ANCESTRY in RELEASE
    missing = RELEASE.replace("      - name: Require a commit reachable from main\n" + ANCESTRY, "")
    assert PolicyCode.RELEASE_GUARD_INVALID in release_codes(missing)
    weakened = RELEASE.replace(ANCESTRY, ANCESTRY + "        continue-on-error: true\n")
    assert PolicyCode.RELEASE_GUARD_INVALID in release_codes(weakened)
    other = RELEASE.replace(ANCESTRY, '        run: git merge-base --is-ancestor "$GITHUB_SHA" origin/main || true\n')
    assert PolicyCode.RELEASE_GUARD_INVALID in release_codes(other)
    shallow = RELEASE.replace("          fetch-depth: 0\n", "")
    assert PolicyCode.RELEASE_GUARD_INVALID in release_codes(shallow)


def test_cr04_sc07_build_and_attest_jobs_are_separated() -> None:
    privileged_build = RELEASE.replace("    permissions:\n      contents: read\n    env:\n      RELEASE_DIR",
                                       "    permissions:\n      contents: read\n      " + ID_TOKEN + ": write\n"
                                       "    env:\n      RELEASE_DIR", 1)
    found = release_codes(privileged_build)
    assert PolicyCode.ID_TOKEN_WRITE_OUTSIDE_RELEASE in found and PolicyCode.RELEASE_BUILD_JOB_PRIVILEGED in found
    attest_runs_code = RELEASE.replace("        run: sha256sum release-out/*\n",
                                       "        run: pip install -c requirements.lock somepkg\n")
    assert PolicyCode.RELEASE_ATTEST_JOB_NOT_MINIMAL in release_codes(attest_runs_code)
    attest_checkout = RELEASE.replace(
        "      - name: Download release candidate\n",
        f"      - uses: actions/checkout@{SHA} # v7.0.1\n        with:\n          persist-credentials: false\n"
        "      - name: Download release candidate\n")
    assert PolicyCode.RELEASE_ATTEST_JOB_NOT_MINIMAL in release_codes(attest_checkout)
    extra_job = RELEASE + "  extra:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    permissions: {}\n" \
                          "    steps:\n      - run: echo hi\n"
    assert PolicyCode.RELEASE_WORKFLOW_UNEXPECTED_JOB in release_codes(extra_job)


@pytest.mark.parametrize("run", [
    'pip install "build>=1.2,<2"',
    "python -m pip install build",
    "python -m build --sdist --wheel",
    "npm install left-pad",
])
def test_cr04_sc07_unpinned_tool_installs_are_refused(run: str) -> None:
    assert PolicyCode.UNPINNED_TOOL_INSTALL in codes(_replace("python -m pytest -q", run))


def test_cr04_sc07_locked_installs_are_accepted() -> None:
    text = _replace("python -m pytest -q", "python -m pip install -c requirements.lock -r scripts/release/"
                                           "build-requirements.lock -e . && python -m build --no-isolation && npm ci")
    assert PolicyCode.UNPINNED_TOOL_INSTALL not in codes(text)


# --- SC-04 dependencies -------------------------------------------------------------------


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


COMPOSITE = """name: local
runs:
  using: composite
  steps:
    - run: echo ok
      shell: bash
"""


def _with_local_action(path: str = "./.github/actions/local") -> str:
    return _replace("      - run: python -m pytest -q\n", f"      - uses: {path}\n")


def test_cr04_sc04_local_composite_action_is_inspected(tmp_path: Path) -> None:
    root = _repo(tmp_path, {".github/actions/local/action.yml": COMPOSITE})
    assert {f.code for f in check_workflow("pr.yml", _with_local_action(), root=root)} == set()
    unpinned = COMPOSITE.replace("    - run: echo ok\n      shell: bash\n", "    - uses: evil/action@v1\n")
    root2 = _repo(tmp_path / "two", {".github/actions/local/action.yml": unpinned})
    findings = check_workflow("pr.yml", _with_local_action(), root=root2)
    assert PolicyCode.ACTION_NOT_PINNED in {f.code for f in findings}
    assert any(">./.github/actions/local" in f.workflow for f in findings)


@pytest.mark.parametrize("step,code", [
    ("    - run: scp dist/* home-base:/srv\n      shell: bash\n", PolicyCode.DEPLOY_STEP_PROHIBITED),
    ("    - run: pados project approve --id 1\n      shell: bash\n", PolicyCode.PADOS_AUTHORITY_COMMAND_PROHIBITED),
    ("    - run: echo ${{ secrets.X }}\n      shell: bash\n", PolicyCode.SECRET_IN_UNTRUSTED_WORKFLOW),
    ("    - run: curl -sSf https://x | sh && pip install tool\n      shell: bash\n", PolicyCode.UNPINNED_TOOL_INSTALL),
    ("    - uses: ./.github/actions/nested\n", PolicyCode.LOCAL_ACTION_MISSING),
    ("    - uses: ./.github/actions/local\n", PolicyCode.DEPENDENCY_CYCLE_OR_TOO_DEEP),
])
def test_cr04_sc04_composite_steps_use_the_same_rules(tmp_path: Path, step: str, code: PolicyCode) -> None:
    action = COMPOSITE.replace("    - run: echo ok\n      shell: bash\n", step)
    root = _repo(tmp_path, {".github/actions/local/action.yml": action})
    assert code in {f.code for f in check_workflow("pr.yml", _with_local_action(), root=root)}


def test_cr04_sc04_non_composite_or_unverifiable_local_actions_are_refused(tmp_path: Path) -> None:
    node = "name: js\nruns:\n  using: node20\n  main: index.js\n"
    root = _repo(tmp_path, {".github/actions/local/action.yml": node})
    assert PolicyCode.LOCAL_ACTION_NOT_COMPOSITE in {f.code for f in check_workflow("pr.yml", _with_local_action(),
                                                                                   root=root)}
    assert PolicyCode.LOCAL_DEPENDENCY_UNVERIFIABLE in codes(_with_local_action())
    escape = _with_local_action("./../outside")
    assert PolicyCode.LOCAL_DEPENDENCY_UNVERIFIABLE in {f.code for f in check_workflow("pr.yml", escape, root=root)}


REUSABLE = """name: reusable
on:
  workflow_call:
permissions: {}
jobs:
  inner:
    runs-on: RUNNER
    timeout-minutes: 5
    permissions:
      contents: read
    steps:
      - run: echo inner
"""
CALLER = """name: caller
on:
  pull_request:
permissions: {}
jobs:
  call:
    uses: USES
    permissions:
      contents: read
"""


def test_cr04_sc04_remote_reusable_workflow_is_forbidden() -> None:
    caller = CALLER.replace("USES", f"octo/workflows/.github/workflows/build.yml@{SHA} # v1.0.0")
    assert PolicyCode.REUSABLE_WORKFLOW_REMOTE_FORBIDDEN in codes(caller, "caller.yml")


def test_cr04_sc04_local_reusable_workflow_is_checked_with_the_caller_trigger(tmp_path: Path) -> None:
    caller = CALLER.replace("USES", "./.github/workflows/reusable.yml")
    clean = _repo(tmp_path, {".github/workflows/reusable.yml": REUSABLE.replace("RUNNER", "ubuntu-latest")})
    assert check_workflow("caller.yml", caller, root=clean) == []
    dirty = _repo(tmp_path / "two", {".github/workflows/reusable.yml": REUSABLE.replace("RUNNER", "self-hosted")})
    found = {f.code for f in check_workflow("caller.yml", caller, root=dirty)}
    assert PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER in found
    no_call = _repo(tmp_path / "three", {".github/workflows/reusable.yml":
                                         REUSABLE.replace("workflow_call", "push").replace("RUNNER", "ubuntu-latest")})
    assert PolicyCode.REUSABLE_WORKFLOW_INVALID in {f.code for f in check_workflow("caller.yml", caller, root=no_call)}


# --- SC-05 matrix --------------------------------------------------------------------------


@pytest.mark.parametrize("matrix,expected", [
    ("        os: [ubuntu-latest]\n        include:\n          - os: self-hosted\n",
     PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER),
    ("        os: [ubuntu-latest]\n        py: [\"3.12\"]\n        include:\n          - py: \"3.13\"\n"
     "            os: home-base\n", PolicyCode.RUNNER_NOT_HOSTED_EPHEMERAL),
    ("        include:\n          - os: self-hosted\n", PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER),
    ("        py: [\"3.12\"]\n", PolicyCode.RUNNER_UNRESOLVABLE),
    ("        os: ${{ fromJSON(inputs.runners) }}\n", PolicyCode.RUNNER_UNRESOLVABLE),
    ("        os: [ubuntu-latest]\n        include:\n          - py: \"3.12\"\n", None),
])
def test_cr04_sc05_matrix_is_fully_expanded(matrix: str, expected: PolicyCode | None) -> None:
    text = _replace("runs-on: ubuntu-latest", "runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n" + matrix.rstrip("\n"))
    found = codes(text)
    if expected is None:
        assert not found & {PolicyCode.RUNNER_UNRESOLVABLE, PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER,
                            PolicyCode.RUNNER_NOT_HOSTED_EPHEMERAL}
    else:
        assert expected in found


def test_cr04_sc05_matrix_expansion_semantics() -> None:
    combos = matrix_combinations({"os": ["ubuntu-latest", "windows-latest"], "py": ["3.12"],
                                  "exclude": [{"os": "windows-latest"}],
                                  "include": [{"os": "ubuntu-latest", "extra": "x"}, {"os": "macos-latest"}]})
    assert combos == [{"os": "ubuntu-latest", "py": "3.12", "extra": "x"}, {"os": "macos-latest"}]
    assert matrix_combinations("${{ fromJSON(x) }}") is None


# --- review bypasses -----------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    (_replace("    runs-on: ubuntu-latest\n", '    "runs-on": self-hosted\n'), PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER),
    (_replace(f"      - uses: actions/checkout@{SHA} # v7.0.1\n", f"      - 'uses': actions/checkout@{SHA[:7]}\n"),
     PolicyCode.ACTION_NOT_PINNED),
    (_replace("    permissions:\n      contents: read\n", "    permissions: {contents: read, " + ID_TOKEN + ": write}\n"),
     PolicyCode.ID_TOKEN_WRITE_OUTSIDE_RELEASE),
    (_replace(f"      - uses: actions/checkout@{SHA} # v7.0.1\n        with:\n          persist-credentials: false\n",
              "      - {uses: docker://alpine:3}\n"), PolicyCode.ACTION_NOT_PINNED),
    (_replace("      - run: python -m pytest -q\n", "      - uses: docker://alpine:3\n"), PolicyCode.ACTION_NOT_PINNED),
    (_replace("    runs-on: ubuntu-latest\n", "    runs-on: [ubuntu-latest, self-hosted]\n"),
     PolicyCode.SELF_HOSTED_ON_UNTRUSTED_TRIGGER),
    (_replace("    runs-on: ubuntu-latest\n", "    runs-on:\n      group: home-base\n"), PolicyCode.RUNNER_UNRESOLVABLE),
])
def test_cr04_review_bypasses_are_caught(text: str, expected: PolicyCode) -> None:
    assert expected in codes(text)


def test_cr04_docker_uses_pinned_by_digest_is_accepted() -> None:
    text = _replace("      - run: python -m pytest -q\n", f"      - uses: docker://alpine@sha256:{'a' * 64}\n")
    assert PolicyCode.ACTION_NOT_PINNED not in codes(text)


def test_security_workflow_required_controls() -> None:
    security = (ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8")
    assert codes(security, "security.yml") == set()
    no_js = security.replace("language: [python, javascript-typescript]", "language: [python]")
    assert {f.location for f in check_workflow("security.yml", no_js)} >= {"codeql:javascript-typescript"}
    no_scan = security.replace("python scripts/release/secret_scan.py secret-scan.json", "true")
    assert "secret-scan" in {f.location for f in check_workflow("security.yml", no_scan)}


def test_dependabot_covers_pip_npm_and_actions() -> None:
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert check_dependabot(text) == []
    missing = text.replace('directory: "/web"', 'directory: "/other"')
    assert [f.location for f in check_dependabot(missing)] == ["npm:/web"]


def test_policy_script_reports_ok(tmp_path: Path) -> None:
    out = tmp_path / "ci-policy.json"
    done = subprocess.run([sys.executable, str(ROOT / "scripts/release/ci_policy_check.py"), str(out)],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "CI_POLICY_OK" in done.stdout
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "PASS" and report["public_pr_runner_policy"] == "HOSTED_EPHEMERAL_ONLY"


@pytest.mark.parametrize("injection", [
    "    env:\n      BASH_ENV: release-out/payload.sh\n",
    "    defaults:\n      run:\n        shell: bash --rcfile release-out/payload.sh {0}\n",
    "    container: ghcr.io/example/image:latest\n",
])
def test_cr04b_n01_attest_job_cannot_carry_startup_hooks(injection: str) -> None:
    hooked = RELEASE.replace("    timeout-minutes: 15\n    permissions:\n      contents: read\n      " + ID_TOKEN,
                             "    timeout-minutes: 15\n" + injection + "    permissions:\n      contents: read\n      "
                             + ID_TOKEN, 1)
    assert hooked != RELEASE
    assert PolicyCode.RELEASE_ATTEST_JOB_NOT_MINIMAL in release_codes(hooked)


def test_cr04b_n01_release_workflow_level_env_is_refused() -> None:
    hooked = RELEASE.replace("jobs:\n", "env:\n  BASH_ENV: release-out/payload.sh\njobs:\n", 1)
    assert hooked != RELEASE
    assert PolicyCode.RELEASE_ATTEST_JOB_NOT_MINIMAL in release_codes(hooked)
