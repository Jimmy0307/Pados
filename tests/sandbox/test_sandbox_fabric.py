from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from pados.enterprise.classification import DataClassification
from pados.sandbox.adapter import Phase, SimulatedSandboxAdapter
from pados.sandbox.exchange import assert_mount_allowed
from pados.sandbox.policy import (
    DEFAULT_PROFILE,
    Action,
    DataEgressPolicy,
    Direction,
    IsolationClass,
    PolicyError,
    SandboxProfile,
    SandboxRequest,
    Zone,
    profile_policy,
    validate_request,
)
from tests.windows_links import create_link_or_skip


def test_default_profile_is_l3_isolated_and_never_l2() -> None:
    assert DEFAULT_PROFILE is SandboxProfile.L3_ISOLATED
    assert SandboxRequest().profile is SandboxProfile.L3_ISOLATED
    with pytest.raises(PolicyError, match="L2_PROFILE_REQUIRES_EXPLICIT_OPT_IN"):
        validate_request(SandboxRequest(profile=SandboxProfile.L2_SECURITY_LAB, lab_targets=("lab-synthetic:a",)))


@pytest.mark.parametrize("profile", list(SandboxProfile))
def test_every_profile_denies_by_default_and_blocks_internet_host_lan(profile: SandboxProfile) -> None:
    policy = profile_policy(profile)
    assert policy.default_action is Action.DENY
    assert policy.internet_egress is False
    for dest in (Zone.INTERNET, Zone.HOST, Zone.LAN, Zone.MANAGEMENT):
        assert policy.decide(source=Zone.SANDBOX, destination=dest, direction=Direction.EGRESS,
                             protocol="tcp", port=443) is Action.DENY


def test_l3_isolated_allows_only_artifact_exchange() -> None:
    policy = profile_policy(SandboxProfile.L3_ISOLATED)
    assert policy.decide(source=Zone.SANDBOX, destination=Zone.PADOS_ARTIFACT_EXCHANGE,
                         direction=Direction.EGRESS, protocol="tcp", port=443) is Action.ALLOW
    assert policy.decide(source=Zone.SANDBOX, destination=Zone.PADOS_ARTIFACT_EXCHANGE,
                         direction=Direction.EGRESS, protocol="tcp", port=22) is Action.DENY
    assert policy.decide(source=Zone.SANDBOX, destination=Zone.LAB_SEGMENT,
                         direction=Direction.EGRESS, protocol="tcp", port=80) is Action.DENY


def test_offline_profile_allows_nothing() -> None:
    policy = profile_policy(SandboxProfile.OFFLINE_SANDBOX)
    assert policy.decide(source=Zone.SANDBOX, destination=Zone.PADOS_ARTIFACT_EXCHANGE,
                         direction=Direction.EGRESS, protocol="tcp", port=443) is Action.DENY


def test_redteam_profile_requires_vm_and_synthetic_lab_targets() -> None:
    ok = SandboxRequest(profile=SandboxProfile.L3_REDTEAM, lab_targets=("lab-synthetic:web-fixture",))
    assert validate_request(ok) is ok
    with pytest.raises(PolicyError, match="LAB_TARGET_MUST_BE_SYNTHETIC"):
        validate_request(SandboxRequest(profile=SandboxProfile.L3_REDTEAM, lab_targets=("198.51.100.7",)))
    with pytest.raises(PolicyError, match="LAB_PROFILE_REQUIRES_VM_ISOLATION"):
        validate_request(SandboxRequest(profile=SandboxProfile.L3_REDTEAM, isolation=IsolationClass.CONTAINER,
                                        lab_targets=("lab-synthetic:x",)))
    with pytest.raises(PolicyError, match="LAB_TARGETS_REQUIRED"):
        validate_request(SandboxRequest(profile=SandboxProfile.L3_REDTEAM))
    with pytest.raises(PolicyError, match="LAB_TARGETS_NOT_ALLOWED_FOR_PROFILE"):
        validate_request(SandboxRequest(lab_targets=("lab-synthetic:x",)))
    lab = profile_policy(SandboxProfile.L3_REDTEAM)
    assert lab.decide(source=Zone.SANDBOX, destination=Zone.LAB_SEGMENT, direction=Direction.EGRESS,
                      protocol="tcp", port=80) is Action.ALLOW
    assert lab.decide(source=Zone.LAB_SEGMENT, destination=Zone.HOST, direction=Direction.EGRESS,
                      protocol="tcp", port=80) is Action.DENY


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("C:\\Users\\someone", "MOUNT_WINDOWS_USER_PROFILE_FORBIDDEN"),
        ("C:/Users", "MOUNT_WINDOWS_USER_PROFILE_FORBIDDEN"),
        ("D:\\", "MOUNT_WHOLE_DRIVE_FORBIDDEN"),
        ("C:\\data", "MOUNT_HOST_DRIVE_FORBIDDEN"),
        ("/", "MOUNT_WHOLE_DRIVE_FORBIDDEN"),
        ("/Users/someone", "MOUNT_HOST_PATH_FORBIDDEN"),
        ("/etc", "MOUNT_HOST_PATH_FORBIDDEN"),
        ("/var/lib/postgresql/18/main", "MOUNT_HOST_PATH_FORBIDDEN"),
    ],
)
def test_host_mounts_are_forbidden(tmp_path: Path, source: str, code: str) -> None:
    with pytest.raises(PolicyError, match=code):
        assert_mount_allowed(source, staging_root=tmp_path / "staging")


def test_repository_and_protected_paths_cannot_be_mounted(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    (staging / "run-1").mkdir(parents=True)
    assert assert_mount_allowed(str(staging / "run-1"), staging_root=staging) == Path(
        os.path.realpath(staging / "run-1")
    )
    repo = Path(__file__).resolve().parents[2]
    with pytest.raises(PolicyError):
        assert_mount_allowed(str(repo), staging_root=staging, protected=(repo,))
    with pytest.raises(PolicyError) as outside:
        assert_mount_allowed(str(tmp_path), staging_root=staging)
    # On Windows the pytest temp root sits under C:\Users, so the more specific code applies.
    assert outside.value.code in {"MOUNT_OUTSIDE_STAGING_FORBIDDEN", "MOUNT_WINDOWS_USER_PROFILE_FORBIDDEN"}
    link = staging / "escape"
    create_link_or_skip(tmp_path, link, directory=True)
    with pytest.raises(PolicyError):
        assert_mount_allowed(str(link), staging_root=staging)


def _upper(inputs: Mapping[str, bytes]) -> dict[str, bytes]:
    return {f"{name}.out.txt": data.upper() for name, data in inputs.items()}


def test_simulated_lifecycle_is_ephemeral_and_artifact_only(tmp_path: Path) -> None:
    before = set(Path(tempfile.gettempdir()).glob("pados-sbx-*"))
    adapter = SimulatedSandboxAdapter(_upper, base_dir=tmp_path)
    run = adapter.run(SandboxRequest(), {"input.txt": b"hello"}, DataEgressPolicy())
    assert run.phases == [p.value for p in (Phase.CREATED, Phase.STAGED, Phase.EXECUTED, Phase.CAPTURED,
                                            Phase.VERIFIED, Phase.DESTROYED)]
    assert [a.name for a in run.artifacts] == ["input.txt.out.txt"]
    assert run.to_dict()["semantic_authority"] == "NONE"
    assert list(tmp_path.glob("pados-sbx-*")) == []
    assert set(Path(tempfile.gettempdir()).glob("pados-sbx-*")) == before


def test_disallowed_artifact_fails_and_workspace_is_still_destroyed(tmp_path: Path) -> None:
    adapter = SimulatedSandboxAdapter(lambda inputs: {"payload.exe": b"x"}, base_dir=tmp_path)
    run = adapter.run(SandboxRequest(), {"a.txt": b"a"}, DataEgressPolicy())
    assert run.failure == "ARTIFACT_TYPE_NOT_ALLOWED"
    assert run.phases[-2:] == [Phase.FAILED.value, Phase.DESTROYED.value]
    assert run.artifacts == ()
    assert list(tmp_path.glob("pados-sbx-*")) == []


def test_egress_policy_gates_classification_and_remote_nodes() -> None:
    policy = DataEgressPolicy(classification_ceiling=DataClassification.INTERNAL)
    policy.admit_input(DataClassification.PUBLIC, IsolationClass.VM_SANDBOX)
    with pytest.raises(PolicyError, match="CLASSIFICATION_ABOVE_EGRESS_CEILING"):
        policy.admit_input(DataClassification.RESTRICTED, IsolationClass.VM_SANDBOX)
    with pytest.raises(PolicyError, match="REMOTE_NODE_NOT_ELIGIBLE"):
        policy.admit_input(DataClassification.PUBLIC, IsolationClass.REMOTE_NODE)
    with pytest.raises(PolicyError, match="ARTIFACT_TOO_LARGE"):
        DataEgressPolicy(max_artifact_bytes=10).admit_artifact("a.txt", 11)
    assert DataEgressPolicy().internet_egress is False and DataEgressPolicy().cloud_eligible is False
