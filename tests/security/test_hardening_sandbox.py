"""Security Hardening V1: Sandbox Fabric (network policy, mounts, artifact-only exchange)."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from pados.sandbox.adapter import Phase, SimulatedSandboxAdapter
from pados.sandbox.exchange import assert_mount_allowed
from pados.sandbox.policy import (
    Action,
    DataEgressPolicy,
    Direction,
    IsolationClass,
    NetworkPolicy,
    NetworkRule,
    PolicyError,
    SandboxProfile,
    SandboxRequest,
    Zone,
    profile_policy,
    validate_request,
)
from tests.windows_links import create_link_or_skip


def _echo(inputs: Mapping[str, bytes]) -> Mapping[str, bytes]:
    return {"result.txt": b"".join(inputs.values())}


@pytest.mark.parametrize("escape", ["../../escaped.txt", "../escaped.txt", "sub/escaped.txt", "..\\escaped.txt"])
def test_sandbox_output_names_cannot_escape_the_outbox(tmp_path: Path, escape: str) -> None:
    """FIXED: output artifact names from the sandboxed step were joined to the outbox unchecked."""
    adapter = SimulatedSandboxAdapter(lambda _inputs: {escape: b"planted"}, base_dir=tmp_path)
    run = adapter.run(SandboxRequest(), {"input.txt": b"x"}, DataEgressPolicy())
    assert run.failure == "ARTIFACT_NAME_INVALID"
    assert Phase.VERIFIED.value not in run.phases and run.phases[-1] == Phase.DESTROYED.value
    assert not list(tmp_path.rglob("escaped.txt"))


def test_sandbox_output_absolute_names_cannot_write_the_host(tmp_path: Path) -> None:
    target = tmp_path / "host-file.txt"
    adapter = SimulatedSandboxAdapter(lambda _inputs: {str(target): b"planted"}, base_dir=tmp_path / "sbx")
    (tmp_path / "sbx").mkdir()
    run = adapter.run(SandboxRequest(), {"input.txt": b"x"}, DataEgressPolicy())
    assert run.failure == "ARTIFACT_NAME_INVALID" and not target.exists()


@pytest.mark.parametrize("name", ["C:evil.txt", "evil.txt:stream", "", ".", "..", "CON", "nul.txt", "a\x00b.txt"])
def test_sandbox_input_names_reject_drive_stream_device_and_empty_forms(tmp_path: Path, name: str) -> None:
    """FIXED: drive-relative / ADS / device names passed the slash-and-dot-dot check."""
    run = SimulatedSandboxAdapter(_echo, base_dir=tmp_path).run(SandboxRequest(), {name: b"x"}, DataEgressPolicy())
    assert run.failure == "INPUT_NAME_INVALID"
    assert run.phases[-1] == Phase.DESTROYED.value and not run.artifacts


def test_benign_artifact_exchange_still_verifies(tmp_path: Path) -> None:
    run = SimulatedSandboxAdapter(_echo, base_dir=tmp_path).run(
        SandboxRequest(), {"input.txt": b"hello"}, DataEgressPolicy())
    assert run.failure is None and Phase.VERIFIED.value in run.phases
    assert [a.name for a in run.artifacts] == ["result.txt"] and run.semantic_authority == "NONE"
    assert not list(tmp_path.iterdir())  # workspace destroyed


@pytest.mark.parametrize("destination", [Zone.HOST, Zone.LAN])
def test_explicit_allow_rules_cannot_open_host_or_lan(destination: Zone) -> None:
    rogue = NetworkRule(Zone.SANDBOX, destination, Direction.EGRESS, "any", None, Action.ALLOW)
    policy = NetworkPolicy(profile=SandboxProfile.L3_ISOLATED, rules=(rogue,), default_action=Action.ALLOW)
    for source in (Zone.SANDBOX, Zone.LAB_SEGMENT):
        assert policy.decide(source=source, destination=destination, direction=Direction.EGRESS,
                             protocol="tcp", port=22) is Action.DENY


@pytest.mark.parametrize("profile", list(SandboxProfile))
def test_no_profile_reaches_internet_or_management(profile: SandboxProfile) -> None:
    policy = profile_policy(profile)
    assert policy.internet_egress is False and policy.management_access is False
    for destination in (Zone.INTERNET, Zone.MANAGEMENT, Zone.HOST, Zone.LAN):
        assert policy.decide(source=Zone.SANDBOX, destination=destination, direction=Direction.EGRESS,
                             protocol="tcp", port=443) is Action.DENY
    rule = NetworkRule(Zone.SANDBOX, Zone.MANAGEMENT, Direction.EGRESS, "any", None, Action.ALLOW)
    widened = NetworkPolicy(profile=profile, rules=(rule,))
    assert widened.decide(source=Zone.SANDBOX, destination=Zone.MANAGEMENT, direction=Direction.EGRESS,
                          protocol="tcp", port=22) is Action.DENY


def test_lab_profiles_require_vm_isolation_explicit_opt_in_and_synthetic_targets() -> None:
    with pytest.raises(PolicyError, match="L2_PROFILE_REQUIRES_EXPLICIT_OPT_IN"):
        validate_request(SandboxRequest(profile=SandboxProfile.L2_SECURITY_LAB, lab_targets=("lab-synthetic:a",)))
    with pytest.raises(PolicyError, match="LAB_PROFILE_REQUIRES_VM_ISOLATION"):
        validate_request(SandboxRequest(profile=SandboxProfile.L3_REDTEAM, isolation=IsolationClass.CONTAINER,
                                        lab_targets=("lab-synthetic:a",)))
    with pytest.raises(PolicyError, match="LAB_TARGET_MUST_BE_SYNTHETIC"):
        validate_request(SandboxRequest(profile=SandboxProfile.L3_REDTEAM, lab_targets=("10.0.0.5",)))
    with pytest.raises(PolicyError, match="LAB_TARGETS_NOT_ALLOWED_FOR_PROFILE"):
        validate_request(SandboxRequest(lab_targets=("lab-synthetic:a",)))


@pytest.mark.parametrize("source", ["/", "/etc", "/home", "/root", "/proc/self", "C:\\", "C:\\Users\\me",
                                    "D:\\data", "\x00", ""])
def test_host_paths_are_never_mountable(tmp_path: Path, source: str) -> None:
    with pytest.raises(PolicyError):
        assert_mount_allowed(source, staging_root=tmp_path / "staging")


def test_symlink_from_staging_to_protected_repository_is_refused(tmp_path: Path) -> None:
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    staging.mkdir()
    repo.mkdir()
    link = staging / "run-1"
    create_link_or_skip(repo, link, directory=True)
    with pytest.raises(PolicyError):
        assert_mount_allowed(str(link), staging_root=staging, protected=(repo,))
    with pytest.raises(PolicyError, match="MOUNT_PROTECTED_PATH_FORBIDDEN"):
        assert_mount_allowed(str(tmp_path), staging_root=staging, protected=(repo,))


def test_egress_policy_admits_only_bounded_text_artifacts_and_classified_inputs() -> None:
    from pados.enterprise.classification import DataClassification

    egress = DataEgressPolicy()
    for name in ("payload.exe", "script.ps1", "noext", "archive.zip"):
        with pytest.raises(PolicyError, match="ARTIFACT_TYPE_NOT_ALLOWED"):
            egress.admit_artifact(name, 1)
    with pytest.raises(PolicyError, match="ARTIFACT_TOO_LARGE"):
        egress.admit_artifact("big.txt", egress.max_artifact_bytes + 1)
    with pytest.raises(PolicyError, match="CLASSIFICATION_ABOVE_EGRESS_CEILING"):
        egress.admit_input(DataClassification.RESTRICTED, IsolationClass.VM_SANDBOX)
    with pytest.raises(PolicyError, match="REMOTE_NODE_NOT_ELIGIBLE"):
        egress.admit_input(DataClassification.PUBLIC, IsolationClass.REMOTE_NODE)
