"""Security Hardening V1: static checks that CI/CD cannot write PADOS semantic authority and that
public pull requests cannot reach personal infrastructure or secrets.

These are invariants over the checked-in workflow and release tooling. The Secure CI/CD agent owns
those files; these tests are the acceptance gate the integrator re-runs after merging them.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
# Modules whose import gives write access to semantic state, approvals, memory authority or the audit chain.
AUTHORITY_MODULES = (
    "pados.state_proof", "pados.product_loop.state", "pados.memory.service", "pados.memory.repository",
    "pados.control.service", "pados.remote.live", "pados.project_goal",
)
AUTHORITY_CALLS = ("commit_result", "decide_approval", "promote_memory", "create_approval", "append_audit")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _triggers(text: str) -> set[str]:
    match = re.search(r"^on:\s*(.+?)(?=^\S)", text + "\nend:", re.MULTILINE | re.DOTALL)
    body = match.group(1) if match else ""
    return set(re.findall(r"[a-z_]+", body)) & {
        "push", "pull_request", "pull_request_target", "workflow_run", "issue_comment", "workflow_dispatch",
        "release", "schedule"}


def test_workflows_exist_and_have_read_only_default_token() -> None:
    assert WORKFLOWS, "no CI workflow found"
    for path in WORKFLOWS:
        text = _text(path)
        assert re.search(r"^permissions:\s*\n\s+contents:\s*read\s*$", text, re.MULTILINE) or re.search(
            r"^permissions:\s*(read-all|\{\s*\}|\{\s*contents:\s*read\s*\})\s*$", text, re.MULTILINE), path.name
        assert "write-all" not in text, path.name


def test_public_pull_requests_cannot_reach_personal_infrastructure_or_secrets() -> None:
    for path in WORKFLOWS:
        text = _text(path)
        triggers = _triggers(text)
        assert "pull_request_target" not in triggers, f"{path.name}: pull_request_target runs fork code with secrets"
        assert "workflow_run" not in triggers or "pull_request" not in text, path.name
        if "pull_request" in triggers:
            assert not re.search(r"runs-on:.*self-hosted", text), f"{path.name}: PR code on a personal runner"
            assert "${{ secrets." not in text, f"{path.name}: secrets exposed to PR-triggered jobs"
            assert not re.search(r"\b(ssh|scp|rsync)\s", text), f"{path.name}: PR job opens a remote shell"
            for host in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
                assert host.startswith("127."), f"{path.name}: PR job addresses {host}"


def test_ci_cannot_invoke_pados_semantic_authority() -> None:
    for path in WORKFLOWS:
        steps = "\n".join(line for line in _text(path).splitlines() if line.strip().startswith(("- run:", "run:")))
        assert not re.search(r"\bpados\s+(approve|commit|promote|remote|node\s+serve|loop|goal)\b", steps), path.name
        for call in AUTHORITY_CALLS:
            assert call not in steps, (path.name, call)


def test_release_and_ci_scripts_do_not_import_authority_modules() -> None:
    scripts = sorted((ROOT / "scripts" / "release").glob("*.py"))
    package = ROOT / "src" / "pados" / "release"
    scripts += sorted(package.glob("*.py")) if package.is_dir() else []
    assert scripts
    for script in scripts:
        text = _text(script)
        for module in AUTHORITY_MODULES:
            assert not re.search(rf"^\s*(from|import)\s+{re.escape(module)}\b", text, re.MULTILINE), (
                script.name, module)
        for call in AUTHORITY_CALLS:
            assert f"{call}(" not in text, (script.name, call)
