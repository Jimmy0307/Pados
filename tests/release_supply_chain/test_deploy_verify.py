"""Pull-based Home Base verification: staged copy, digest pin, signed single-use approval, never deploys."""
import hashlib
import json
import multiprocessing
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import pados.release.approval as approval_module
import pados.release.deploy_verify as deploy_verify_module
import pados.release.staging as staging_module
from pados.enterprise.secrets import SecretBoundary
from pados.release.approval import (
    ApprovalCode,
    ApprovalError,
    Binding,
    ConsumedApprovalLedger,
    HmacApprovalAuthority,
    OperatorRegistry,
)
from pados.release.deploy_verify import (
    OperatorPin,
    ReadinessCode,
    ReadinessStatus,
    VerificationResult,
    VerificationStatus,
    VerifyCode,
    consume_approval,
    deployment_readiness,
    verify_release,
)
from pados.release.sbom import render
from pados.release.staging import staged_digest
from pados.release.supply_chain import BuildInvocation, SourceIdentity, write_release_metadata

from .support import (
    APPROVER,
    COMMIT,
    KEY_REF,
    NOW,
    ROOT,
    TARGET,
    TREE,
    WHEEL,
    FixedResolver,
    build_release_dir,
    make_authority,
)

COMMIT_PIN = OperatorPin(git_commit=COMMIT, git_tree=TREE)


def _verify(tmp: Path, release: Path, pin: OperatorPin, **kwargs: Any) -> VerificationResult:
    return verify_release(release, pin, staging_root=tmp / "staging", **kwargs)


def _codes(tmp: Path, release: Path, pin: OperatorPin) -> set[str]:
    return {code.split(":", 1)[0] for code in _verify(tmp, release, pin).codes}


def _full_codes(tmp: Path, release: Path, pin: OperatorPin) -> set[str]:
    return set(_verify(tmp, release, pin).codes)


def _rewrite_manifest(release: Path, change: Callable[[dict[str, Any]], object]) -> None:
    path = release / "release-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def verified(tmp_path: Path) -> tuple[Path, VerificationResult, HmacApprovalAuthority]:
    release, metadata = build_release_dir(tmp_path)
    result = _verify(tmp_path, release, OperatorPin(manifest_sha256=metadata.manifest_sha256))
    assert result.status is VerificationStatus.STAGED_VERIFIED, result.codes
    return release, result, make_authority(tmp_path)


def _issue(authority: HmacApprovalAuthority, result: VerificationResult, **overrides: Any) -> dict[str, Any]:
    assert result.staged is not None and result.manifest_sha256 is not None
    fields: dict[str, Any] = {"manifest_sha256": result.manifest_sha256, "staged_digest": result.staged.staged_digest,
                              "target": TARGET, "approver": APPROVER, "issued_at": NOW - timedelta(minutes=5),
                              "lifetime": timedelta(hours=1)}
    fields.update(overrides)
    return authority.issue(**fields)


def _ready(result: VerificationResult, record: Any, authority: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("target", TARGET)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("environ", {})
    return deployment_readiness(result, record, authority=authority, **kwargs)


def _consume(result: VerificationResult, record: Any, authority: Any) -> Any:
    return consume_approval(result, record, authority=authority, target=TARGET, now=NOW, environ={})


# --- verification and staging -------------------------------------------------------------


def test_valid_release_is_staged_into_a_read_only_copy(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path)
    result = _verify(tmp_path, release, OperatorPin(manifest_sha256=metadata.manifest_sha256))
    assert result.status is VerificationStatus.STAGED_VERIFIED and result.codes == ()
    assert result.readiness_eligible and result.deployment_performed is False
    staged = result.staged
    assert staged is not None and staged.directory.parent == tmp_path / "staging"
    assert staged.staged_digest == staged_digest(list(staged.files))
    assert {f.name for f in staged.files} >= {WHEEL, "sbom.cdx.json", "provenance.intoto.json"}
    if sys.platform != "win32":
        assert stat.S_IMODE(staged.directory.stat().st_mode) == 0o500
        assert all(stat.S_IMODE((staged.directory / f.name).stat().st_mode) == 0o444 for f in staged.files)
    assert staged.documents["release-manifest.json"] == (staged.directory / "release-manifest.json").read_bytes()


def test_cr04_sc02_commit_tree_pin_is_never_readiness_eligible(tmp_path: Path) -> None:
    release, _ = build_release_dir(tmp_path)
    result = _verify(tmp_path, release, COMMIT_PIN)
    assert result.status is VerificationStatus.STAGED_VERIFIED_COMMIT_PIN_ONLY and not result.readiness_eligible
    authority = make_authority(tmp_path)
    readiness = _ready(result, _issue(authority, result), authority)
    assert readiness.status is ReadinessStatus.NOT_AUTHORIZED
    assert readiness.code == ReadinessCode.MANIFEST_DIGEST_PIN_REQUIRED


def test_cr04_sc02_regenerated_artifacts_with_the_same_commit_fail_the_digest_pin(tmp_path: Path) -> None:
    _, original = build_release_dir(tmp_path / "a")
    attacker, _ = build_release_dir(tmp_path / "b")
    (attacker / WHEEL).write_bytes(b"malicious wheel")
    for name in ("release-manifest.json", "provenance.intoto.json"):
        (attacker / name).unlink()
    write_release_metadata(ROOT, attacker, source=SourceIdentity(COMMIT, TREE, False, "git+https://example.invalid/pados"),
                           invocation=BuildInvocation(), created_at="2026-10-02T00:00:00Z")
    assert _verify(tmp_path, attacker, COMMIT_PIN).status is VerificationStatus.STAGED_VERIFIED_COMMIT_PIN_ONLY
    assert VerifyCode.PIN_MANIFEST_HASH_MISMATCH in _codes(
        tmp_path, attacker, OperatorPin(manifest_sha256=original.manifest_sha256))


def test_cr04_sc02_attestation_bundle_is_an_operator_input_digest(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path, attestation=True)
    bundle = hashlib.sha256((release / "attestation.sigstore.json").read_bytes()).hexdigest()
    ok = _verify(tmp_path, release, OperatorPin(manifest_sha256=metadata.manifest_sha256,
                                                attestation_bundle_sha256=bundle))
    assert ok.status is VerificationStatus.STAGED_VERIFIED and ok.attestation_operator_verified
    wrong = OperatorPin(manifest_sha256=metadata.manifest_sha256, attestation_bundle_sha256="0" * 64)
    assert VerifyCode.ATTESTATION_PIN_MISMATCH in _codes(tmp_path, release, wrong)
    assert VerifyCode.UNRECORDED_FILE_IN_RELEASE in _codes(
        tmp_path, release, OperatorPin(manifest_sha256=metadata.manifest_sha256))


def test_artifact_hash_size_and_presence(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path)
    pin = OperatorPin(manifest_sha256=metadata.manifest_sha256)
    (release / WHEEL).write_bytes(b"wheel-bytez")
    assert VerifyCode.ARTIFACT_HASH_MISMATCH in _codes(tmp_path, release, pin)
    (release / WHEEL).write_bytes(b"tampered and longer")
    assert VerifyCode.ARTIFACT_SIZE_MISMATCH in _codes(tmp_path, release, pin)
    (release / WHEEL).unlink()
    assert VerifyCode.ARTIFACT_MISSING in _codes(tmp_path, release, pin)


def test_tampered_sbom_and_provenance_are_refused(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path)
    pin = OperatorPin(manifest_sha256=metadata.manifest_sha256)
    sbom = release / "sbom.cdx.json"
    original = sbom.read_bytes()
    sbom.write_bytes(original.replace(b'"react"', b'"reakt"'))
    assert VerifyCode.SBOM_HASH_MISMATCH in _codes(tmp_path, release, pin)
    sbom.write_bytes(original)
    provenance = release / "provenance.intoto.json"
    provenance.write_text(provenance.read_text().replace(COMMIT, "9" * 40))
    assert VerifyCode.PROVENANCE_HASH_MISMATCH in _codes(tmp_path, release, pin)


def test_pins_commit_tree_and_local_git(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path)
    assert VerifyCode.PIN_COMMIT_MISMATCH in _codes(tmp_path, release, OperatorPin(git_commit="c" * 40, git_tree=TREE))
    assert VerifyCode.PIN_TREE_MISMATCH in _codes(tmp_path, release, OperatorPin(git_commit=COMMIT, git_tree="d" * 40))
    for pin in (OperatorPin(), OperatorPin(git_commit=COMMIT), OperatorPin(manifest_sha256="xyz")):
        assert VerifyCode.OPERATOR_PIN_REQUIRED in _codes(tmp_path, release, pin)
    digest_pin = OperatorPin(manifest_sha256=metadata.manifest_sha256)
    assert VerifyCode.LOCAL_GIT_TREE_MISMATCH in _verify(tmp_path, release, digest_pin,
                                                         git_tree_of=lambda commit: "f" * 40).codes
    assert _verify(tmp_path, release, digest_pin,
                   git_tree_of=lambda commit: TREE).status is VerificationStatus.STAGED_VERIFIED
    _rewrite_manifest(release, lambda m: m["source"].update(git_commit="e" * 40))
    assert VerifyCode.COMMIT_TREE_INCONSISTENT in _codes(
        tmp_path, release, OperatorPin(git_commit="e" * 40, git_tree=TREE))


def test_unrecorded_dirty_and_malformed(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path, dirty=True)
    pin = OperatorPin(manifest_sha256=metadata.manifest_sha256)
    assert VerifyCode.SOURCE_TREE_DIRTY in _codes(tmp_path, release, pin)
    (release / "extra.sh").write_text("echo")
    assert VerifyCode.UNRECORDED_FILE_IN_RELEASE in _codes(tmp_path, release, pin)
    (release / "extra.sh").unlink()
    (release / "release-manifest.json").write_text("{not json")
    assert _verify(tmp_path, release, pin).codes == (VerifyCode.MANIFEST_UNREADABLE,)
    (release / "release-manifest.json").unlink()
    assert _verify(tmp_path, release, pin).codes == (VerifyCode.MANIFEST_MISSING,)


# --- SC-03 staging -------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX no-follow staging")
def test_cr04_sc03_symlinks_fifos_and_symlinked_directory_are_refused(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path)
    pin = OperatorPin(manifest_sha256=metadata.manifest_sha256)
    (release / "link.whl").symlink_to(release / WHEEL)
    assert _verify(tmp_path, release, pin).codes == ("STAGING_REFUSED:RELEASE_FILE_NOT_REGULAR",)
    (release / "link.whl").unlink()
    os.mkfifo(release / "pipe.json")
    assert _verify(tmp_path, release, pin).codes == ("STAGING_REFUSED:RELEASE_FILE_NOT_REGULAR",)
    (release / "pipe.json").unlink()
    (tmp_path / "alias").symlink_to(release, target_is_directory=True)
    assert _verify(tmp_path, tmp_path / "alias", pin).codes == ("STAGING_REFUSED:RELEASE_DIRECTORY_UNREADABLE",)
    assert list((tmp_path / "staging").glob("stage-*")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_cr04_sc03_source_changes_after_staging_do_not_matter_but_staged_changes_do(
        verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    release, result, authority = verified
    record = _issue(authority, result)
    (release / WHEEL).write_bytes(b"swapped after verification")
    assert _ready(result, record, authority).status is ReadinessStatus.DEPLOYMENT_AUTHORIZED_BY_HUMAN
    assert result.staged is not None
    directory = result.staged.directory
    os.chmod(directory, 0o700)
    os.chmod(directory / WHEEL, 0o600)
    (directory / WHEEL).write_bytes(b"swapped in staging")
    readiness = _ready(result, record, authority)
    assert readiness.status is ReadinessStatus.NOT_AUTHORIZED and readiness.code == ReadinessCode.STAGED_CONTENT_CHANGED


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_cr04_sc03_extra_staged_file_is_detected(verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    assert result.staged is not None
    os.chmod(result.staged.directory, 0o700)
    (result.staged.directory / "late.txt").write_text("x")
    assert _ready(result, _issue(authority, result), authority).code == ReadinessCode.STAGED_CONTENT_CHANGED


# --- SC-08 strict manifest schema ----------------------------------------------------------


@pytest.mark.parametrize("change,field", [
    (lambda m: m.pop("migrations"), "top-level"),
    (lambda m: m.pop("versions"), "top-level"),
    (lambda m: m["migrations"]["proof"].update(heads=[]), "migrations"),
    (lambda m: m["versions"].pop("protocols"), "versions"),
    (lambda m: m["versions"]["product"].update(version="9.9.9"), "versions"),
    (lambda m: m["artifacts"][0].update(kind="SDIST"), "artifact-kinds"),
    (lambda m: m["artifacts"][0].update(name="../outside.whl"), "artifact-kinds"),
    (lambda m: m["artifacts"].append(dict(m["artifacts"][0])), "artifact-kinds"),
    (lambda m: m["sbom"][0].update(format="CycloneDX-1.4"), "sbom"),
    (lambda m: m["sbom"].pop(), "sbom"),
    (lambda m: m["provenance"].update(attestation="GITHUB_SIGNED_TRUST_ME"), "provenance"),
    (lambda m: m["build_environment"].update(package_count=0), "build_environment"),
    (lambda m: m["authority"].update(deployment_authorized=True), "authority"),
    (lambda m: m["posture"].update(certified=True), "posture"),
    (lambda m: m["product"].update(name="other"), "product"),
    (lambda m: m.update(extra="field"), "top-level"),
])
def test_cr04_sc08_manifest_schema_is_strict(tmp_path: Path, change: Callable[[dict[str, Any]], object],
                                             field: str) -> None:
    release, _ = build_release_dir(tmp_path)
    _rewrite_manifest(release, change)
    assert f"MANIFEST_SCHEMA_INVALID:{field}" in _full_codes(tmp_path, release, COMMIT_PIN)


def _replace_sbom(release: Path, name: str, change: Callable[[dict[str, Any]], object]) -> None:
    path = release / name
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_bytes(render(document))
    digest, size = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size

    def update(manifest: dict[str, Any]) -> None:
        for entry in manifest["sbom"]:
            if entry["name"] == name:
                entry["sha256"], entry["size"] = digest, size

    _rewrite_manifest(release, update)


def test_cr04_sc08_sbom_package_sets_must_agree(tmp_path: Path) -> None:
    release, _ = build_release_dir(tmp_path)

    def swap(document: dict[str, Any]) -> None:
        document["packages"][-1]["externalRefs"][0]["referenceLocator"] = "pkg:npm/evil@1.0.0"

    _replace_sbom(release, "sbom.spdx.json", swap)
    codes = _full_codes(tmp_path, release, COMMIT_PIN)
    assert VerifyCode.SBOM_PACKAGE_SET_MISMATCH in codes
    assert VerifyCode.PROVENANCE_SUBJECT_MISMATCH in codes


def test_cr04_sc08_sbom_documents_and_counts_are_validated(tmp_path: Path) -> None:
    release, _ = build_release_dir(tmp_path / "one")
    _replace_sbom(release, "sbom.cdx.json", lambda d: d.update(specVersion="1.4"))
    assert "SBOM_INVALID:sbom.cdx.json" in _full_codes(tmp_path / "one", release, COMMIT_PIN)
    release2, _ = build_release_dir(tmp_path / "two")
    _replace_sbom(release2, "sbom.spdx.json", lambda d: d["packages"].pop())
    assert "SBOM_INVALID:sbom.spdx.json" in _full_codes(tmp_path / "two", release2, COMMIT_PIN)
    release3, _ = build_release_dir(tmp_path / "three")
    _rewrite_manifest(release3, lambda m: [e.update(component_count=1) for e in m["sbom"]])
    assert "SBOM_INVALID:sbom.cdx.json" in _full_codes(tmp_path / "three", release3, COMMIT_PIN)


def test_cr04_sc08_build_environment_must_match(tmp_path: Path) -> None:
    release, _ = build_release_dir(tmp_path)
    _rewrite_manifest(release, lambda m: m["build_environment"].update(package_count=7))
    assert VerifyCode.BUILD_ENVIRONMENT_MISMATCH in _codes(tmp_path, release, COMMIT_PIN)


# --- SC-01 / SC-09 approval authority ------------------------------------------------------


def test_cr04_sc01_signed_bound_approval_authorizes_then_is_single_use(
        verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    record = _issue(authority, result)
    readiness = _ready(result, record, authority)
    assert readiness.status is ReadinessStatus.DEPLOYMENT_AUTHORIZED_BY_HUMAN
    assert readiness.code == ApprovalCode.AUTHORIZED and readiness.deployment_performed is False
    consumed = _consume(result, record, authority)
    assert consumed.code == ApprovalCode.CONSUMED and consumed.deployment_performed is False
    assert _ready(result, record, authority).code == ApprovalCode.APPROVAL_ALREADY_CONSUMED
    assert _consume(result, record, authority).code == ApprovalCode.APPROVAL_ALREADY_CONSUMED


def test_cr04_sc01_self_declared_json_is_refused(verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    forged = {**_issue(authority, result), "signature": "a" * 64}
    assert _ready(result, forged, authority).code == ApprovalCode.APPROVAL_SIGNATURE_INVALID
    unsigned = {k: v for k, v in _issue(authority, result).items() if k != "signature"}
    assert _ready(result, unsigned, authority).code == ApprovalCode.APPROVAL_MALFORMED


def test_cr04_sc01_other_key_or_tampered_fields_fail_the_signature(
        tmp_path: Path, verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    other = make_authority(tmp_path)
    assert _ready(result, _issue(other, result), authority).code == ApprovalCode.APPROVAL_SIGNATURE_INVALID
    tampered = {**_issue(authority, result), "target": "home-base-02"}
    assert _ready(result, tampered, authority, target="home-base-02").code == ApprovalCode.APPROVAL_SIGNATURE_INVALID
    longer = {**_issue(authority, result), "expires_at": (NOW + timedelta(days=30)).isoformat()}
    assert _ready(result, longer, authority).code == ApprovalCode.APPROVAL_SIGNATURE_INVALID


def test_cr04_sc01_approver_must_be_registered(tmp_path: Path,
                                               verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, _ = verified
    key = secrets.token_hex(32)
    issuer = make_authority(tmp_path, key=key, operators=(APPROVER, "op.mallory"))
    checker = make_authority(tmp_path, key=key, operators=(APPROVER,))
    assert _ready(result, _issue(issuer, result, approver="op.mallory"), checker).code \
        == ApprovalCode.APPROVER_NOT_REGISTERED
    with pytest.raises(ApprovalError) as error:
        _issue(checker, result, approver="op.mallory")
    assert error.value.code is ApprovalCode.APPROVER_NOT_REGISTERED
    with pytest.raises(ValueError, match="PRINCIPAL_INVALID"):
        OperatorRegistry.from_document({"schema": "pados.deploy-operators/v1",
                                        "operators": [{"operator_id": "github-actions", "enabled": True}]})
    disabled = OperatorRegistry.from_document({"schema": "pados.deploy-operators/v1",
                                               "operators": [{"operator_id": APPROVER, "enabled": False}]})
    assert not disabled.allows(APPROVER)


@pytest.mark.parametrize("environ", [{"GITHUB_ACTIONS": "true"}, {"ACTIONS_ID_TOKEN_REQUEST_URL": "x"},
                                     {"GITHUB_RUN_ID": "1"}])
def test_cr04_sc01_inside_github_actions_everything_is_refused(
        tmp_path: Path, environ: dict[str, str],
        verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    record = _issue(authority, result)
    assert _ready(result, record, authority, environ=environ).code == ApprovalCode.CI_CANNOT_AUTHORIZE_DEPLOYMENT
    ci_authority = make_authority(tmp_path, environ=environ)
    assert result.staged is not None and result.manifest_sha256 is not None
    binding = Binding(result.manifest_sha256, result.staged.staged_digest, TARGET)
    assert ci_authority.check(record, binding, now=NOW).code == ApprovalCode.CI_CANNOT_AUTHORIZE_DEPLOYMENT
    with pytest.raises(ApprovalError) as error:
        _issue(ci_authority, result)
    assert error.value.code is ApprovalCode.CI_CANNOT_AUTHORIZE_DEPLOYMENT


@pytest.mark.parametrize("change", [
    {"origin": "CI"}, {"origin": "GITHUB_ACTIONS"}, {"approver": "github-actions[bot]"},
    {"approver": "dependabot[bot]"}, {"github_run_id": "123"},
])
def test_cr04_sc01_ci_origin_markers_are_refused_regardless_of_signature(
        verified: tuple[Path, VerificationResult, HmacApprovalAuthority], change: dict[str, str]) -> None:
    _, result, authority = verified
    record = {**_issue(authority, result), **change}
    assert _ready(result, record, authority).code == ApprovalCode.CI_CANNOT_AUTHORIZE_DEPLOYMENT


def test_cr04_sc09_bindings_manifest_staged_digest_and_target(
        verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    assert _ready(result, _issue(authority, result, manifest_sha256="1" * 64), authority).code \
        == ApprovalCode.APPROVAL_MANIFEST_MISMATCH
    assert _ready(result, _issue(authority, result, staged_digest="2" * 64), authority).code \
        == ApprovalCode.APPROVAL_STAGED_DIGEST_MISMATCH
    assert _ready(result, _issue(authority, result), authority, target="home-base-02").code \
        == ApprovalCode.APPROVAL_TARGET_MISMATCH


def test_cr04_sc09_expiry_is_mandatory_and_bounded(verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    record = _issue(authority, result)
    assert _ready(result, record, authority, now=NOW + timedelta(hours=2)).code == ApprovalCode.APPROVAL_EXPIRED
    assert _ready(result, record, authority, now=NOW - timedelta(hours=1)).code == ApprovalCode.APPROVAL_NOT_YET_VALID
    with pytest.raises(ApprovalError):
        _issue(authority, result, lifetime=timedelta(hours=25))
    assert _issue(authority, result, lifetime=timedelta(hours=24))["expires_at"]
    no_expiry = {k: v for k, v in record.items() if k != "expires_at"}
    assert _ready(result, no_expiry, authority).code == ApprovalCode.APPROVAL_MALFORMED
    assert _ready(result, {**record, "approval_id": "abc"}, authority).code == ApprovalCode.APPROVAL_MALFORMED
    assert len(record["approval_id"]) == 32 and record["approval_id"] != _issue(authority, result)["approval_id"]


def test_cr04_sc01_key_is_resolved_by_secretref_and_never_exposed(
        tmp_path: Path, verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, _ = verified
    key = "k" * 48
    authority = make_authority(tmp_path, key=key)
    record = _issue(authority, result)
    assert key not in json.dumps(record)
    no_key = HmacApprovalAuthority(key_ref=KEY_REF, boundary=SecretBoundary(FixedResolver({})),
                                   registry=OperatorRegistry(frozenset({APPROVER})),
                                   ledger=ConsumedApprovalLedger(tmp_path / "l.jsonl"), environ={})
    assert _ready(result, record, no_key).code == ApprovalCode.APPROVAL_KEY_UNAVAILABLE
    weak = make_authority(tmp_path, key="short")
    with pytest.raises(ApprovalError) as error:
        _issue(weak, result)
    assert error.value.code is ApprovalCode.APPROVAL_KEY_UNAVAILABLE and "short" not in str(error.value)
    with pytest.raises(ValueError, match="APPROVAL_KEY_REF_INVALID"):
        HmacApprovalAuthority(key_ref="plain-key", boundary=SecretBoundary(FixedResolver({})),
                              registry=OperatorRegistry(frozenset()), ledger=ConsumedApprovalLedger(tmp_path / "x"))


def test_cr04_sc09_ledger_is_hash_chained_and_tamper_evident(
        tmp_path: Path, verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    for _ in range(2):
        assert _consume(result, _issue(authority, result), authority).code == ApprovalCode.CONSUMED
    ledger = tmp_path / "consumed-approvals.jsonl"
    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [e["seq"] for e in entries] == [1, 2] and entries[1]["prev_hash"] == entries[0]["entry_hash"]
    assert all("signature" not in e for e in entries)
    lines = ledger.read_text().splitlines()
    ledger.write_text("\n".join([lines[0].replace(TARGET, "home-base-99"), lines[1]]) + "\n")
    assert _ready(result, _issue(authority, result), authority).code == ApprovalCode.APPROVAL_LEDGER_CORRUPT


def _consume_in_process(ledger_path: str, record: dict[str, Any], results: Any) -> None:
    try:
        ConsumedApprovalLedger(Path(ledger_path)).append(record, consumed_at="2026-10-02T12:00:00+00:00")
        results.put("CONSUMED")
    except ApprovalError as error:
        results.put(error.code.value)


@pytest.mark.skipif(sys.platform == "win32", reason="fork start method")
def test_cr04_sc09_concurrent_consumption_succeeds_exactly_once(
        tmp_path: Path, verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    _, result, authority = verified
    record = _issue(authority, result)
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    ledger = str(tmp_path / "race.jsonl")
    processes = [context.Process(target=_consume_in_process, args=(ledger, record, results)) for _ in range(6)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
    outcomes = sorted(results.get(timeout=5) for _ in processes)
    assert outcomes.count("CONSUMED") == 1
    assert outcomes.count("APPROVAL_ALREADY_CONSUMED") == 5


def test_readiness_preconditions(tmp_path: Path, verified: tuple[Path, VerificationResult, HmacApprovalAuthority]) -> None:
    release, result, authority = verified
    record = _issue(authority, result)
    assert _ready(result, None, authority).code == ReadinessCode.DEPLOYMENT_APPROVAL_REQUIRED
    assert _ready(result, record, None).code == ReadinessCode.APPROVAL_AUTHORITY_REQUIRED
    (release / WHEEL).write_bytes(b"tampered!!!")
    rejected = _verify(tmp_path, release, OperatorPin(manifest_sha256=result.manifest_sha256))
    assert _ready(rejected, record, authority).code == ReadinessCode.VERIFICATION_NOT_STAGED


def test_verifier_modules_have_no_network_or_deploy_capability() -> None:
    for module in (deploy_verify_module, staging_module, approval_module):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        for forbidden in ("import socket", "import http", "urllib.request", "httpx", "requests", "paramiko",
                          "subprocess", "import ssl"):
            assert forbidden not in source, (module.__name__, forbidden)


def test_cr04_sc01_end_to_end_scripts(tmp_path: Path) -> None:
    release, metadata = build_release_dir(tmp_path)
    operators = tmp_path / "operators.json"
    operators.write_text(json.dumps({"schema": "pados.deploy-operators/v1",
                                     "operators": [{"operator_id": APPROVER, "enabled": True}]}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "ACTIONS_", "RUNNER_"))}
    env["PADOS_DEPLOY_APPROVAL_KEY"] = secrets.token_hex(32)
    common = ["--operators", str(operators), "--approval-key-ref", "secretref://env/PADOS_DEPLOY_APPROVAL_KEY",
              "--ledger", str(tmp_path / "ledger.jsonl")]
    verify = [sys.executable, str(ROOT / "scripts/release/verify_release.py"), str(release),
              "--staging-root", str(tmp_path / "staging"), "--expect-manifest-sha256", metadata.manifest_sha256]
    staged = subprocess.run(verify, capture_output=True, text=True, env=env, check=False)
    assert staged.returncode == 0, staged.stdout + staged.stderr
    staged_value = json.loads(staged.stdout[:staged.stdout.rindex("}") + 1])["staged_digest"]
    approval = tmp_path / "approval.json"
    issued = subprocess.run([sys.executable, str(ROOT / "scripts/release/issue_deploy_approval.py"), str(approval),
                             "--manifest-sha256", metadata.manifest_sha256, "--staged-digest", staged_value,
                             "--target", TARGET, "--approver", APPROVER, *common],
                            capture_output=True, text=True, env=env, check=False)
    assert issued.returncode == 0, issued.stdout + issued.stderr
    assert env["PADOS_DEPLOY_APPROVAL_KEY"] not in issued.stdout + approval.read_text()
    consume = [*verify, "--approval", str(approval), "--target", TARGET, *common, "--consume"]
    first = subprocess.run(consume, capture_output=True, text=True, env=env, check=False)
    assert first.returncode == 0 and "code=CONSUMED" in first.stdout, first.stdout + first.stderr
    second = subprocess.run(consume, capture_output=True, text=True, env=env, check=False)
    assert second.returncode == 1 and "APPROVAL_ALREADY_CONSUMED" in second.stdout
    in_ci = subprocess.run(consume[:-1], capture_output=True, text=True, env={**env, "GITHUB_ACTIONS": "true"},
                           check=False)
    assert in_ci.returncode == 1 and "CI_CANNOT_AUTHORIZE_DEPLOYMENT" in in_ci.stdout


@pytest.mark.parametrize("bad_kind", [["wheel"], {"k": "wheel"}, 7, None])
def test_cr04b_n02_non_string_artifact_kind_is_rejected_not_raised(tmp_path: Path, bad_kind: object) -> None:
    release, _ = build_release_dir(tmp_path)
    _rewrite_manifest(release, lambda m: m["artifacts"][0].update(kind=bad_kind))
    assert "MANIFEST_SCHEMA_INVALID:artifacts" in _full_codes(tmp_path, release, COMMIT_PIN)
