# SPDX-License-Identifier: AGPL-3.0-only
"""Release manifest and provenance for a PADOS release candidate.

A release manifest ties the product version, git commit and tree, the SHA-256 of
every release artifact (wheel, sdist, web bundle), the SBOM hashes, an in-toto
v1 Statement with a SLSA provenance v1 predicate, the offline alembic migration
heads and the protocol/schema versions.

Authority boundary: the manifest is release evidence produced by a build. It
never authorizes deployment, never writes PADOS semantic state, ScopeVersion,
CAS, memory or audit authority. CI IS NOT PADOS AUTHORITY; BUILD SUCCESS !=
TRUST. The provenance statement written here is UNSIGNED_LOCAL_PROVENANCE. The
signed GitHub artifact attestation is produced by a separate minimal job and
shipped as a separate bundle; the operator verifies it out of band
(``gh attestation verify``) and records its digest in the pin. The build job's
actual ``pip freeze`` inventory (``build-environment.txt``) is bound into the
manifest and provenance. The posture is a target
(SLSA_L2_STYLE_TARGET); no certification is claimed.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_no_credentials
from pados.lifecycle.retention import DataKind

MANIFEST_SCHEMA = "pados.release-manifest/v1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
SLSA_PROVENANCE_PREDICATE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "https://pados.invalid/release-candidate/build/v1"
LOCAL_BUILDER_ID = "https://pados.invalid/builders/local-unsigned/v1"
UNSIGNED_LOCAL_PROVENANCE = "UNSIGNED_LOCAL_PROVENANCE"
GITHUB_ARTIFACT_ATTESTATION = "GITHUB_ARTIFACT_ATTESTATION"
ATTESTATION_HANDLING = "SEPARATE_BUNDLE_OPERATOR_VERIFIED"
POSTURE_TARGET = "SLSA_L2_STYLE_TARGET"
DEPLOYMENT_REQUIREMENT = "HOME_BASE_PULL_VERIFICATION_AND_HUMAN_APPROVAL"
RELEASE_DATA_KIND = DataKind.RELEASE_TEST_EVIDENCE

MANIFEST_FILE = "release-manifest.json"
PROVENANCE_FILE = "provenance.intoto.json"
SBOM_CYCLONEDX_FILE = "sbom.cdx.json"
SBOM_SPDX_FILE = "sbom.spdx.json"
ATTESTATION_FILE = "attestation.sigstore.json"
BUILD_ENVIRONMENT_FILE = "build-environment.txt"
SBOM_FORMATS = {SBOM_CYCLONEDX_FILE: "CycloneDX-1.5", SBOM_SPDX_FILE: "SPDX-2.3"}
GIT_OBJECT = re.compile(r"[0-9a-f]{40}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
MIGRATION_CONFIGS = {"operational": "alembic.ini", "proof": "alembic_p6_19b.ini"}


class ArtifactKind(StrEnum):
    WHEEL = "WHEEL"
    SDIST = "SDIST"
    WEB_BUNDLE = "WEB_BUNDLE"


class SupplyChainError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class SourceIdentity:
    commit: str
    tree: str
    dirty: bool
    repository: str = "git+local"
    ref: str | None = None

    def __post_init__(self) -> None:
        if not GIT_OBJECT.fullmatch(self.commit) or not GIT_OBJECT.fullmatch(self.tree):
            raise SupplyChainError("GIT_OBJECT_ID_INVALID")


@dataclass(frozen=True)
class BuildInvocation:
    builder_id: str = LOCAL_BUILDER_ID
    invocation_id: str = "local"
    started_on: str = ""
    finished_on: str = ""
    workflow: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ReleaseFile:
    name: str
    sha256: str
    size: int
    kind: ArtifactKind | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_kind(name: str, version: str) -> ArtifactKind | None:
    """The artifact kind ``name`` denotes for ``version``; ``None`` for any other name."""
    v = re.escape(version)
    rules = {ArtifactKind.WHEEL: rf"pados-{v}-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl",
             ArtifactKind.SDIST: rf"pados-{v}\.tar\.gz", ArtifactKind.WEB_BUNDLE: rf"pados-web-{v}\.tar\.gz"}
    return next((kind for kind, rule in rules.items() if re.fullmatch(rule, name)), None)


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SupplyChainError("RELEASE_FILE_NOT_REGULAR", path.name)


_FREEZE_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.!+_-]+)")


def parse_build_environment(text: str) -> list[tuple[str, str]]:
    """``pip freeze --all --exclude-editable`` inventory; any non ``name==version`` line fails closed."""
    pins = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _FREEZE_LINE.fullmatch(line)
        if match is None:
            raise SupplyChainError("BUILD_ENVIRONMENT_INVALID", f"line {number}")
        pins.append((match.group(1), match.group(2)))
    if not pins:
        raise SupplyChainError("BUILD_ENVIRONMENT_INVALID", "empty")
    return pins


def environment_inventory() -> str:
    """The running interpreter's installed distributions as ``name==version`` lines (excluding pados)."""
    from importlib import metadata

    names = {(d.metadata["Name"], d.version) for d in metadata.distributions() if d.metadata["Name"]}
    return "".join(f"{n}=={v}\n" for n, v in sorted(names, key=lambda item: item[0].lower()) if n != "pados")


def collect_release_files(release_dir: Path, version: str) -> tuple[list[ReleaseFile], list[ReleaseFile],
                                                                   ReleaseFile]:
    """(artifacts, sboms, build environment) in ``release_dir``; unknown files fail closed."""
    artifacts: list[ReleaseFile] = []
    sboms: list[ReleaseFile] = []
    environment: ReleaseFile | None = None
    for path in sorted(release_dir.iterdir()):
        if path.name in {MANIFEST_FILE, PROVENANCE_FILE}:
            continue
        _regular_file(path)
        record = ReleaseFile(path.name, sha256_file(path), path.stat().st_size)
        if path.name in SBOM_FORMATS:
            sboms.append(record)
        elif path.name == BUILD_ENVIRONMENT_FILE:
            environment = record
        else:
            kind = artifact_kind(path.name, version)
            if kind is None:
                raise SupplyChainError("UNEXPECTED_RELEASE_FILE", path.name)
            if any(a.kind is kind for a in artifacts):
                raise SupplyChainError("DUPLICATE_ARTIFACT_KIND", path.name)
            artifacts.append(ReleaseFile(record.name, record.sha256, record.size, kind))
    if not {a.kind for a in artifacts} & {ArtifactKind.WHEEL, ArtifactKind.SDIST}:
        raise SupplyChainError("NO_PYTHON_DISTRIBUTION")
    missing = set(SBOM_FORMATS) - {s.name for s in sboms}
    if missing:
        raise SupplyChainError("SBOM_MISSING", sorted(missing)[0])
    if environment is None:
        raise SupplyChainError("BUILD_ENVIRONMENT_MISSING", BUILD_ENVIRONMENT_FILE)
    return artifacts, sboms, environment


def migration_heads(project_root: Path) -> dict[str, Any]:
    """Alembic heads read offline from the migration scripts (no database connection)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    result: dict[str, Any] = {}
    for lane, config_name in MIGRATION_CONFIGS.items():
        config = Config(str(project_root / config_name))
        if config.get_main_option("path_separator") is None:
            config.set_main_option("path_separator", "os")
        location = config.get_main_option("script_location") or ""
        resolved = Path(location) if Path(location).is_absolute() else project_root / location
        config.set_main_option("script_location", str(resolved))
        script = ScriptDirectory.from_config(config)
        result[lane] = {"config": config_name,
                        "script_location": resolved.relative_to(project_root).as_posix(),
                        "heads": sorted(script.get_heads())}
    return result


def build_provenance(
    subjects: Sequence[ReleaseFile],
    *,
    source: SourceIdentity,
    invocation: BuildInvocation,
    lock_digests: Mapping[str, str],
) -> dict[str, Any]:
    """in-toto Statement v1 with a SLSA provenance v1 predicate (unsigned)."""
    if not subjects:
        raise SupplyChainError("PROVENANCE_REQUIRES_SUBJECTS")
    dependencies: list[dict[str, Any]] = [
        {"uri": f"{source.repository}@{source.ref or source.commit}", "digest": {"gitCommit": source.commit}}
    ]
    dependencies += [{"uri": f"file:{name}", "digest": {"sha256": digest}}
                     for name, digest in sorted(lock_digests.items())]
    external: dict[str, Any] = {"source": {"repository": source.repository, "ref": source.ref or ""}}
    if invocation.workflow:
        external["workflow"] = dict(sorted(invocation.workflow.items()))
    metadata: dict[str, Any] = {"invocationId": invocation.invocation_id}
    if invocation.started_on:
        metadata["startedOn"] = invocation.started_on
    if invocation.finished_on:
        metadata["finishedOn"] = invocation.finished_on
    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": s.name, "digest": {"sha256": s.sha256}} for s in sorted(subjects, key=lambda s: s.name)],
        "predicateType": SLSA_PROVENANCE_PREDICATE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": external,
                "internalParameters": {"pados_signing": UNSIGNED_LOCAL_PROVENANCE, "git_tree": source.tree,
                                       "source_dirty": source.dirty},
                "resolvedDependencies": dependencies,
            },
            "runDetails": {"builder": {"id": invocation.builder_id}, "metadata": metadata},
        },
    }


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """The value an operator pins: SHA-256 of the canonical manifest JSON."""
    return canonical_hash(dict(manifest))


def build_manifest(
    *,
    product: Mapping[str, str],
    source: SourceIdentity,
    artifacts: Sequence[ReleaseFile],
    sboms: Sequence[tuple[ReleaseFile, Mapping[str, Any]]],
    provenance_sha256: str,
    build_environment: tuple[ReleaseFile, int],
    migrations: Mapping[str, Any],
    versions: Mapping[str, Any],
    builder_id: str,
    created_at: str,
) -> dict[str, Any]:
    sbom_entries = []
    for record, document in sorted(sboms, key=lambda item: item[0].name):
        components = document.get("components") if record.name == SBOM_CYCLONEDX_FILE else document.get("packages")
        count = len(components) if isinstance(components, list) else 0
        if record.name == SBOM_SPDX_FILE:
            count -= 1  # the described root package
        sbom_entries.append({"name": record.name, "format": SBOM_FORMATS[record.name], "sha256": record.sha256,
                             "size": record.size, "component_count": count})
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "product": dict(product),
        "source": {"git_commit": source.commit, "git_tree": source.tree, "dirty": source.dirty,
                   "repository": source.repository, "ref": source.ref or ""},
        "artifacts": [{"name": a.name, "kind": str(a.kind), "sha256": a.sha256, "size": a.size}
                      for a in sorted(artifacts, key=lambda a: a.name)],
        "sbom": sbom_entries,
        "provenance": {
            "statement": {"name": PROVENANCE_FILE, "sha256": provenance_sha256, "statement_type": STATEMENT_TYPE,
                          "predicate_type": SLSA_PROVENANCE_PREDICATE, "signing": UNSIGNED_LOCAL_PROVENANCE},
            "attestation": ATTESTATION_HANDLING,
        },
        "build_environment": {"name": build_environment[0].name, "sha256": build_environment[0].sha256,
                              "size": build_environment[0].size, "package_count": build_environment[1]},
        "migrations": dict(migrations),
        "versions": dict(versions),
        "builder_id": builder_id,
        "posture": {"target": POSTURE_TARGET, "certified": False},
        "authority": {"ci_is_pados_authority": False, "deployment_authorized": False,
                      "deployment_requires": DEPLOYMENT_REQUIREMENT},
        "created_at": created_at,
    }
    assert_no_credentials(manifest)
    return manifest


def deterministic_tar_gz(source_dir: Path, *, prefix: str, mtime: int) -> bytes:
    """A byte-reproducible ``.tar.gz`` of ``source_dir`` (sorted, fixed owner/mode/mtime)."""
    if not source_dir.is_dir():
        raise SupplyChainError("BUNDLE_SOURCE_MISSING")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(source_dir.rglob("*"), key=lambda p: p.relative_to(source_dir).as_posix()):
            if path.is_symlink():
                raise SupplyChainError("BUNDLE_SYMLINK_REJECTED", path.name)
            info = tarfile.TarInfo(f"{prefix}/{path.relative_to(source_dir).as_posix()}")
            info.mtime, info.uid, info.gid, info.uname, info.gname = mtime, 0, 0, "", ""
            if path.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            else:
                data = path.read_bytes()
                info.size, info.mode = len(data), 0o644
                archive.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as compressed:
        compressed.write(raw.getvalue())
    return out.getvalue()


def _git(project_root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=project_root, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SupplyChainError("GIT_READ_FAILED", " ".join(args[:2]))
    return completed.stdout.strip()


def read_source_identity(project_root: Path, env: Mapping[str, str] | None = None) -> SourceIdentity:
    """Commit, tree and tracked-file cleanliness of ``project_root`` (read-only git)."""
    env = env or {}
    commit, tree = _git(project_root, "rev-parse", "HEAD", "HEAD^{tree}").split()
    dirty = bool(_git(project_root, "status", "--porcelain", "--untracked-files=no"))
    server, repository = env.get("GITHUB_SERVER_URL"), env.get("GITHUB_REPOSITORY")
    uri = f"git+{server}/{repository}" if server and repository else "git+local"
    return SourceIdentity(commit=commit, tree=tree, dirty=dirty, repository=uri, ref=env.get("GITHUB_REF"))


def build_timestamp(project_root: Path, env: Mapping[str, str] | None = None) -> str:
    """SOURCE_DATE_EPOCH when set, else the HEAD commit time (UTC, second precision)."""
    env = env or {}
    epoch = env.get("SOURCE_DATE_EPOCH") or _git(project_root, "log", "-1", "--format=%ct")
    return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def invocation_from_env(env: Mapping[str, str], *, started_on: str, finished_on: str) -> BuildInvocation:
    """GitHub Actions identity when running there; otherwise the local unsigned builder."""
    if env.get("GITHUB_ACTIONS") != "true":
        return BuildInvocation(started_on=started_on, finished_on=finished_on)
    server, repository = env.get("GITHUB_SERVER_URL", ""), env.get("GITHUB_REPOSITORY", "")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF", "")
    return BuildInvocation(
        builder_id=f"{server}/{workflow_ref}" if workflow_ref else f"{server}/{repository}/actions",
        invocation_id=f"{server}/{repository}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"
                      f"/attempts/{env.get('GITHUB_RUN_ATTEMPT', '')}",
        started_on=started_on, finished_on=finished_on,
        workflow={"ref": env.get("GITHUB_REF", ""), "repository": f"{server}/{repository}",
                  "path": workflow_ref.split("@", 1)[0].removeprefix(f"{repository}/")},
    )


@dataclass(frozen=True)
class ReleaseMetadata:
    manifest: dict[str, Any]
    manifest_sha256: str
    provenance_sha256: str
    manifest_path: Path
    provenance_path: Path


def _render(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def write_release_metadata(
    project_root: Path,
    release_dir: Path,
    *,
    source: SourceIdentity,
    invocation: BuildInvocation,
    created_at: str,
) -> ReleaseMetadata:
    """Write ``provenance.intoto.json`` and ``release-manifest.json`` into ``release_dir``."""
    from pados.versions import PRODUCT_VERSION, RELEASE_CHANNEL, version_report

    artifacts, sboms, environment = collect_release_files(release_dir, PRODUCT_VERSION)
    packages = parse_build_environment((release_dir / environment.name).read_text(encoding="utf-8"))
    lock_digests = {name: sha256_file(project_root / name) for name in ("requirements.lock", "web/package-lock.json")
                    if (project_root / name).is_file()}
    lock_digests[BUILD_ENVIRONMENT_FILE] = environment.sha256
    statement = build_provenance([*artifacts, *sboms], source=source, invocation=invocation,
                                 lock_digests=lock_digests)
    assert_no_credentials(statement)
    provenance_bytes = _render(statement)
    provenance_path = release_dir / PROVENANCE_FILE
    provenance_path.write_bytes(provenance_bytes)
    sbom_documents = [(record, json.loads((release_dir / record.name).read_text(encoding="utf-8")))
                      for record in sboms]
    manifest = build_manifest(
        product={"name": "pados", "version": PRODUCT_VERSION, "channel": RELEASE_CHANNEL},
        source=source, artifacts=artifacts, sboms=sbom_documents,
        provenance_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        build_environment=(environment, len(packages)),
        migrations=migration_heads(project_root), versions=version_report(),
        builder_id=invocation.builder_id, created_at=created_at,
    )
    manifest_path = release_dir / MANIFEST_FILE
    manifest_path.write_bytes(_render(manifest))
    return ReleaseMetadata(manifest, manifest_sha256(manifest), hashlib.sha256(provenance_bytes).hexdigest(),
                           manifest_path, provenance_path)
