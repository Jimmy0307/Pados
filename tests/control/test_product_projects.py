# SPDX-License-Identifier: AGPL-3.0-only
"""Project creation inputs, the derived folder, the shared proof-scope setup, and the read projections' file reads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from pados.control import product_projects, work_projection
from pados.control.product_errors import ProductControlError, require_uuid
from pados.control.product_projects import ProjectCreate


def _refused(payload: dict[str, Any]) -> str:
    with pytest.raises(ProductControlError) as excinfo:
        product_projects.validate_request(ProjectCreate(idempotency_key="k", **payload))
    return excinfo.value.code


def test_request_validation_uses_closed_codes() -> None:
    assert product_projects.validate_request(ProjectCreate(name="  文件檢查 Demo ", idempotency_key="k")) == (
        "文件檢查 Demo", "", "internal")
    assert _refused({"name": "   "}) == "PROJECT_NAME_INVALID"
    assert _refused({"name": "x" * 121}) == "PROJECT_NAME_INVALID"
    assert _refused({"name": "a\nb"}) == "PROJECT_NAME_INVALID"
    assert _refused({"name": "a‮b"}) == "PROJECT_NAME_INVALID"  # bidi override
    assert _refused({"name": "ok", "description": "x" * 2001}) == "PROJECT_DESCRIPTION_INVALID"
    assert _refused({"name": "ok", "classification": "top-secret"}) == "PROJECT_CLASSIFICATION_INVALID"
    assert _refused({"name": "ok", "description": "https://u:hunter2x@example.com"}) == "PROJECT_CONTENT_REJECTED"
    multi = ProjectCreate(name="ok", description="line one\nline two", idempotency_key="k")
    assert product_projects.validate_request(multi)[1] == "line one\nline two"


@pytest.mark.parametrize(("name", "slug"), [
    ("Docs Check", "docs-check"), ("文件檢查", "project"), ("../../etc", "etc"), ("CON", "con"),
    ("Ünïcode Name!", "unicode-name"), ("a" * 80, "a" * 40),
])
def test_folder_name_is_derived_from_the_name_only(name: str, slug: str) -> None:
    folder = product_projects.project_folder_name(name)
    digest = hashlib.sha256(name.casefold().encode()).hexdigest()[:8]
    assert folder == f"{slug}-{digest}"
    assert "/" not in folder and "\\" not in folder and ".." not in folder
    assert product_projects.project_folder_name(name.casefold()) == folder  # names differ by case: same folder


def test_workspace_root_must_be_configured_and_absolute(tmp_path: Path) -> None:
    class S:
        product_workspace_root = ""

    with pytest.raises(ProductControlError) as missing:
        product_projects.approved_workspace_root(S())
    assert (missing.value.code, missing.value.status) == ("PROJECT_WORKSPACE_ROOT_NOT_CONFIGURED", 503)
    S.product_workspace_root = "relative/workspace"
    with pytest.raises(ProductControlError) as relative:
        product_projects.approved_workspace_root(S())
    assert relative.value.code == "PROJECT_WORKSPACE_ROOT_INVALID"
    S.product_workspace_root = str(tmp_path / "ws")
    root = product_projects.approved_workspace_root(S())
    folder = product_projects.project_folder(root, "Demo")
    assert folder.parent == root and not folder.exists()


def test_a_linked_project_folder_is_refused(tmp_path: Path) -> None:
    import os

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / product_projects.project_folder_name("Demo")
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("no symlink privilege")
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    with pytest.raises(ProductControlError) as excinfo:
        product_projects.project_folder(workspace.resolve(), "Demo")
    assert excinfo.value.code == "PROJECT_FOLDER_OUTSIDE_WORKSPACE"


def test_require_uuid() -> None:
    assert require_uuid("11111111-2222-4333-8444-555555555555") == "11111111-2222-4333-8444-555555555555"
    with pytest.raises(ProductControlError):
        require_uuid("1; DROP TABLE projects")


def test_cli_and_control_share_one_proof_scope_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    import pados.cli.main as cli_main
    from pados.control import project_setup

    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(project_setup, "ensure_local_proof_scope", lambda s, pid: (calls.append((s, pid)), 7)[1])
    marker = object()
    monkeypatch.setattr(cli_main, "get_settings", lambda: marker)
    assert cli_main._ensure_local_proof_scope("p-1") == 7
    assert calls == [(marker, "p-1")]


class _Rows:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def mappings(self) -> _Rows:
        return self

    def first(self) -> dict[str, Any] | None:
        return self.row


class _Session:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def execute(self, *_args: Any, **_kwargs: Any) -> _Rows:
        return _Rows(self.row)


def test_artifact_content_is_bounded_verified_and_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    stderr = root / "worker_jobs" / "j" / "stderr.txt"
    stderr.parent.mkdir(parents=True)
    report = json.dumps({"broken": [{"file": "guide.md", "target": "missing.md"}]}).encode()
    stderr.write_bytes(report)
    good = {"path": stderr.as_posix(), "sha256": hashlib.sha256(report).hexdigest()}
    read = work_projection.artifact_content(_Session(good), root, "j", "stderr")  # type: ignore[arg-type]
    assert read["integrity"] == "VERIFIED" and json.loads(read["content"])["broken"][0]["target"] == "missing.md"

    def refused(row: dict[str, Any] | None, kind: str = "stderr") -> str:
        with pytest.raises(ProductControlError) as excinfo:
            work_projection.artifact_content(_Session(row), root, "j", kind)  # type: ignore[arg-type]
        return excinfo.value.code

    assert refused(good, "handoff_json") == "ARTIFACT_TYPE_NOT_READABLE"
    assert refused(None) == "ARTIFACT_NOT_FOUND"
    assert refused({**good, "sha256": "0" * 64}) == "ARTIFACT_INTEGRITY_MISMATCH"
    outside = tmp_path / "elsewhere.txt"
    outside.write_bytes(b"x")
    assert refused({"path": outside.as_posix(), "sha256": hashlib.sha256(b"x").hexdigest()}) == \
        "ARTIFACT_PATH_OUTSIDE_ROOT"
    assert refused({"path": (root / ".." / "elsewhere.txt").as_posix(), "sha256": "0" * 64}) == \
        "ARTIFACT_PATH_OUTSIDE_ROOT"
    big = root / "worker_jobs" / "j" / "stdout.txt"
    big.write_bytes(b"x" * (work_projection.ARTIFACT_MAX_BYTES + 1))
    assert refused({"path": big.as_posix(), "sha256": "0" * 64}, "stdout") == "ARTIFACT_TOO_LARGE"


def test_step_up_readiness_counts_enrolled_approvers_only(tmp_path: Path) -> None:
    project = "11111111-2222-4333-8444-555555555555"

    class S:
        remote_config = ""

    assert work_projection.step_up_readiness(S(), project)["remote_console"] == "NOT_CONFIGURED"
    config = tmp_path / "remote.json"
    actor = {"actor_id": "owner", "credential_hash": "A" * 64, "projects": [project], "may_approve": True}
    config.write_text(json.dumps({"bind_host": "127.0.0.1", "staging_root": str(tmp_path / "s"),
                                  "ledger_path": str(tmp_path / "l" / "x.jsonl"), "actors": [actor],
                                  "presence": {"rp_id": "localhost", "origins": ["http://localhost:5173"]}}),
                      encoding="utf-8")
    S.remote_config = str(config)
    status = work_projection.step_up_readiness(S(), project)
    assert status == {"remote_console": "CONFIGURED", "presence_verifier": "CONFIGURED",
                      "presence_device": "NOT_ENROLLED", "approvers": 1}
    assert "owner" not in json.dumps(status)
    assert work_projection.step_up_readiness(S(), "99999999-2222-4333-8444-555555555555")["approvers"] == 0
    config.write_text("{not json", encoding="utf-8")
    assert work_projection.step_up_readiness(S(), project)["remote_console"] == "CONFIGURATION_UNREADABLE"
