"""CI/CD V1.1: PR CI / RC CI separation, RC-only packaging, artifact-only live acceptance, and tool fixes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from pados.release.workflow_policy import check_workflow, triggers
from pados.release.yaml_subset import load

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
RC_EVENT_GUARD = ("github.event_name == 'workflow_dispatch' || (github.event_name == 'push' && "
                  "startsWith(github.ref, 'refs/heads/rc/'))")


def _module(path: Path) -> ModuleType:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workflow(name: str) -> dict[str, Any]:
    document = load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


# --- PR CI / RC CI boundary ----------------------------------------------------------------------------------

def test_pr_ci_never_packages_and_never_runs_the_rc_only_gates() -> None:
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    document = _workflow("ci.yml")
    assert "pull_request" in triggers(document)
    assert document["on"]["push"] == {"branches-ignore": ["rc/**"]}
    for job in document["jobs"].values():
        assert "uses" not in job, "PR CI calls no reusable workflow (in particular never packaging.yml)"
    assert "packaging.yml" not in text.split("jobs:", 1)[1]
    assert "postgres_integration_gate.py" not in text and "--fresh-install" not in text
    assert "package_provenance.py" not in text and "build_runtime.py" not in text


def test_rc_ci_runs_only_on_rc_events_and_packages_after_every_gate() -> None:
    document = _workflow("rc-ci.yml")
    assert set(triggers(document)) == {"workflow_dispatch", "push"}
    assert document["on"]["push"] == {"branches": ["rc/**"]}
    jobs = document["jobs"]
    assert jobs["checks"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["packaging"]["uses"] == "./.github/workflows/packaging.yml"
    assert set(jobs["packaging"]["needs"]) == {"checks", "postgres-integration", "fresh-install-migration"}
    assert jobs["rc-manifest"]["needs"] == ["packaging"]
    rc_text = (WORKFLOWS / "rc-ci.yml").read_text(encoding="utf-8")
    assert "postgres_integration_gate.py" in rc_text and "migration_gate.py" in rc_text and "--fresh-install" in rc_text
    assert "scripts/release/rc_manifest.py write" in rc_text and "name: pados-rc-artifact" in rc_text


def test_packaging_is_callable_only_and_refuses_non_rc_events() -> None:
    document = _workflow("packaging.yml")
    assert set(triggers(document)) == {"workflow_call"}
    for job_id, job in document["jobs"].items():
        if job_id == "macos-arm64":
            assert job["if"] is False or str(job["if"]).lower() == "false"
            continue
        assert job["if"] == RC_EVENT_GUARD, job_id
    callers = sorted(path.name for path in WORKFLOWS.glob("*.yml")
                     if "./.github/workflows/packaging.yml" in path.read_text(encoding="utf-8"))
    assert callers == ["rc-ci.yml"]


def test_packaging_and_rc_ci_cannot_publish_or_write() -> None:
    for name in ("packaging.yml", "rc-ci.yml", "ci.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert check_workflow(name, text, root=ROOT) == []
        for job in _workflow(name)["jobs"].values():
            assert all(level != "write" for level in (job.get("permissions") or {}).values())
        for forbidden in ("npm publish", "twine upload", "gh release", "git push", "secrets."):
            assert forbidden not in text


def test_pr_ci_success_is_never_a_deployment_authorization() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        assert "workflow_run" not in triggers(load(path.read_text(encoding="utf-8")))
    release = _workflow("release-candidate.yml")
    assert set(triggers(release)) == {"workflow_dispatch", "push"}
    assert release["on"]["push"] == {"tags": ["v*"]}
    deploy_verify = (ROOT / "src/pados/release/deploy_verify.py").read_text(encoding="utf-8")
    assert "CI_CANNOT_AUTHORIZE_DEPLOYMENT" in deploy_verify


# --- Tool fixes ------------------------------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_sha256sums_are_lf_and_crlf_manifests_still_verify(tmp_path: Path) -> None:
    provenance = _module(ROOT / "scripts/release/package_provenance.py")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "pkg-1.0.0.tgz").write_bytes(b"package bytes")
    commit, tree = _git(ROOT, "rev-parse", "HEAD"), _git(ROOT, "rev-parse", "HEAD^{tree}")
    provenance.source_identity = lambda expected: {"source_commit": expected, "source_tree": tree}
    provenance.record("win32-x64", dist, commit)
    sums = (dist / "SHA256SUMS-win32-x64.txt").read_bytes()
    assert b"\r" not in sums and sums == f"{hashlib.sha256(b'package bytes').hexdigest()}  pkg-1.0.0.tgz\n".encode()
    assert b"\r" not in (dist / "provenance-win32-x64.json").read_bytes()
    (dist / "SHA256SUMS-win32-x64.txt").write_bytes(sums.replace(b"\n", b"\r\n"))
    assert provenance.verify("win32-x64", dist, commit, tree)["SOURCE_COMMIT"] == commit
    (dist / "pkg-1.0.0.tgz").write_bytes(b"tampered")
    with pytest.raises(provenance.ProvenanceError):
        provenance.verify("win32-x64", dist, commit, tree)


@pytest.fixture(scope="module")
def live() -> ModuleType:
    return _module(ROOT / "scripts/acceptance/live_acceptance.py")


def test_chat_model_is_the_profile_runtime_tag_not_the_catalog_id(live: ModuleType, tmp_path: Path) -> None:
    environment = live.chat_environment("qwen3:8b", tmp_path / "catalog.json", tmp_path / "nodes.json")
    assert environment["PADOS_CHAT_MODEL"] == "qwen3:8b"
    assert "@sha256:" not in environment["PADOS_CHAT_MODEL"]
    source = (ROOT / "scripts/acceptance/live_acceptance.py").read_text(encoding="utf-8")
    assert "chat_environment(runtime_tag" in source and "runtime_tag = profile.artifact.artifact_ref" in source


def test_d2_evidence_is_read_from_the_cognitive_response_shape(live: ModuleType) -> None:
    evidence = {"execution": {"context_window": 8192, "reasoning_mode": "DISABLED"}}
    from_outputs = {"status": "ok", "work_item": {"candidate_outputs": [{"measured_execution": evidence}]},
                    "candidate_nodes": []}
    from_nodes = {"status": "ok", "work_item": {},
                  "candidate_nodes": [{"metadata_json": {"measured_execution": evidence}}]}
    assert live.find_measured_execution(from_outputs) == evidence
    assert live.find_measured_execution(from_nodes) == evidence
    assert live.find_measured_execution({"status": "ok", "created": [{"metadata_json": {}}]}) is None
    assert live.find_measured_execution("not json") is None


def test_runtime_identity_reads_the_source_block(live: ModuleType) -> None:
    manifest = {"schema": "pados.runtime-manifest.v1", "platform": "win32-x64",
                "package": "@pados-ai/runtime-win32-x64", "package_version": "0.1.0-alpha.1",
                "source": {"commit": "a" * 40, "dirty": False, "files": 402}}
    assert live.runtime_source_identity(manifest) == {
        "package": "@pados-ai/runtime-win32-x64", "version": "0.1.0-alpha.1", "platform": "win32-x64",
        "commit": "a" * 40, "dirty": False}


def test_live_acceptance_accepts_only_the_rc_artifact(live: ModuleType) -> None:
    runtime = {"commit": "a" * 40, "dirty": False, "platform": "win32-x64", "version": "0.1.0-alpha.1"}
    manifest = {"schema": "pados.rc-manifest.v1", "RC_COMMIT": "a" * 40, "RC_TREE": "b" * 40,
                "platforms": {"win32-x64": {"packages": [
                    {"file": "pados-ai-runtime-win32-x64-0.1.0-alpha.1.tgz"}]}}}
    assert live.rc_identity_check(runtime, manifest) == []
    assert "RUNTIME_NOT_RC_COMMIT" in live.rc_identity_check({**runtime, "commit": "c" * 40}, manifest)
    assert "RUNTIME_DIRTY_OR_UNKNOWN" in live.rc_identity_check({**runtime, "dirty": True}, manifest)
    assert "RUNTIME_PACKAGE_NOT_IN_RC" in live.rc_identity_check({**runtime, "version": "9.9.9"}, manifest)
    assert "RC_MANIFEST_INVALID" in live.rc_identity_check(runtime, {**manifest, "schema": "x"})
    source = (ROOT / "scripts/acceptance/live_acceptance.py").read_text(encoding="utf-8")
    assert "run needs --rc-manifest" in source


def _rc_artifact(tmp_path: Path) -> tuple[Path, str, str, ModuleType]:
    provenance = _module(ROOT / "scripts/release/package_provenance.py")
    commit, tree = _git(ROOT, "rev-parse", "HEAD"), _git(ROOT, "rev-parse", "HEAD^{tree}")
    provenance.source_identity = lambda expected: {"source_commit": expected, "source_tree": tree}
    rc = tmp_path / "rc"
    packages = []
    for platform in ("linux-x64", "win32-x64"):
        directory = rc / platform
        directory.mkdir(parents=True)
        (directory / f"pados-ai-runtime-{platform}-0.1.0-alpha.1.tgz").write_bytes(platform.encode())
        packages.append((platform, provenance.record(platform, directory, commit)["PACKAGES"]))
    manifest = {"schema": "pados.rc-manifest.v1", "RC_COMMIT": commit, "RC_TREE": tree,
                "platforms": {platform: {"packages": items} for platform, items in packages}}
    (rc / "rc-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return rc, commit, tree, provenance


def test_promotion_requires_rc_evidence_and_live_acceptance_of_the_same_commit(tmp_path: Path) -> None:
    rc, commit, tree, _ = _rc_artifact(tmp_path)
    promotion = _module(ROOT / "scripts/release/promotion_check.py")
    summary = {"status": "PASS", "gates": {"d1": "PASS", "d2": "PASS", "d4": "PASS"},
               "rc": {"RC_COMMIT": commit, "RC_TREE": tree},
               "runtime": {"source": {"commit": commit, "dirty": False}}}
    live_file = tmp_path / "live.json"
    live_file.write_text(json.dumps(summary), encoding="utf-8")
    assert promotion.problems(rc, live_file) == []
    for broken, code in (({**summary, "status": "FAIL"}, "LIVE_ACCEPTANCE_NOT_PASS"),
                         ({**summary, "gates": {"d1": "PASS", "d2": "PASS"}}, "LIVE_GATE_MISSING_OR_FAILED:d4"),
                         ({**summary, "rc": {"RC_COMMIT": "0" * 40, "RC_TREE": tree}}, "LIVE_ACCEPTANCE_FOR_ANOTHER_RC")):
        live_file.write_text(json.dumps(broken), encoding="utf-8")
        assert code in promotion.problems(rc, live_file)
    live_file.write_text(json.dumps(summary), encoding="utf-8")
    (rc / "win32-x64" / "pados-ai-runtime-win32-x64-0.1.0-alpha.1.tgz").write_bytes(b"tampered")
    assert any(item.startswith("RC_ARTIFACT_INVALID") for item in promotion.problems(rc, live_file))
    source = (ROOT / "scripts/release/promotion_check.py").read_text(encoding="utf-8")
    for capability in ("subprocess", "urllib", "requests", "npm publish", "twine"):
        assert capability not in source  # advice only: no way to publish, deploy or call out


def test_rc_manifest_refuses_non_rc_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, commit, _, _ = _rc_artifact(tmp_path)
    manifest_tool = _module(ROOT / "scripts/release/rc_manifest.py")
    monkeypatch.setenv("RC_EVENT", "pull_request")
    monkeypatch.setenv("RC_REF", "refs/pull/1/merge")
    with pytest.raises(manifest_tool.ProvenanceError, match="NOT_AN_RC_EVENT"):
        manifest_tool.write(rc, commit)
    monkeypatch.setenv("RC_EVENT", "push")
    monkeypatch.setenv("RC_REF", "refs/heads/feature/x")
    with pytest.raises(manifest_tool.ProvenanceError, match="NOT_AN_RC_EVENT"):
        manifest_tool.write(rc, commit)
