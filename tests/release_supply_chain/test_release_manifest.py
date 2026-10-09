"""Release manifest, in-toto/SLSA provenance, migration heads and reproducible bundles."""
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

import pytest

from pados.config import OPERATIONAL_REVISION_DEFAULT, PROOF_REVISION_HEAD
from pados.release.supply_chain import (
    POSTURE_TARGET,
    SLSA_PROVENANCE_PREDICATE,
    STATEMENT_TYPE,
    UNSIGNED_LOCAL_PROVENANCE,
    BuildInvocation,
    SourceIdentity,
    ArtifactKind,
    SupplyChainError,
    artifact_kind,
    collect_release_files,
    deterministic_tar_gz,
    invocation_from_env,
    manifest_sha256,
    migration_heads,
    parse_build_environment,
    environment_inventory,
)
from pados.versions import PRODUCT_VERSION, version_report
from tests.windows_links import create_link_or_skip

from .support import BUILD_ENVIRONMENT, COMMIT, ROOT, TREE, build_release_dir, sbom_documents


def test_manifest_binds_version_source_artifacts_sbom_and_provenance(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path)
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest == metadata.manifest
    assert manifest["schema"] == "pados.release-manifest/v1"
    assert manifest["product"] == {"name": "pados", "version": PRODUCT_VERSION, "channel": "alpha"}
    assert manifest["source"]["git_commit"] == COMMIT and manifest["source"]["git_tree"] == TREE
    kinds = {a["kind"]: a for a in manifest["artifacts"]}
    assert set(kinds) == {"WHEEL", "SDIST", "WEB_BUNDLE"}
    assert kinds["WHEEL"]["sha256"] == hashlib.sha256(b"wheel-bytes").hexdigest()
    cdx = next(s for s in manifest["sbom"] if s["name"] == "sbom.cdx.json")
    assert cdx["sha256"] == hashlib.sha256((release / "sbom.cdx.json").read_bytes()).hexdigest()
    assert cdx["component_count"] == len(json.loads((release / "sbom.cdx.json").read_text())["components"])
    statement = manifest["provenance"]["statement"]
    assert statement["signing"] == UNSIGNED_LOCAL_PROVENANCE
    assert statement["sha256"] == hashlib.sha256((release / "provenance.intoto.json").read_bytes()).hexdigest()
    assert manifest["provenance"]["attestation"] == "SEPARATE_BUNDLE_OPERATOR_VERIFIED"
    assert manifest["build_environment"] == {
        "name": "build-environment.txt", "sha256": hashlib.sha256(BUILD_ENVIRONMENT).hexdigest(),
        "size": len(BUILD_ENVIRONMENT), "package_count": 3}
    assert manifest["posture"] == {"target": POSTURE_TARGET, "certified": False}
    assert manifest["authority"]["deployment_authorized"] is False
    assert manifest["authority"]["ci_is_pados_authority"] is False
    assert manifest["versions"] == version_report()
    assert metadata.manifest_sha256 == manifest_sha256(manifest)


def test_provenance_is_in_toto_v1_with_slsa_v1_predicate(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path)
    statement = json.loads((release / "provenance.intoto.json").read_text(encoding="utf-8"))
    assert statement["_type"] == STATEMENT_TYPE == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == SLSA_PROVENANCE_PREDICATE == "https://slsa.dev/provenance/v1"
    subjects = {s["name"]: s["digest"]["sha256"] for s in statement["subject"]}
    manifest = metadata.manifest
    assert subjects == {e["name"]: e["sha256"] for e in manifest["artifacts"] + manifest["sbom"]}
    definition = statement["predicate"]["buildDefinition"]
    assert definition["internalParameters"]["pados_signing"] == UNSIGNED_LOCAL_PROVENANCE
    assert definition["internalParameters"]["git_tree"] == TREE
    assert definition["resolvedDependencies"][0]["digest"] == {"gitCommit": COMMIT}
    locks = {d["uri"] for d in definition["resolvedDependencies"][1:]}
    assert locks == {"file:requirements.lock", "file:web/package-lock.json", "file:build-environment.txt"}
    assert statement["predicate"]["runDetails"]["builder"]["id"].startswith("https://pados.invalid/builders/")


def test_cr04_sc07_build_environment_is_strict_and_bound(tmp_path: Path) -> None:
    assert parse_build_environment("# header\nbuild==1.6.1\nwheel==0.48.0\n") == [("build", "1.6.1"), ("wheel", "0.48.0")]
    for bad in ("-e git+https://example.invalid/x#egg=x\n", "pkg @ file:///tmp/x\n", "\n"):
        with pytest.raises(SupplyChainError, match="BUILD_ENVIRONMENT_INVALID"):
            parse_build_environment(bad)
    cyclonedx, _ = sbom_documents()
    properties = {p["name"]: p["value"] for p in cyclonedx["metadata"]["properties"]}
    assert properties["pados:source_sha256:build-environment.txt"] == hashlib.sha256(BUILD_ENVIRONMENT).hexdigest()
    assert "build" in environment_inventory() or "pip" in environment_inventory()


def test_cr04_sc07_build_requirements_are_exact_pins() -> None:
    lines = [line for line in (ROOT / "scripts/release/build-requirements.lock").read_text().splitlines()
             if line and not line.startswith("#")]
    assert {line.split("==")[0] for line in lines} == {"build", "pyproject_hooks", "setuptools", "wheel"}
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9.]*", line) for line in lines)


def test_migration_heads_are_read_offline() -> None:
    heads = migration_heads(ROOT)
    assert heads["operational"]["config"] == "alembic.ini"
    assert OPERATIONAL_REVISION_DEFAULT in heads["operational"]["heads"]
    assert heads["proof"]["heads"] == [PROOF_REVISION_HEAD]


def test_unknown_or_symlinked_release_files_fail_closed(tmp_path: Path) -> None:
    (tmp_path / f"pados-{PRODUCT_VERSION}.tar.gz").write_bytes(b"x")
    (tmp_path / "sbom.cdx.json").write_text("{}")
    (tmp_path / "sbom.spdx.json").write_text("{}")
    (tmp_path / "build-environment.txt").write_bytes(BUILD_ENVIRONMENT)
    assert len(collect_release_files(tmp_path, PRODUCT_VERSION)[0]) == 1
    (tmp_path / "notes.txt").write_text("unexpected")
    with pytest.raises(SupplyChainError, match="UNEXPECTED_RELEASE_FILE"):
        collect_release_files(tmp_path, PRODUCT_VERSION)
    (tmp_path / "notes.txt").unlink()
    (tmp_path / "pados-9.9.9.tar.gz").write_bytes(b"other version")
    with pytest.raises(SupplyChainError, match="UNEXPECTED_RELEASE_FILE"):
        collect_release_files(tmp_path, PRODUCT_VERSION)
    (tmp_path / "pados-9.9.9.tar.gz").unlink()
    (tmp_path / "sbom.spdx.json").unlink()
    create_link_or_skip(tmp_path / "sbom.cdx.json", tmp_path / "sbom.spdx.json", directory=False)
    with pytest.raises(SupplyChainError, match="RELEASE_FILE_NOT_REGULAR"):
        collect_release_files(tmp_path, PRODUCT_VERSION)
    (tmp_path / "sbom.spdx.json").unlink()
    with pytest.raises(SupplyChainError, match="SBOM_MISSING"):
        collect_release_files(tmp_path, PRODUCT_VERSION)
    (tmp_path / "sbom.spdx.json").write_text("{}")
    (tmp_path / "build-environment.txt").unlink()
    with pytest.raises(SupplyChainError, match="BUILD_ENVIRONMENT_MISSING"):
        collect_release_files(tmp_path, PRODUCT_VERSION)


def test_cr04_sc08_artifact_names_are_tied_to_version() -> None:
    v = PRODUCT_VERSION
    assert artifact_kind(f"pados-{v}-py3-none-any.whl", v) is ArtifactKind.WHEEL
    assert artifact_kind(f"pados-{v}.tar.gz", v) is ArtifactKind.SDIST
    assert artifact_kind(f"pados-web-{v}.tar.gz", v) is ArtifactKind.WEB_BUNDLE
    for name in (f"pados-{v}x.tar.gz", "pados-0.0.1.tar.gz", f"evil-{v}.whl", f"pados-{v}-py3.whl", "../x.whl"):
        assert artifact_kind(name, v) is None


def test_source_identity_requires_full_object_ids() -> None:
    with pytest.raises(SupplyChainError, match="GIT_OBJECT_ID_INVALID"):
        SourceIdentity("abc", TREE, False)


def test_github_invocation_identity() -> None:
    env = {"GITHUB_ACTIONS": "true", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/pados",
           "GITHUB_WORKFLOW_REF": "o/pados/.github/workflows/release-candidate.yml@refs/tags/v1.0.0",
           "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REF": "refs/tags/v1.0.0"}
    invocation = invocation_from_env(env, started_on="s", finished_on="f")
    assert invocation.builder_id == ("https://github.com/o/pados/.github/workflows/release-candidate.yml"
                                     "@refs/tags/v1.0.0")
    assert invocation.workflow is not None and invocation.workflow["path"] == ".github/workflows/release-candidate.yml"
    assert invocation_from_env({}, started_on="s", finished_on="f") == BuildInvocation(started_on="s", finished_on="f")


def test_web_bundle_is_byte_reproducible(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    first = deterministic_tar_gz(dist, prefix="pados-web-1", mtime=1_700_000_000)
    (dist / "index.html").touch()
    second = deterministic_tar_gz(dist, prefix="pados-web-1", mtime=1_700_000_000)
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first)) as archive:
        members = archive.getmembers()
    assert [m.name for m in members] == ["pados-web-1/assets", "pados-web-1/assets/app.js", "pados-web-1/index.html"]
    assert {(m.uid, m.gid, m.mtime) for m in members} == {(0, 0, 1_700_000_000)}
    create_link_or_skip(dist / "index.html", dist / "link", directory=False)
    with pytest.raises(SupplyChainError, match="BUNDLE_SYMLINK_REJECTED"):
        deterministic_tar_gz(dist, prefix="pados-web-1", mtime=0)
