"""The GitHub Pages deployment workflow is an inactive template (see deploy/github-pages/README.md).

It needs OIDC, the pages write scope and a deployment environment, which the Secure CI/CD policy refuses for
every workflow job except the release attestation job. It therefore must never sit in .github/workflows/
without an explicit, policy-recorded decision and a reviewed policy exception.
"""
from __future__ import annotations

import re
from pathlib import Path

from pados.release.workflow_policy import PolicyCode, check_workflow
from pados.release.yaml_subset import load
from pados.retrieval.indexer import is_secret_suspected

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy" / "github-pages" / "pages.workflow.yml.template"
WORKFLOWS = ROOT / ".github" / "workflows"
OIDC_PERMISSION = re.compile(r"^\s*id-" + "token" + r":\s*write\s*$")


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_pages_template_only_has_the_expected_oidc_permission_marker() -> None:
    lines = _template().splitlines()
    assert sum(1 for line in lines if OIDC_PERMISSION.fullmatch(line)) == 1
    without_oidc_permission = "\n".join(line for line in lines if not OIDC_PERMISSION.fullmatch(line))
    assert not is_secret_suspected(without_oidc_permission)


def test_pages_template_is_not_an_active_workflow() -> None:
    assert not (WORKFLOWS / "pages.yml").exists()
    assert TEMPLATE.suffix == ".template"
    assert WORKFLOWS.resolve() not in TEMPLATE.resolve().parents
    assert _template().startswith("# INACTIVE TEMPLATE")


def test_no_active_workflow_deploys_pages_or_requests_pages_write() -> None:
    for path in sorted(WORKFLOWS.glob("*")):
        text = path.read_text(encoding="utf-8")
        assert "deploy-pages" not in text, path.name
        assert "upload-pages-artifact" not in text, path.name
        assert not re.search(r"^\s*pages:\s*write\s*$", text, re.MULTILINE), path.name


def test_pages_template_keeps_its_manual_publish_gate() -> None:
    document = load(_template())
    dispatch = document["on"]["workflow_dispatch"]["inputs"]["publish"]
    assert dispatch["default"] is False and dispatch["type"] == "boolean"
    build, deploy = document["jobs"]["build"], document["jobs"]["deploy"]
    assert build["permissions"] == {"contents": "read"}
    assert deploy["if"] == ("github.event_name == 'workflow_dispatch' && inputs.publish"
                            " && github.ref == 'refs/heads/main'")
    assert "secrets." not in _template()


def test_activating_the_template_unchanged_is_refused_by_the_policy() -> None:
    codes = {finding.code for finding in check_workflow("pages.yml", _template(), root=ROOT)}
    assert {PolicyCode.ID_TOKEN_WRITE_OUTSIDE_RELEASE, PolicyCode.WRITE_PERMISSION_NOT_ALLOWED,
            PolicyCode.DEPLOYMENT_ENVIRONMENT_PROHIBITED, PolicyCode.DEPLOY_STEP_PROHIBITED} <= codes
