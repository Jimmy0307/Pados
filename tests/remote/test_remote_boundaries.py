from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

import pytest

from pados.config import Settings
from pados.enterprise.classification import DataClassification
from pados.remote.auth import RemoteActorRegistry, authorize_project, may_approve
from pados.remote.deployment import check_remote_deployment, classify_bind_address
from pados.remote.jobs import ControlAction, ControlState, RemoteJobController
from pados.remote.staging import (
    BulkInbox,
    ScanStatus,
    StagingArea,
    StagingError,
    StagingState,
    validate_upload_name,
)


def _hardened_settings() -> Settings:
    value = secrets.token_hex(16)
    return Settings(
        PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(value.encode()).hexdigest().upper(),
        PADOS_DB_PASSWORD=secrets.token_hex(12),
    )


def test_default_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped defaults, independent of any disposable credentials in the gate environment.
    for name in ("PADOS_DB_PASSWORD", "PADOS_CONTROL_API_TOKEN_HASH"):
        monkeypatch.delenv(name, raising=False)
    result = check_remote_deployment(Settings(_env_file=None), remote_bind_host="100.64.1.2")
    assert not result.ok
    assert "DEFAULT_CONTROL_CREDENTIAL_FORBIDDEN" in result.violations
    assert "DEFAULT_DATABASE_CREDENTIAL_FORBIDDEN" in result.violations


@pytest.mark.parametrize(
    ("host", "violation"),
    [
        ("0.0.0.0", "WILDCARD_BIND_FORBIDDEN"),
        ("8.8.8.8", "DIRECT_PUBLIC_INTERNET_UNSUPPORTED"),
        ("pados.example", "BIND_ADDRESS_MUST_BE_LITERAL_IP"),
    ],
)
def test_non_private_binds_are_rejected(host: str, violation: str) -> None:
    result = check_remote_deployment(_hardened_settings(), remote_bind_host=host)
    assert violation in result.violations


@pytest.mark.parametrize("host", ["100.64.1.2", "172.16.8.1", "192.168.1.20", "fd7a:115c::1", "127.0.0.1"])
def test_private_overlay_binds_pass_with_hardened_settings(host: str) -> None:
    result = check_remote_deployment(_hardened_settings(), remote_bind_host=host)
    assert result.ok, result.violations
    assert result.network_model.value == "PRIVATE_OVERLAY_ONLY"


def test_database_must_stay_loopback() -> None:
    settings = _hardened_settings().model_copy(update={"db_host": "172.16.0.5"})
    assert "DATABASE_MUST_NOT_BE_REMOTELY_EXPOSED" in check_remote_deployment(
        settings, remote_bind_host="172.16.8.1"
    ).violations
    assert classify_bind_address("::") == "WILDCARD"


def _registry(presented: str) -> RemoteActorRegistry:
    return RemoteActorRegistry.from_mapping(
        {
            "actors": [
                {
                    "actor_id": "owner",
                    "credential_hash": hashlib.sha256(presented.encode()).hexdigest(),
                    "projects": ["proj-alpha"],
                    "may_approve": True,
                },
                {
                    "actor_id": "viewer",
                    "credential_hash": hashlib.sha256(b"other").hexdigest(),
                    "projects": ["proj-alpha"],
                },
            ]
        }
    )


def test_remote_auth_binds_actor_and_project_allowlist() -> None:
    presented = secrets.token_hex(16)
    registry = _registry(presented)
    session = registry.authenticate("owner", presented)
    assert session.actor.actor_id == "owner" and session.session_id.startswith("rs-")
    authorize_project(session, "proj-alpha")
    with pytest.raises(PermissionError, match="REMOTE_PROJECT_NOT_ALLOWED"):
        authorize_project(session, "proj-beta")
    for actor, value in [("owner", "wrong"), ("ghost", presented), ("owner", None)]:
        with pytest.raises(PermissionError):
            registry.authenticate(actor, value)
    assert may_approve(registry, "owner", "proj-alpha")
    assert not may_approve(registry, "viewer", "proj-alpha")
    assert not may_approve(registry, "owner", "proj-beta")


@pytest.mark.parametrize(
    "name",
    ["../x.txt", "a/b.txt", "a\\b.txt", "/etc/passwd", "C:evil.txt", "NUL", "com1.txt", "", "x\x00y"],
)
def test_upload_names_reject_traversal_absolute_and_device(name: str) -> None:
    with pytest.raises(StagingError):
        validate_upload_name(name)


def _area(tmp_path: Path, scanner: object = None) -> StagingArea:
    return StagingArea(
        tmp_path / "staging",
        scanner=scanner,  # type: ignore[arg-type]
        project_ceiling=lambda pid: DataClassification.CONFIDENTIAL if pid == "proj-alpha" else None,
    )


def test_staging_records_hash_identity_and_binding(tmp_path: Path) -> None:
    area = _area(tmp_path)
    staged = area.stage(
        project_id="proj-alpha", actor_id="owner", name="notes.md", content=b"hello", classification="internal"
    )
    assert staged.state is StagingState.STAGED
    assert staged.scan_status is ScanStatus.MALWARE_SCAN_NOT_CONFIGURED
    assert staged.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert staged.ref.startswith("staged:stg-")
    assert staged.source.channel == "REMOTE_UPLOAD"
    assert area.verify(staged.staged_id)
    assert area.get("../etc") is None


def test_staging_quarantines_on_ceiling_executable_and_scan(tmp_path: Path) -> None:
    area = _area(tmp_path, scanner=lambda data: b"EICAR-SYNTHETIC" not in data)
    over = area.stage(project_id="proj-alpha", actor_id="o", name="a.txt", content=b"x", classification="restricted")
    exe = area.stage(project_id="proj-alpha", actor_id="o", name="tool.exe", content=b"x", classification="public")
    bad = area.stage(
        project_id="proj-alpha", actor_id="o", name="b.txt", content=b"EICAR-SYNTHETIC", classification="public"
    )
    assert over.reasons == ("CLASSIFICATION_ABOVE_PROJECT_CEILING",)
    assert exe.reasons == ("EXECUTABLE_CONTENT_TYPE",)
    assert bad.scan_status is ScanStatus.INFECTED
    assert {over.state, exe.state, bad.state} == {StagingState.QUARANTINED}
    with pytest.raises(StagingError, match="PROJECT_NOT_REGISTERED"):
        area.stage(project_id="unknown", actor_id="o", name="c.txt", content=b"x", classification="public")


def test_bulk_inbox_registers_external_transfer_and_rejects_symlinks(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "dataset.csv").write_bytes(b"a,b\n1,2\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    bulk = BulkInbox(inbox, _area(tmp_path))
    staged = bulk.register(project_id="proj-alpha", actor_id="owner", name="dataset.csv", classification="internal")
    assert staged.source.channel == "BULK_EXTERNAL_TRANSFER"
    (inbox / "sub").mkdir()
    with pytest.raises(StagingError, match="INBOX_FILE_NOT_REGULAR"):
        bulk.register(project_id="proj-alpha", actor_id="owner", name="sub", classification="internal")
    try:
        (inbox / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not permitted on this host (Windows without the privilege)")
    with pytest.raises(StagingError, match="INBOX_FILE_NOT_REGULAR"):
        bulk.register(project_id="proj-alpha", actor_id="owner", name="link.txt", classification="internal")


class _Backend:
    def __init__(self, *, pause: bool, cancel: bool) -> None:
        self.supports_pause = pause
        self.supports_cancel = cancel
        self.terminal: set[str] = set()
        self.cancel_calls: list[str] = []

    def exists(self, run_id: str) -> bool:
        return run_id.startswith("run-")

    def is_terminal(self, run_id: str) -> bool:
        return run_id in self.terminal

    def pause(self, run_id: str) -> bool:
        return True

    def resume(self, run_id: str) -> bool:
        return True

    def request_cancel(self, run_id: str) -> bool:
        self.cancel_calls.append(run_id)
        return True


def test_pause_unsupported_and_cancel_lifecycle() -> None:
    backend = _Backend(pause=False, cancel=True)
    ctl = RemoteJobController(backend)
    assert ctl.control(run_id="run-1", action=ControlAction.PAUSE, actor_id="o").state == ControlState.PAUSE_UNSUPPORTED
    record = ctl.control(run_id="run-1", action=ControlAction.CANCEL, actor_id="o")
    assert record.state == ControlState.CANCEL_ACKNOWLEDGED
    backend.terminal.add("run-1")
    refreshed = ctl.refresh("run-1")
    assert refreshed is not None and refreshed.state == ControlState.CANCEL_COMPLETED
    assert refreshed.to_dict()["changes_semantic_state"] is False
    assert ctl.control(run_id="nope", action=ControlAction.CANCEL, actor_id="o").state == ControlState.JOB_NOT_FOUND


def test_cancel_unsupported_and_disconnect_does_not_cancel() -> None:
    backend = _Backend(pause=True, cancel=False)
    ctl = RemoteJobController(backend)
    assert ctl.control(run_id="run-2", action=ControlAction.CANCEL, actor_id="o").state == ControlState.CANCEL_UNSUPPORTED
    assert ctl.control(run_id="run-2", action=ControlAction.PAUSE, actor_id="o").state == ControlState.PAUSED
    ctl.on_client_disconnect("rs-abc")
    assert backend.cancel_calls == []
