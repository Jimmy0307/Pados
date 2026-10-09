# SPDX-License-Identifier: AGPL-3.0-only
"""Pull-based release verification for the Home Base. Verification never deploys.

The Home Base explicitly fetches a release candidate into a local directory by
its own means; this module performs no network, SSH or GitHub access. It first
copies the directory into a Home Base-owned, read-only staging directory
(``pados.release.staging``: no-follow opens, hash-and-copy in one pass) and
verifies only the staged bytes: the strict manifest schema, every artifact and
SBOM hash, both SBOM documents and their consistency, the provenance statement,
the build-environment inventory, the exact file set and an operator pin
obtained out of band.

Outcomes: ``STAGED_VERIFIED`` (manifest digest pinned) is the only result
eligible for deployment readiness. A commit/tree-only pin yields
``STAGED_VERIFIED_COMMIT_PIN_ONLY``: strings were matched, but artifact bytes are
not bound to anything the operator trusts, so it never reaches readiness.
Unsigned local provenance never satisfies readiness on its own.

``deployment_readiness`` re-hashes the staged copy and asks a
``DeploymentApprovalAuthority`` (signed, bound, single-use approvals) before it
reports ``DEPLOYMENT_AUTHORIZED_BY_HUMAN``; ``consume_approval`` is the separate
explicit step that marks the approval used. Neither deploys. CI SUCCESS !=
DEPLOYMENT AUTHORIZATION: inside GitHub Actions every approval is refused with
CI_CANNOT_AUTHORIZE_DEPLOYMENT.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.release.approval import ApprovalCode, Binding, DeploymentApprovalAuthority, running_in_ci
from pados.release.staging import StagedRelease, StagingError, stage_release, staged_integrity_error
from pados.release.supply_chain import (
    ATTESTATION_FILE,
    ATTESTATION_HANDLING,
    BUILD_ENVIRONMENT_FILE,
    DEPLOYMENT_REQUIREMENT,
    GIT_OBJECT,
    MANIFEST_FILE,
    MANIFEST_SCHEMA,
    MIGRATION_CONFIGS,
    POSTURE_TARGET,
    PROVENANCE_FILE,
    SBOM_CYCLONEDX_FILE,
    SBOM_FORMATS,
    SBOM_SPDX_FILE,
    SHA256_HEX,
    SLSA_PROVENANCE_PREDICATE,
    STATEMENT_TYPE,
    UNSIGNED_LOCAL_PROVENANCE,
    ArtifactKind,
    SupplyChainError,
    artifact_kind,
    manifest_sha256,
    parse_build_environment,
)

MANIFEST_KEYS = frozenset({"schema", "product", "source", "artifacts", "sbom", "provenance", "build_environment",
                           "migrations", "versions", "builder_id", "posture", "authority", "created_at"})
VERSIONS_KEYS = frozenset({"product", "protocols", "schemas", "runtimes", "database"})
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_UUID_URN = re.compile(r"urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class VerificationStatus(StrEnum):
    STAGED_VERIFIED = "STAGED_VERIFIED"
    STAGED_VERIFIED_COMMIT_PIN_ONLY = "STAGED_VERIFIED_COMMIT_PIN_ONLY"
    REJECTED = "REJECTED"


class ReadinessStatus(StrEnum):
    DEPLOYMENT_AUTHORIZED_BY_HUMAN = "DEPLOYMENT_AUTHORIZED_BY_HUMAN"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"


class VerifyCode(StrEnum):
    STAGING_REFUSED = "STAGING_REFUSED"
    MANIFEST_MISSING = "MANIFEST_MISSING"
    MANIFEST_UNREADABLE = "MANIFEST_UNREADABLE"
    MANIFEST_SCHEMA_INVALID = "MANIFEST_SCHEMA_INVALID"
    SOURCE_TREE_DIRTY = "SOURCE_TREE_DIRTY"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    ARTIFACT_SIZE_MISMATCH = "ARTIFACT_SIZE_MISMATCH"
    SBOM_MISSING = "SBOM_MISSING"
    SBOM_HASH_MISMATCH = "SBOM_HASH_MISMATCH"
    SBOM_INVALID = "SBOM_INVALID"
    SBOM_COUNT_MISMATCH = "SBOM_COUNT_MISMATCH"
    SBOM_PACKAGE_SET_MISMATCH = "SBOM_PACKAGE_SET_MISMATCH"
    BUILD_ENVIRONMENT_MISMATCH = "BUILD_ENVIRONMENT_MISMATCH"
    PROVENANCE_MISSING = "PROVENANCE_MISSING"
    PROVENANCE_HASH_MISMATCH = "PROVENANCE_HASH_MISMATCH"
    PROVENANCE_INVALID = "PROVENANCE_INVALID"
    PROVENANCE_SUBJECT_MISMATCH = "PROVENANCE_SUBJECT_MISMATCH"
    COMMIT_TREE_INCONSISTENT = "COMMIT_TREE_INCONSISTENT"
    ATTESTATION_PIN_MISMATCH = "ATTESTATION_PIN_MISMATCH"
    UNRECORDED_FILE_IN_RELEASE = "UNRECORDED_FILE_IN_RELEASE"
    OPERATOR_PIN_REQUIRED = "OPERATOR_PIN_REQUIRED"
    PIN_MANIFEST_HASH_MISMATCH = "PIN_MANIFEST_HASH_MISMATCH"
    PIN_COMMIT_MISMATCH = "PIN_COMMIT_MISMATCH"
    PIN_TREE_MISMATCH = "PIN_TREE_MISMATCH"
    PIN_VERSION_MISMATCH = "PIN_VERSION_MISMATCH"
    LOCAL_GIT_TREE_MISMATCH = "LOCAL_GIT_TREE_MISMATCH"


class ReadinessCode(StrEnum):
    VERIFICATION_NOT_STAGED = "VERIFICATION_NOT_STAGED"
    MANIFEST_DIGEST_PIN_REQUIRED = "MANIFEST_DIGEST_PIN_REQUIRED"
    STAGED_CONTENT_CHANGED = "STAGED_CONTENT_CHANGED"
    DEPLOYMENT_APPROVAL_REQUIRED = "DEPLOYMENT_APPROVAL_REQUIRED"
    APPROVAL_AUTHORITY_REQUIRED = "APPROVAL_AUTHORITY_REQUIRED"


class PinKind(StrEnum):
    MANIFEST_DIGEST = "MANIFEST_DIGEST"
    COMMIT_TREE = "COMMIT_TREE"


@dataclass(frozen=True)
class OperatorPin:
    """What the operator expects, obtained out of band (never from the release itself).

    ``attestation_bundle_sha256`` is the digest of the Sigstore bundle the
    operator verified with ``gh attestation verify``; it is an input digest, not
    a signature check performed by PADOS.
    """

    manifest_sha256: str | None = None
    git_commit: str | None = None
    git_tree: str | None = None
    version: str | None = None
    attestation_bundle_sha256: str | None = None

    def kind(self) -> PinKind | None:
        if self.manifest_sha256 is not None:
            return PinKind.MANIFEST_DIGEST if SHA256_HEX.fullmatch(self.manifest_sha256) else None
        if (self.git_commit and self.git_tree and GIT_OBJECT.fullmatch(self.git_commit)
                and GIT_OBJECT.fullmatch(self.git_tree)):
            return PinKind.COMMIT_TREE
        return None


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    codes: tuple[str, ...]
    manifest_sha256: str | None = None
    git_commit: str | None = None
    git_tree: str | None = None
    version: str | None = None
    pin_kind: PinKind | None = None
    staged: StagedRelease | None = None
    attestation_operator_verified: bool = False
    deployment_performed: bool = field(default=False, init=False)

    @property
    def readiness_eligible(self) -> bool:
        return self.status is VerificationStatus.STAGED_VERIFIED and self.pin_kind is PinKind.MANIFEST_DIGEST

    def as_record(self) -> dict[str, Any]:
        staged = self.staged
        return {"status": self.status.value, "codes": list(self.codes), "manifest_sha256": self.manifest_sha256,
                "git_commit": self.git_commit, "git_tree": self.git_tree, "version": self.version,
                "pin_kind": self.pin_kind.value if self.pin_kind else None,
                "readiness_eligible": self.readiness_eligible,
                "staged_dir": str(staged.directory) if staged else None,
                "staged_digest": staged.staged_digest if staged else None,
                "staging_mode": staged.mode if staged else None,
                "staged_files": [[f.name, f.sha256, f.size] for f in staged.files] if staged else [],
                "attestation_operator_verified": self.attestation_operator_verified,
                "deployment_performed": self.deployment_performed}


@dataclass(frozen=True)
class DeploymentReadiness:
    status: ReadinessStatus
    code: str
    manifest_sha256: str | None = None
    staged_digest: str | None = None
    approval_id: str | None = None
    deployment_performed: bool = field(default=False, init=False)


GitTreeResolver = Callable[[str], str | None]


def _is(value: Any, kind: type) -> bool:
    return isinstance(value, kind) and not (kind is int and isinstance(value, bool))


def _entry_ok(entry: Any, keys: set[str]) -> bool:
    return (isinstance(entry, dict) and set(entry) == keys and isinstance(entry.get("name"), str)
            and isinstance(entry.get("sha256"), str) and bool(SHA256_HEX.fullmatch(entry["sha256"]))
            and _is(entry.get("size"), int) and entry["size"] >= 0)


def validate_manifest(manifest: Any) -> list[str]:
    """Strict manifest schema; returns ``MANIFEST_SCHEMA_INVALID:<field>`` codes."""
    bad = VerifyCode.MANIFEST_SCHEMA_INVALID
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS or manifest["schema"] != MANIFEST_SCHEMA:
        return [f"{bad}:top-level"]
    errors: list[str] = []
    product, source = manifest["product"], manifest["source"]
    version = product.get("version") if isinstance(product, dict) else None
    if not (isinstance(product, dict) and set(product) == {"name", "version", "channel"}
            and product["name"] == "pados" and isinstance(version, str) and _VERSION.fullmatch(version)
            and isinstance(product["channel"], str)):
        return [f"{bad}:product"]
    if not (isinstance(source, dict) and set(source) == {"git_commit", "git_tree", "dirty", "repository", "ref"}
            and isinstance(source["git_commit"], str) and GIT_OBJECT.fullmatch(source["git_commit"])
            and isinstance(source["git_tree"], str) and GIT_OBJECT.fullmatch(source["git_tree"])
            and _is(source["dirty"], bool) and isinstance(source["repository"], str)
            and isinstance(source["ref"], str)):
        errors.append(f"{bad}:source")
    artifacts = manifest["artifacts"]
    if not (isinstance(artifacts, list) and artifacts
            and all(_entry_ok(a, {"name", "kind", "sha256", "size"}) and isinstance(a["kind"], str)
                    for a in artifacts)):
        errors.append(f"{bad}:artifacts")
    else:
        kinds = [a["kind"] for a in artifacts]
        mismatched = any(str(artifact_kind(a["name"], version)) != a["kind"] for a in artifacts)
        if mismatched or len(set(kinds)) != len(kinds) or not set(kinds) & {ArtifactKind.WHEEL, ArtifactKind.SDIST}:
            errors.append(f"{bad}:artifact-kinds")
    sboms = manifest["sbom"]
    if not (isinstance(sboms, list) and len(sboms) == len(SBOM_FORMATS)
            and all(_entry_ok(s, {"name", "format", "sha256", "size", "component_count"}) for s in sboms)
            and {s["name"] for s in sboms} == set(SBOM_FORMATS)
            and all(s["format"] == SBOM_FORMATS[s["name"]] and _is(s["component_count"], int) for s in sboms)):
        errors.append(f"{bad}:sbom")
    expected_statement = {"name": PROVENANCE_FILE, "statement_type": STATEMENT_TYPE,
                          "predicate_type": SLSA_PROVENANCE_PREDICATE, "signing": UNSIGNED_LOCAL_PROVENANCE}
    provenance = manifest["provenance"]
    statement = provenance.get("statement") if isinstance(provenance, dict) else None
    if not (isinstance(provenance, dict) and set(provenance) == {"statement", "attestation"}
            and provenance["attestation"] == ATTESTATION_HANDLING and isinstance(statement, dict)
            and set(statement) == {*expected_statement, "sha256"}
            and all(statement[k] == v for k, v in expected_statement.items())
            and isinstance(statement["sha256"], str) and SHA256_HEX.fullmatch(statement["sha256"])):
        errors.append(f"{bad}:provenance")
    environment = manifest["build_environment"]
    if not (_entry_ok(environment, {"name", "sha256", "size", "package_count"})
            and environment["name"] == BUILD_ENVIRONMENT_FILE and _is(environment["package_count"], int)
            and environment["package_count"] > 0):
        errors.append(f"{bad}:build_environment")
    migrations = manifest["migrations"]
    if not (isinstance(migrations, dict) and set(migrations) == set(MIGRATION_CONFIGS) and all(
            isinstance(m, dict) and set(m) == {"config", "script_location", "heads"}
            and m["config"] == MIGRATION_CONFIGS[lane] and isinstance(m["script_location"], str)
            and isinstance(m["heads"], list) and m["heads"] and all(isinstance(h, str) and h for h in m["heads"])
            for lane, m in migrations.items())):
        errors.append(f"{bad}:migrations")
    versions = manifest["versions"]
    if not (isinstance(versions, dict) and set(versions) == VERSIONS_KEYS
            and isinstance(versions["product"], dict) and versions["product"].get("version") == version
            and isinstance(versions["protocols"], dict) and versions["protocols"]
            and all(isinstance(k, str) and isinstance(v, str) and v for k, v in versions["protocols"].items())
            and all(isinstance(versions[k], dict) for k in ("schemas", "runtimes", "database"))):
        errors.append(f"{bad}:versions")
    if not (isinstance(manifest["builder_id"], str) and manifest["builder_id"]):
        errors.append(f"{bad}:builder_id")
    if not (isinstance(manifest["created_at"], str) and _TIMESTAMP.fullmatch(manifest["created_at"])):
        errors.append(f"{bad}:created_at")
    if manifest["posture"] != {"target": POSTURE_TARGET, "certified": False}:
        errors.append(f"{bad}:posture")
    if manifest["authority"] != {"ci_is_pados_authority": False, "deployment_authorized": False,
                                 "deployment_requires": DEPLOYMENT_REQUIREMENT}:
        errors.append(f"{bad}:authority")
    return errors


def _document(staged: StagedRelease, name: str) -> Any:
    return json.loads(staged.documents[name].decode("utf-8"))


def _check_entry(staged: StagedRelease, entry: Mapping[str, Any], missing: VerifyCode, mismatch: VerifyCode,
                 codes: list[str]) -> bool:
    found = staged.file(str(entry["name"]))
    if found is None:
        codes.append(f"{missing}:{entry['name']}")
        return False
    if "size" in entry and found.size != entry["size"]:
        codes.append(f"{VerifyCode.ARTIFACT_SIZE_MISMATCH}:{found.name}")
        return False
    if found.sha256 != entry["sha256"]:
        codes.append(f"{mismatch}:{found.name}")
        return False
    return True


def _purls_cyclonedx(document: Any, version: str, count: int) -> tuple[str, frozenset[str]] | None:
    try:
        root = document["metadata"]["component"]
        components = document["components"]
        ok = (document["bomFormat"] == "CycloneDX" and document["specVersion"] == "1.5"
              and document["version"] == 1 and isinstance(document["serialNumber"], str)
              and _UUID_URN.fullmatch(document["serialNumber"]) and root["name"] == "pados"
              and root["version"] == version and isinstance(root["purl"], str) and isinstance(components, list)
              and all(isinstance(c, dict) and all(isinstance(c.get(k), str) for k in
                                                  ("type", "bom-ref", "name", "version", "purl"))
                      for c in components)
              and len({c["bom-ref"] for c in components}) == len(components) == count)
    except (KeyError, TypeError):
        return None
    return (root["purl"], frozenset(c["purl"] for c in components)) if ok else None


def _spdx_purl(package: Mapping[str, Any]) -> str | None:
    refs = package.get("externalRefs")
    found = [r.get("referenceLocator") for r in refs if isinstance(r, dict) and r.get("referenceType") == "purl"] \
        if isinstance(refs, list) else []
    return found[0] if len(found) == 1 and isinstance(found[0], str) else None


def _purls_spdx(document: Any, version: str, count: int) -> tuple[str, frozenset[str]] | None:
    try:
        packages = document["packages"]
        described = document["documentDescribes"]
        ok = (document["spdxVersion"] == "SPDX-2.3" and document["dataLicense"] == "CC0-1.0"
              and document["SPDXID"] == "SPDXRef-DOCUMENT" and isinstance(document["documentNamespace"], str)
              and isinstance(document["creationInfo"]["created"], str) and isinstance(packages, list)
              and isinstance(described, list) and len(described) == 1 and len(packages) == count + 1
              and all(isinstance(p, dict) and all(isinstance(p.get(k), str) for k in ("SPDXID", "name", "versionInfo"))
                      and _spdx_purl(p) for p in packages))
        if not ok:
            return None
        roots = [p for p in packages if p["SPDXID"] == described[0]]
        if len(roots) != 1 or roots[0]["name"] != "pados" or roots[0]["versionInfo"] != version:
            return None
        others = frozenset(str(_spdx_purl(p)) for p in packages if p is not roots[0])
    except (KeyError, TypeError):
        return None
    return str(_spdx_purl(roots[0])), others


def _check_sboms(staged: StagedRelease, manifest: Mapping[str, Any], codes: list[str]) -> None:
    version = manifest["product"]["version"]
    counts = {s["name"]: s["component_count"] for s in manifest["sbom"]}
    try:
        cyclonedx = _purls_cyclonedx(_document(staged, SBOM_CYCLONEDX_FILE), version, counts[SBOM_CYCLONEDX_FILE])
        spdx = _purls_spdx(_document(staged, SBOM_SPDX_FILE), version, counts[SBOM_SPDX_FILE])
    except (KeyError, UnicodeDecodeError, ValueError):
        codes.append(VerifyCode.SBOM_INVALID)
        return
    if cyclonedx is None:
        codes.append(f"{VerifyCode.SBOM_INVALID}:{SBOM_CYCLONEDX_FILE}")
    if spdx is None:
        codes.append(f"{VerifyCode.SBOM_INVALID}:{SBOM_SPDX_FILE}")
    if cyclonedx is None or spdx is None:
        return
    if counts[SBOM_CYCLONEDX_FILE] != counts[SBOM_SPDX_FILE]:
        codes.append(VerifyCode.SBOM_COUNT_MISMATCH)
    if cyclonedx != spdx:
        codes.append(VerifyCode.SBOM_PACKAGE_SET_MISMATCH)


def _check_provenance(staged: StagedRelease, manifest: Mapping[str, Any], codes: list[str]) -> None:
    try:
        statement = _document(staged, PROVENANCE_FILE)
        definition = statement["predicate"]["buildDefinition"]
        subjects = sorted((s["name"], s["digest"]["sha256"]) for s in statement["subject"])
        commits = {d["digest"]["gitCommit"] for d in definition["resolvedDependencies"]
                   if "gitCommit" in d.get("digest", {})}
        files = {d["uri"]: d["digest"].get("sha256") for d in definition["resolvedDependencies"]
                 if str(d.get("uri", "")).startswith("file:")}
        internal = definition["internalParameters"]
        signing, tree = internal.get("pados_signing"), internal.get("git_tree")
    except (KeyError, TypeError, AttributeError, UnicodeDecodeError, ValueError):
        codes.append(VerifyCode.PROVENANCE_INVALID)
        return
    if (statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != SLSA_PROVENANCE_PREDICATE
            or signing != UNSIGNED_LOCAL_PROVENANCE):
        codes.append(VerifyCode.PROVENANCE_INVALID)
        return
    expected = sorted((e["name"], e["sha256"]) for e in [*manifest["artifacts"], *manifest["sbom"]])
    if subjects != expected:
        codes.append(VerifyCode.PROVENANCE_SUBJECT_MISMATCH)
    if commits != {manifest["source"]["git_commit"]} or tree != manifest["source"]["git_tree"]:
        codes.append(VerifyCode.COMMIT_TREE_INCONSISTENT)
    if files.get(f"file:{BUILD_ENVIRONMENT_FILE}") != manifest["build_environment"]["sha256"]:
        codes.append(VerifyCode.BUILD_ENVIRONMENT_MISMATCH)


def _check_build_environment(staged: StagedRelease, manifest: Mapping[str, Any], codes: list[str]) -> None:
    try:
        packages = parse_build_environment(staged.documents[BUILD_ENVIRONMENT_FILE].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, SupplyChainError):
        codes.append(VerifyCode.BUILD_ENVIRONMENT_MISMATCH)
        return
    if len(packages) != manifest["build_environment"]["package_count"]:
        codes.append(VerifyCode.BUILD_ENVIRONMENT_MISMATCH)


def _check_pin(manifest: Mapping[str, Any], digest: str, pin: OperatorPin, codes: list[str]) -> None:
    if pin.kind() is None:
        codes.append(VerifyCode.OPERATOR_PIN_REQUIRED)
        return
    if pin.manifest_sha256 is not None and pin.manifest_sha256 != digest:
        codes.append(VerifyCode.PIN_MANIFEST_HASH_MISMATCH)
    if pin.git_commit is not None and pin.git_commit != manifest["source"]["git_commit"]:
        codes.append(VerifyCode.PIN_COMMIT_MISMATCH)
    if pin.git_tree is not None and pin.git_tree != manifest["source"]["git_tree"]:
        codes.append(VerifyCode.PIN_TREE_MISMATCH)
    if pin.version is not None and pin.version != manifest["product"]["version"]:
        codes.append(VerifyCode.PIN_VERSION_MISMATCH)


def verify_release(release_dir: Path, pin: OperatorPin, *, staging_root: Path,
                   git_tree_of: GitTreeResolver | None = None) -> VerificationResult:
    """Stage ``release_dir`` under ``staging_root`` and verify the staged bytes. Never deploys."""
    rejected = VerificationStatus.REJECTED
    try:
        staged = stage_release(release_dir, staging_root)
    except StagingError as error:
        return VerificationResult(rejected, (f"{VerifyCode.STAGING_REFUSED}:{error.code}",))
    if MANIFEST_FILE not in staged.documents:
        return VerificationResult(rejected, (VerifyCode.MANIFEST_MISSING,), staged=staged)
    try:
        manifest = _document(staged, MANIFEST_FILE)
    except (UnicodeDecodeError, ValueError):
        return VerificationResult(rejected, (VerifyCode.MANIFEST_UNREADABLE,), staged=staged)
    schema_errors = validate_manifest(manifest)
    if schema_errors:
        return VerificationResult(rejected, tuple(schema_errors), staged=staged)
    digest = manifest_sha256(manifest)
    source = manifest["source"]
    codes: list[str] = []
    if source["dirty"]:
        codes.append(VerifyCode.SOURCE_TREE_DIRTY)
    for entry in manifest["artifacts"]:
        _check_entry(staged, entry, VerifyCode.ARTIFACT_MISSING, VerifyCode.ARTIFACT_HASH_MISMATCH, codes)
    sboms_ok = all([_check_entry(staged, entry, VerifyCode.SBOM_MISSING, VerifyCode.SBOM_HASH_MISMATCH, codes)
                    for entry in manifest["sbom"]])
    if sboms_ok:
        _check_sboms(staged, manifest, codes)
    if _check_entry(staged, manifest["provenance"]["statement"], VerifyCode.PROVENANCE_MISSING,
                    VerifyCode.PROVENANCE_HASH_MISMATCH, codes):
        _check_provenance(staged, manifest, codes)
    if _check_entry(staged, manifest["build_environment"], VerifyCode.BUILD_ENVIRONMENT_MISMATCH,
                    VerifyCode.BUILD_ENVIRONMENT_MISMATCH, codes):
        _check_build_environment(staged, manifest, codes)
    recorded = {MANIFEST_FILE, PROVENANCE_FILE, BUILD_ENVIRONMENT_FILE,
                *(e["name"] for e in manifest["artifacts"]), *(e["name"] for e in manifest["sbom"])}
    attestation_verified = False
    if pin.attestation_bundle_sha256 is not None:
        recorded.add(ATTESTATION_FILE)
        bundle = staged.file(ATTESTATION_FILE)
        if bundle is None or bundle.sha256 != pin.attestation_bundle_sha256:
            codes.append(VerifyCode.ATTESTATION_PIN_MISMATCH)
        else:
            attestation_verified = True
    for staged_file in staged.files:
        if staged_file.name not in recorded:
            codes.append(f"{VerifyCode.UNRECORDED_FILE_IN_RELEASE}:{staged_file.name}")
    _check_pin(manifest, digest, pin, codes)
    if git_tree_of is not None and git_tree_of(source["git_commit"]) != source["git_tree"]:
        codes.append(VerifyCode.LOCAL_GIT_TREE_MISMATCH)
    pin_kind = pin.kind()
    if codes:
        status = rejected
    elif pin_kind is PinKind.MANIFEST_DIGEST:
        status = VerificationStatus.STAGED_VERIFIED
    else:
        status = VerificationStatus.STAGED_VERIFIED_COMMIT_PIN_ONLY
    return VerificationResult(status, tuple(str(c) for c in codes), digest, source["git_commit"], source["git_tree"],
                              manifest["product"]["version"], pin_kind, staged, attestation_verified)


def _precheck(verification: VerificationResult, environ: Mapping[str, str]) -> str | None:
    if running_in_ci(environ):
        return ApprovalCode.CI_CANNOT_AUTHORIZE_DEPLOYMENT.value
    if verification.status is VerificationStatus.STAGED_VERIFIED_COMMIT_PIN_ONLY:
        return ReadinessCode.MANIFEST_DIGEST_PIN_REQUIRED.value
    if not verification.readiness_eligible or verification.staged is None or verification.manifest_sha256 is None:
        return ReadinessCode.VERIFICATION_NOT_STAGED.value
    if staged_integrity_error(verification.staged) is not None:
        return ReadinessCode.STAGED_CONTENT_CHANGED.value
    return None


def _evaluate(verification: VerificationResult, approval: Mapping[str, Any] | None,
              authority: DeploymentApprovalAuthority | None, target: str, now: datetime | None,
              environ: Mapping[str, str] | None, consume: bool) -> DeploymentReadiness:
    staged_digest = verification.staged.staged_digest if verification.staged else None

    def refuse(code: str, approval_id: str | None = None) -> DeploymentReadiness:
        return DeploymentReadiness(ReadinessStatus.NOT_AUTHORIZED, code, verification.manifest_sha256,
                                   staged_digest, approval_id)

    code = _precheck(verification, os.environ if environ is None else environ)
    if code is not None:
        return refuse(code)
    if approval is None:
        return refuse(ReadinessCode.DEPLOYMENT_APPROVAL_REQUIRED.value)
    if authority is None or verification.manifest_sha256 is None or staged_digest is None:
        return refuse(ReadinessCode.APPROVAL_AUTHORITY_REQUIRED.value)
    binding = Binding(verification.manifest_sha256, staged_digest, target)
    moment = now or datetime.now(UTC)
    decision = (authority.consume if consume else authority.check)(approval, binding, now=moment)
    if not decision.ok:
        return refuse(decision.code.value, decision.approval_id)
    return DeploymentReadiness(ReadinessStatus.DEPLOYMENT_AUTHORIZED_BY_HUMAN, decision.code.value,
                               verification.manifest_sha256, staged_digest, decision.approval_id)


def deployment_readiness(verification: VerificationResult, approval: Mapping[str, Any] | None, *,
                         authority: DeploymentApprovalAuthority | None, target: str, now: datetime | None = None,
                         environ: Mapping[str, str] | None = None) -> DeploymentReadiness:
    """``DEPLOYMENT_AUTHORIZED_BY_HUMAN`` only for a digest-pinned, intact staged release plus a valid approval."""
    return _evaluate(verification, approval, authority, target, now, environ, consume=False)


def consume_approval(verification: VerificationResult, approval: Mapping[str, Any] | None, *,
                     authority: DeploymentApprovalAuthority | None, target: str, now: datetime | None = None,
                     environ: Mapping[str, str] | None = None) -> DeploymentReadiness:
    """Explicit single-use step: re-check everything, then record the approval as consumed. Never deploys."""
    return _evaluate(verification, approval, authority, target, now, environ, consume=True)
