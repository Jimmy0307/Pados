"""Security Hardening V1: file staging and bulk-inbox boundary (remote uploads, SFTP inbox)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from pados.enterprise.classification import DataClassification
from pados.remote import staging as staging_module
from pados.remote.staging import BulkInbox, StagingArea, StagingError, StagingState, validate_upload_name


def _area(tmp_path: Path) -> StagingArea:
    return StagingArea(tmp_path / "staging",
                       project_ceiling=lambda pid: DataClassification.CONFIDENTIAL if pid == "proj" else None)


@pytest.mark.parametrize("name", [
    "..", "...", "a/../b", "..\\x", "/abs.txt", "\\\\server\\share\\x", "C:x.txt", "file.txt:ads",
    "CON", "aux.txt", "LPT1", ".hidden", "-rf", " lead.txt", "x" * 201, "a\nb.txt", "a\x00b",
])
def test_upload_names_are_plain_file_names_only(name: str) -> None:
    with pytest.raises(StagingError):
        validate_upload_name(name)


def test_staged_content_never_lands_under_the_client_chosen_name(tmp_path: Path) -> None:
    area = _area(tmp_path)
    staged = area.stage(project_id="proj", actor_id="a", name="report.md", content=b"x", classification="internal")
    stored = sorted(p.name for p in (tmp_path / "staging" / staged.staged_id).iterdir())
    assert stored == ["content.bin", "record.json"]
    assert area.get("../../etc/passwd") is None and area.get(staged.staged_id + "/..") is None
    assert staged.state is StagingState.STAGED


def test_executable_and_over_ceiling_uploads_are_quarantined_not_usable(tmp_path: Path) -> None:
    area = _area(tmp_path)
    exe = area.stage(project_id="proj", actor_id="a", name="setup.PS1", content=b"x", classification="public")
    hot = area.stage(project_id="proj", actor_id="a", name="a.txt", content=b"x", classification="restricted")
    assert exe.state is StagingState.QUARANTINED and hot.state is StagingState.QUARANTINED
    with pytest.raises(StagingError, match="PROJECT_NOT_REGISTERED"):
        area.stage(project_id="other", actor_id="a", name="a.txt", content=b"x", classification="public")


def test_bulk_inbox_symlink_swapped_in_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIXED: lstat/resolve checks were followed by a path-based read (TOCTOU symlink swap)."""
    private = tmp_path / "home-base-private.txt"
    private.write_text("operator-only material", encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    dropped = inbox / "report.txt"
    dropped.write_text("dropped by the external transport", encoding="utf-8")
    try:
        (tmp_path / "probe-link").symlink_to(private)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")

    base = type(Path())

    class SwapAfterResolve(base):  # type: ignore[valid-type, misc]
        """Emulates the transport user replacing the file between check and use."""

        def resolve(self, strict: bool = False) -> Any:
            resolved = super().resolve(strict)
            if self.name == "report.txt" and not self.is_symlink():
                self.unlink()
                self.symlink_to(private)
            return resolved

    monkeypatch.setattr(staging_module, "Path", SwapAfterResolve)
    bulk = BulkInbox(inbox, _area(tmp_path))
    with pytest.raises(StagingError) as raised:
        bulk.register(project_id="proj", actor_id="sftp", name="report.txt", classification="internal")
    assert raised.value.code in {"INBOX_FILE_NOT_REGULAR", "INBOX_FILE_CHANGED"}
    secret_digest = hashlib.sha256(private.read_bytes()).hexdigest()
    records = list((tmp_path / "staging").glob("*/record.json"))
    assert not any(secret_digest in record.read_text(encoding="utf-8") for record in records)


def test_bulk_inbox_rejects_directories_symlinks_and_missing_files(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "dir.txt").mkdir()
    bulk = BulkInbox(inbox, _area(tmp_path))
    with pytest.raises(StagingError, match="INBOX_FILE_NOT_REGULAR"):
        bulk.register(project_id="proj", actor_id="a", name="dir.txt", classification="internal")
    with pytest.raises(StagingError, match="INBOX_FILE_NOT_FOUND"):
        bulk.register(project_id="proj", actor_id="a", name="missing.txt", classification="internal")
    (inbox / "ok.txt").write_bytes(b"fine")
    staged = bulk.register(project_id="proj", actor_id="a", name="ok.txt", classification="internal")
    assert staged.sha256 == hashlib.sha256(b"fine").hexdigest()
