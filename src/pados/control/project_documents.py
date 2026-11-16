# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded Markdown documents inside a project folder, for the Control Center ("add a Markdown file").

The only files this module writes are ``.md`` files strictly inside the root of a project whose root lies inside the
approved workspace root (``PADOS_PRODUCT_WORKSPACE_ROOT``): never a project registered elsewhere by the CLI, never a
path outside the root, never through a symbolic link or junction. A name is 1-3 safe segments ending in ``.md``; the
content is UTF-8, at most 256 KiB, without NUL and without secret-like text. An existing file is replaced only when
the caller says ``replace`` and names the SHA-256 it read (optimistic concurrency).

RECOVERABLE STAGED COMMIT. A file write and a PostgreSQL transaction cannot share one atomic commit, so the control
route never replaces a document before its audit event is durable. It uses the write-ahead journal below:

1. **Intent.** ``stage_document`` writes ``<project>/.pados/journal/<intent_id>.json`` (atomic, fsynced) naming the
   idempotency key, path, new and previous SHA-256, staged path and time. The journal folder is created owner-only
   (``pados.identity.fs_security``); the listing skips dot folders and a document name can never start with a dot,
   so the journal is never listed, read or written through the document routes.
2. **Stage.** The content goes to ``.<name>.<intent_id>.staged`` next to the target (same folder, fsynced).
3. **Commit.** The route appends ``PROJECT_DOCUMENT_WRITTEN`` (with ``intent_id``) and commits. That commit is the
   single decision point: an intent whose event exists is rolled forward, one without it is rolled back.
4. **Finalize.** ``apply_entry`` replaces the target from the staged file (``os.replace``), then removes the intent.

Reconciliation (``product_routes.reconcile_journal``; at server start, before every document write and listing, and
before a read when an intent is open) classifies each open intent from the database alone, under the project locks:

=====================================  ===========================================  ===============================
Crash point                            State left                                   Reconciliation
=====================================  ===========================================  ===============================
before or during the intent record     nothing, or a ``.tmp`` in the journal       sweep the ``.tmp``
after the intent, before/while staged  intent (+ partial staged), no event          roll back: staged, then intent
staged, before or during the commit    intent + staged, event absent (rolled back)  roll back
commit landed, before the replace      intent + staged + event, old target          roll forward (``os.replace``)
  ... staged missing / corrupt         intent + event, no valid staged file         ``PROJECT_DOCUMENT_WRITE_FAILED``
  ... target edited meanwhile          intent + event, target != previous digest    ``..._WRITE_FAILED`` (no clobber)
after the replace, before closing      intent + event, target == new digest         close the intent
after a FAILED commit, before closing  intent + event + FAILED event                close (no second FAILED)
no intent, disk != audited digest      an edit outside PADOS                        report ``DOCUMENT_UNAUDITED_CHANGE``
=====================================  ===========================================  ===============================
``write_document`` is the unjournaled primitive (preconditions plus an atomic replace) kept for callers that audit
nothing; the control route does not use it.

Reading lists ``.md`` files (bounded walk, links never followed) and returns one file's text; reads work for any
registered project root, writes only inside the workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pados.control.product_errors import ProductControlError
from pados.identity.fs_security import StoreInsecure, make_private_directory
from pados.memory.security import contains_secret

DOCUMENT_MAX_BYTES = 256 * 1024
LIST_MAX_ENTRIES = 200
LIST_MAX_DEPTH = 4
HASH_MAX_BYTES = 1024 * 1024
MAX_SEGMENTS = 3
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INTENT = re.compile(r"^[0-9a-f]{32}$")
JOURNAL_PARTS = (".pados", "journal")
JOURNAL_MAX_ENTRIES = 64
JOURNAL_VERSION = 1
STAGED_SUFFIX = ".staged"
APPLIED = "APPLIED"
ALREADY_APPLIED = "ALREADY_APPLIED"
UNREADABLE = "UNREADABLE"  # a digest that never equals a SHA-256: present, but not a regular file within the bound


@dataclass(frozen=True)
class ProjectRoot:
    path: Path
    writable: bool


def validate_document_path(raw: str) -> PurePosixPath:
    """A relative name of 1-3 safe segments ending in ``.md`` (``notes.md``, ``docs/guide.md``)."""
    value = str(raw or "").strip()
    if not value or len(value) > 200 or "\\" in value or "\x00" in value or value.startswith("/"):
        raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
    parts = value.split("/")
    if len(parts) > MAX_SEGMENTS:
        raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
    for part in parts:
        stem = part.split(".", 1)[0].lower()
        if not _SEGMENT.fullmatch(part) or ".." in part or part.endswith(".") or stem in _WINDOWS_RESERVED:
            raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
    if not parts[-1].endswith(".md") or len(parts[-1]) <= 3:
        raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
    return PurePosixPath(*parts)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def resolve_project_root(root_path: str, workspace_root: str) -> ProjectRoot:
    """The project's registered root; writable only when it lies strictly inside the approved workspace root."""
    try:
        root = Path(root_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductControlError("PROJECT_ROOT_UNAVAILABLE", 409) from exc
    if not root.is_dir():
        raise ProductControlError("PROJECT_ROOT_UNAVAILABLE", 409)
    writable = False
    if workspace_root and Path(workspace_root).expanduser().is_absolute():
        workspace = Path(workspace_root).expanduser().resolve()
        writable = workspace in root.parents
    return ProjectRoot(path=root, writable=writable)


def _target(root: Path, relative: PurePosixPath) -> Path:
    """The file under ``root``; no existing component may be a link, and the result stays strictly inside."""
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            raise ProductControlError("DOCUMENT_PATH_OUTSIDE_PROJECT", 403)
    resolved = current.resolve()
    if root not in resolved.parents:
        raise ProductControlError("DOCUMENT_PATH_OUTSIDE_PROJECT", 403)
    return resolved


def encode_content(content: str) -> bytes:
    """UTF-8 bytes of a document, refused when too large, not encodable, containing NUL or secret-like text."""
    if "\x00" in content:
        raise ProductControlError("DOCUMENT_CONTENT_INVALID", 422)
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:  # a lone surrogate from JSON
        raise ProductControlError("DOCUMENT_ENCODING_INVALID", 422) from exc
    if len(data) > DOCUMENT_MAX_BYTES:
        raise ProductControlError("DOCUMENT_TOO_LARGE", 413)
    if contains_secret(content):
        raise ProductControlError("DOCUMENT_CONTENT_REJECTED", 422)
    return data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(target: Path, data: bytes) -> None:
    """Write through a new temporary file in the same folder, then replace: readers see old or new, never partial."""
    temporary = target.parent / f".{target.name}.{secrets.token_hex(6)}.tmp"
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_regular(path: Path, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes of a regular, unlinked file; ``None`` when it is not one or is larger."""
    if _is_link(path):
        return None
    try:
        status = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
        return None
    data = path.read_bytes()
    return None if len(data) > limit else data


@dataclass(frozen=True)
class PreparedWrite:
    target: Path
    previous_sha256: str | None


def prepare_write(root: ProjectRoot, relative: PurePosixPath, *, replace: bool,
                  expected_sha256: str | None) -> PreparedWrite:
    """Every precondition of a create or (with ``replace`` and the current SHA-256) a replace; makes the folder."""
    if not root.writable:
        raise ProductControlError("PROJECT_ROOT_OUTSIDE_WORKSPACE", 403)
    target = _target(root.path, relative)
    previous: str | None = None
    if target.exists():
        if not target.is_file():
            raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
        if not replace:
            raise ProductControlError("DOCUMENT_EXISTS", 409)
        current = _read_regular(target, HASH_MAX_BYTES)
        if current is None:
            raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
        previous = sha256_hex(current)
        if not expected_sha256 or expected_sha256.lower() != previous:
            raise ProductControlError("DOCUMENT_CHANGED", 409)
    elif replace:
        raise ProductControlError("DOCUMENT_NOT_FOUND", 404)
    parent = target.parent
    if parent != root.path:
        parent.mkdir(parents=True, exist_ok=True)
        _target(root.path, relative)  # re-check: a folder created meanwhile must not be a link
    return PreparedWrite(target=target, previous_sha256=previous)


def document_receipt(path: str, size: int, sha256: str, previous_sha256: str | None) -> dict[str, Any]:
    return {"path": path, "size": size, "sha256": sha256, "replaced": previous_sha256 is not None,
            "previous_sha256": previous_sha256}


def write_document(root: ProjectRoot, relative: PurePosixPath, data: bytes, *, replace: bool,
                   expected_sha256: str | None) -> dict[str, Any]:
    """Create or replace one Markdown file atomically, unjournaled and unaudited; returns its receipt."""
    prepared = prepare_write(root, relative, replace=replace, expected_sha256=expected_sha256)
    atomic_write(prepared.target, data)
    return document_receipt(relative.as_posix(), len(data), sha256_hex(data), prepared.previous_sha256)


def read_document(root: ProjectRoot, relative: PurePosixPath) -> dict[str, Any]:
    target = _target(root.path, relative)
    if not target.exists():
        raise ProductControlError("DOCUMENT_NOT_FOUND", 404)
    data = _read_regular(target, DOCUMENT_MAX_BYTES)
    if data is None:
        raise ProductControlError("DOCUMENT_TOO_LARGE", 413)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProductControlError("DOCUMENT_ENCODING_INVALID", 422) from exc
    return {"path": relative.as_posix(), "size": len(data), "sha256": sha256_hex(data), "content": content}


def list_documents(root: ProjectRoot) -> dict[str, Any]:
    """The project's ``.md`` files (bounded, sorted, links never followed) with size, SHA-256 and change time."""
    found: list[dict[str, Any]] = []
    truncated = False
    for directory, subdirs, files in os.walk(root.path):
        base = Path(directory)
        depth = len(base.relative_to(root.path).parts)
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS and not d.startswith(".")
                            and depth + 1 < LIST_MAX_DEPTH and not _is_link(base / d))
        for name in sorted(files):
            if not name.lower().endswith(".md") or name.startswith("."):
                continue
            if len(found) >= LIST_MAX_ENTRIES:
                truncated = True
                break
            path = base / name
            data = _read_regular(path, HASH_MAX_BYTES)
            try:
                status = path.stat()
            except OSError:
                continue
            found.append({"path": path.relative_to(root.path).as_posix(), "size": status.st_size,
                          "sha256": sha256_hex(data) if data is not None else None,
                          "modified_at": datetime.fromtimestamp(status.st_mtime, UTC).isoformat()})
        if truncated:
            break
    return {"documents": found, "truncated": truncated, "writable": root.writable}


def expected_sha(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not _SHA256.fullmatch(value.lower()):
        raise ProductControlError("DOCUMENT_SHA256_INVALID", 422)
    return value.lower()


# --- write-ahead journal (recoverable staged commit) -----------------------------------------------------------------
@dataclass(frozen=True)
class JournalEntry:
    """One write intent: durable before its staged file exists, removed once the write is settled."""

    intent_id: str
    idempotency_key: str
    path: str
    sha256: str
    previous_sha256: str | None
    size: int
    replace: bool
    staged: str  # the staged file, relative to the project root; always derived, never trusted from the file
    created_at: str

    def to_json(self) -> bytes:
        return json.dumps({"version": JOURNAL_VERSION, **asdict(self)}, sort_keys=True).encode("utf-8")

    @classmethod
    def from_json(cls, raw: Any) -> JournalEntry:
        """Parse one entry; ``ValueError`` for anything malformed, including a staged path that is not derived."""
        if not isinstance(raw, dict) or raw.get("version") != JOURNAL_VERSION:
            raise ValueError("journal entry version")
        intent_id, path = str(raw.get("intent_id", "")), str(raw.get("path", ""))
        previous = raw.get("previous_sha256")
        valid = (_INTENT.fullmatch(intent_id) and _SHA256.fullmatch(str(raw.get("sha256", "")))
                 and (previous is None or _SHA256.fullmatch(str(previous)))
                 and isinstance(raw.get("size"), int) and isinstance(raw.get("replace"), bool))
        if not valid:
            raise ValueError("journal entry fields")
        try:
            relative = validate_document_path(path)
        except ProductControlError as exc:
            raise ValueError("journal entry path") from exc
        if raw.get("staged") != staged_relative(relative, intent_id).as_posix():
            raise ValueError("journal entry staged path")
        return cls(intent_id=intent_id, idempotency_key=str(raw.get("idempotency_key", "")), path=path,
                   sha256=str(raw["sha256"]), previous_sha256=previous, size=int(raw["size"]),
                   replace=bool(raw["replace"]), staged=str(raw["staged"]),
                   created_at=str(raw.get("created_at", "")))


def staged_relative(relative: PurePosixPath, intent_id: str) -> PurePosixPath:
    """``docs/.guide.md.<intent_id>.staged`` for ``docs/guide.md``: same folder, hidden, never a ``.md`` name."""
    return relative.parent / f".{relative.name}.{intent_id}{STAGED_SUFFIX}"


def _journal_dir(root: Path) -> Path:
    return root.joinpath(*JOURNAL_PARTS)


def _journal_linked(journal: Path) -> bool:
    return _is_link(journal.parent) or _is_link(journal)


def _ensure_journal_dir(root: Path) -> Path:
    """The project's journal folder, created owner-only from its first moment; never through a link."""
    journal = _journal_dir(root)
    if _journal_linked(journal):
        raise ProductControlError("DOCUMENT_JOURNAL_UNAVAILABLE", 503)
    try:
        make_private_directory(journal)
    except (OSError, StoreInsecure) as exc:
        raise ProductControlError("DOCUMENT_JOURNAL_UNAVAILABLE", 503) from exc
    if _journal_linked(journal) or not journal.is_dir():
        raise ProductControlError("DOCUMENT_JOURNAL_UNAVAILABLE", 503)
    return journal


def _write_staged(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_final(staged: Path, target: Path) -> None:
    os.replace(staged, target)


def _close_entry(root: Path, intent_id: str) -> None:
    (_journal_dir(root) / f"{intent_id}.json").unlink(missing_ok=True)


def stage_document(root: ProjectRoot, relative: PurePosixPath, data: bytes, *, replace: bool,
                   expected_sha256: str | None, idempotency_key: str) -> JournalEntry:
    """Check the write, record its intent durably, then stage the content. The target is not touched."""
    prepared = prepare_write(root, relative, replace=replace, expected_sha256=expected_sha256)
    journal = _ensure_journal_dir(root.path)
    intent_id = secrets.token_hex(16)
    entry = JournalEntry(intent_id=intent_id, idempotency_key=idempotency_key, path=relative.as_posix(),
                         sha256=sha256_hex(data), previous_sha256=prepared.previous_sha256, size=len(data),
                         replace=replace, staged=staged_relative(relative, intent_id).as_posix(),
                         created_at=datetime.now(UTC).isoformat())
    try:
        atomic_write(journal / f"{intent_id}.json", entry.to_json())
    except OSError as exc:
        raise ProductControlError("DOCUMENT_JOURNAL_UNAVAILABLE", 503) from exc
    try:
        _write_staged(root.path / entry.staged, data)
    except OSError as exc:
        discard_entry(root.path, entry)
        raise ProductControlError("DOCUMENT_STAGE_FAILED", 503) from exc
    return entry


def discard_entry(root: Path, entry: JournalEntry) -> None:
    """Roll back: the staged file first, then the intent (a crash between leaves an intent with nothing staged)."""
    (root / entry.staged).unlink(missing_ok=True)
    _close_entry(root, entry.intent_id)


def file_digest(path: Path) -> str | None:
    """``None`` when absent, ``UNREADABLE`` when present but not a regular unlinked file within the hash bound."""
    if not _is_link(path) and not path.exists():
        return None
    data = _read_regular(path, HASH_MAX_BYTES)
    return sha256_hex(data) if data is not None else UNREADABLE


def document_digest(root: Path, path: str) -> str | None:
    """The digest of an audited document path (see ``file_digest``); a path that cannot be checked is unreadable."""
    try:
        return file_digest(_target(root, validate_document_path(path)))
    except (ProductControlError, OSError):
        return UNREADABLE


def apply_entry(root: Path, entry: JournalEntry) -> str:
    """Roll an audited intent forward: ``APPLIED``/``ALREADY_APPLIED``, or a closed failure code.

    Never overwrites a target that no longer holds the intent's previous content (``DOCUMENT_TARGET_CHANGED``), and
    never installs a staged file whose digest is not the audited one. An ``OSError`` of the replace itself (a file
    held open by an editor) propagates: the intent stays open and the next reconciliation retries it.
    """
    try:
        target = _target(root, validate_document_path(entry.path))
    except ProductControlError:
        return "DOCUMENT_TARGET_CHANGED"
    staged = root / entry.staged
    current = file_digest(target)
    if current == entry.sha256:
        staged.unlink(missing_ok=True)
        _close_entry(root, entry.intent_id)
        return ALREADY_APPLIED
    staged_digest = file_digest(staged)
    if staged_digest is None:
        return "DOCUMENT_STAGED_MISSING"
    if staged_digest != entry.sha256:
        return "DOCUMENT_STAGED_CORRUPT"
    if current != entry.previous_sha256:
        return "DOCUMENT_TARGET_CHANGED"
    _replace_final(staged, target)
    _close_entry(root, entry.intent_id)
    return APPLIED


def open_entries(root: ProjectRoot) -> tuple[list[JournalEntry], int]:
    """The open intents of a writable project (bounded, by name) and how many entries are unreadable."""
    journal = _journal_dir(root.path)
    if not root.writable or _journal_linked(journal) or not journal.is_dir():
        return [], 0
    entries: list[JournalEntry] = []
    unreadable = 0
    for item in sorted(journal.glob("*.json"))[:JOURNAL_MAX_ENTRIES]:
        try:
            entry = JournalEntry.from_json(json.loads(item.read_bytes()))
        except (OSError, ValueError, TypeError, KeyError):
            unreadable += 1
            continue
        if entry.intent_id != item.stem:
            unreadable += 1
            continue
        entries.append(entry)
    return entries, unreadable


def has_open_entries(root: ProjectRoot) -> bool:
    """A cheap check (one folder listing) before a read route takes the project locks to reconcile."""
    journal = _journal_dir(root.path)
    return (root.writable and not _journal_linked(journal) and journal.is_dir()
            and any(journal.glob("*.json")))


def sweep_interrupted_intents(root: ProjectRoot) -> int:
    """Remove temporary files of intent records that never became durable (nothing was staged for them)."""
    journal = _journal_dir(root.path)
    if not root.writable or _journal_linked(journal) or not journal.is_dir():
        return 0
    removed = 0
    for item in journal.glob(".*.tmp"):
        item.unlink(missing_ok=True)
        removed += 1
    return removed
