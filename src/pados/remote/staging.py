# SPDX-License-Identifier: AGPL-3.0-only
"""Remote file staging and bulk-transfer boundary.

Uploads land in a PADOS-owned staging area, never in a project workspace. Each
file gets a SHA-256, a Source Identity, a classification and a project binding,
and becomes ``staged:<id>`` for use in a RemoteIntentEnvelope. Bulk transfer
uses external transport (for example SFTP into an inbox directory): the
transport moves bytes, PADOS assigns meaning by registering inbox files through
the same pipeline. PADOS embeds no FTP/SFTP server.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from pados.enterprise.classification import DataClassification, parse_classification


class StagingState(StrEnum):
    STAGED = "STAGED"
    QUARANTINED = "QUARANTINED"


class ScanStatus(StrEnum):
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    MALWARE_SCAN_NOT_CONFIGURED = "MALWARE_SCAN_NOT_CONFIGURED"


class StagingError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# A scanner returns True when content is clean. None means "not configured".
Scanner = Callable[[bytes], bool]

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
_QUARANTINE_SUFFIXES = frozenset(
    {".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".msi", ".scr", ".com", ".vbs", ".jar"}
)
_WINDOWS_DEVICE = re.compile(r"(?i)^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.()+-]{0,199}")


def validate_upload_name(name: str) -> str:
    """Accept a plain file name only; reject traversal, absolute and device paths."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise StagingError("UPLOAD_NAME_INVALID")
    if "/" in name or "\\" in name or ".." in name:
        raise StagingError("UPLOAD_PATH_TRAVERSAL_REJECTED")
    if PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute() or ":" in name:
        raise StagingError("UPLOAD_ABSOLUTE_PATH_REJECTED")
    if _WINDOWS_DEVICE.match(name):
        raise StagingError("UPLOAD_DEVICE_PATH_REJECTED")
    if not _NAME.fullmatch(name):
        raise StagingError("UPLOAD_NAME_INVALID")
    return name


@dataclass(frozen=True)
class SourceIdentity:
    channel: str  # REMOTE_UPLOAD | BULK_EXTERNAL_TRANSFER
    actor_id: str
    original_name: str
    received_at: str


@dataclass(frozen=True)
class StagedFile:
    staged_id: str
    project_id: str
    sha256: str
    size_bytes: int
    classification: str
    state: StagingState
    scan_status: ScanStatus
    reasons: tuple[str, ...]
    source: SourceIdentity

    @property
    def ref(self) -> str:
        return f"staged:{self.staged_id}"

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["state"] = self.state.value
        data["scan_status"] = self.scan_status.value
        data["reasons"] = list(self.reasons)
        data["ref"] = self.ref
        return data


class StagingArea:
    def __init__(
        self,
        root: Path,
        *,
        scanner: Scanner | None = None,
        project_ceiling: Callable[[str], DataClassification | None],
        max_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._scanner = scanner
        self._ceiling = project_ceiling
        self._max_bytes = max_bytes

    @property
    def scanner_configured(self) -> bool:
        return self._scanner is not None

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def _record_dir(self, staged_id: str) -> Path:
        return self.root / staged_id

    def stage(
        self,
        *,
        project_id: str,
        actor_id: str,
        name: str,
        content: bytes,
        classification: str,
        channel: str = "REMOTE_UPLOAD",
    ) -> StagedFile:
        original = validate_upload_name(name)
        if len(content) > self._max_bytes:
            raise StagingError("UPLOAD_TOO_LARGE")
        level = parse_classification(classification)
        ceiling = self._ceiling(project_id)
        if ceiling is None:
            raise StagingError("PROJECT_NOT_REGISTERED")
        reasons: list[str] = []
        if not ceiling.permits(level):
            reasons.append("CLASSIFICATION_ABOVE_PROJECT_CEILING")
        if Path(original).suffix.lower() in _QUARANTINE_SUFFIXES:
            reasons.append("EXECUTABLE_CONTENT_TYPE")
        if self._scanner is None:
            scan = ScanStatus.MALWARE_SCAN_NOT_CONFIGURED
        else:
            scan = ScanStatus.CLEAN if self._scanner(content) else ScanStatus.INFECTED
            if scan is ScanStatus.INFECTED:
                reasons.append("MALWARE_SCAN_FAILED")
        staged = StagedFile(
            staged_id=f"stg-{secrets.token_hex(10)}",
            project_id=project_id,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            classification=level.value,
            state=StagingState.QUARANTINED if reasons else StagingState.STAGED,
            scan_status=scan,
            reasons=tuple(reasons),
            source=SourceIdentity(
                channel=channel,
                actor_id=actor_id,
                original_name=original,
                received_at=datetime.now(UTC).isoformat(),
            ),
        )
        directory = self._record_dir(staged.staged_id)
        directory.mkdir()
        # Stored under a fixed name: the client-chosen name is metadata only.
        (directory / "content.bin").write_bytes(content)
        (directory / "record.json").write_text(
            json.dumps(staged.to_dict(), sort_keys=True, indent=2), encoding="utf-8"
        )
        return staged

    def get(self, staged_id: str) -> dict[str, object] | None:
        if not re.fullmatch(r"stg-[0-9a-f]{20}", staged_id):
            return None
        record = self._record_dir(staged_id) / "record.json"
        if not record.is_file():
            return None
        data: dict[str, object] = json.loads(record.read_text(encoding="utf-8"))
        return data

    def verify(self, staged_id: str) -> bool:
        record = self.get(staged_id)
        if record is None:
            return False
        content = (self._record_dir(staged_id) / "content.bin").read_bytes()
        return hashlib.sha256(content).hexdigest() == record["sha256"]


class BulkInbox:
    """Registers files an external transport (e.g. SFTP) dropped into an inbox."""

    def __init__(self, inbox: Path, staging: StagingArea) -> None:
        self.inbox = Path(inbox).resolve(strict=True)
        self.staging = staging

    def register(
        self, *, project_id: str, actor_id: str, name: str, classification: str
    ) -> StagedFile:
        validate_upload_name(name)
        candidate = self.inbox / name
        try:
            info = os.lstat(candidate)
        except FileNotFoundError as exc:
            raise StagingError("INBOX_FILE_NOT_FOUND") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise StagingError("INBOX_FILE_NOT_REGULAR")
        if candidate.resolve().parent != self.inbox:
            raise StagingError("UPLOAD_PATH_TRAVERSAL_REJECTED")
        snapshot = _snapshot_checked_file(candidate, info, into=self.staging.root, limit=self.staging.max_bytes)
        try:
            # Scan and staging use the PADOS-owned snapshot, never the transport-writable inbox file.
            return self.staging.stage(
                project_id=project_id,
                actor_id=actor_id,
                name=name,
                content=snapshot.read_bytes(),
                classification=classification,
                channel="BULK_EXTERNAL_TRANSFER",
            )
        finally:
            snapshot.unlink(missing_ok=True)


_SNAPSHOT_CHUNK = 1024 * 1024


def _snapshot_checked_file(path: Path, checked: os.stat_result, *, into: Path, limit: int) -> Path:
    """Copy exactly the regular file that was checked into a PADOS-owned snapshot file.

    The inbox is writable by the external transport. The path is opened without following
    symlinks and without blocking (a FIFO swapped in cannot stall the open), the descriptor must
    be the checked regular inode, and a size/mtime change while copying refuses the file.
    """
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise StagingError("INBOX_FILE_NOT_REGULAR") from exc
    snapshot = into / f".inbox-{secrets.token_hex(10)}.part"
    try:
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (checked.st_dev, checked.st_ino):
                raise StagingError("INBOX_FILE_CHANGED")
            copied = 0
            out_fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(out_fd, "wb") as target:
                while copied <= limit:
                    chunk = os.read(source.fileno(), min(_SNAPSHOT_CHUNK, limit + 1 - copied))
                    if not chunk:
                        break
                    target.write(chunk)
                    copied += len(chunk)
            after = os.fstat(source.fileno())
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise StagingError("INBOX_FILE_CHANGED")
    except BaseException:
        snapshot.unlink(missing_ok=True)
        raise
    return snapshot
