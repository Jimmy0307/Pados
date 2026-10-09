# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, operator-controlled P6.15 corpus runtime.

This module deliberately keeps P6.15 separate from the stable CLI. Every
operation validates the authorization and database boundary before it can
read or write corpus data. It has no watcher, scheduler, cloud model, OCR, or
semantic retrieval path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from pados.config import Settings
from pados.corpus.configuration import (
    CorpusConfiguration,
    CorpusConfigurationError,
    load_configuration,
    repository_root,
)
from pados.corpus.persistence import persist_pilot_result
from pados.corpus.pipeline import run_fixture_pilot
from pados.corpus.project_context import (
    ProjectContextProjectionBlocked,
    ensure_project_context_projection,
    inspect_project_context_projection,
)
from pados.corpus.security import CorpusBoundaryError, resolve_approved_root, resolve_child_path
from pados.db import session_scope

PROJECT_ROOT = repository_root()
EXPECTED_WORKTREE = PROJECT_ROOT
EXPECTED_PYTHON = Path(sys.executable).resolve()
APPROVED_ROOT: Path | None = None
AUTHORIZATION_FILE: Path | None = None
_DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "runtime" / "artifacts" / "p6_15_corpus_runtime"
RUNTIME_ARTIFACT_ROOT = _DEFAULT_ARTIFACT_ROOT
SCAN_MANIFEST = RUNTIME_ARTIFACT_ROOT / "scan_manifest.json"
DEFAULT_BATCH_AUTHORIZATION = RUNTIME_ARTIFACT_ROOT / "authorizations" / "batch.json"
PROJECT_ID: str | None = None
ISOLATED_DATABASE = ""
OPERATIONAL_DATABASE = ""
ISOLATED_REVISION = "20260730_0011"
OPERATIONAL_REVISION = "20260711_0009"
EXCLUDED_REVISION = "20260730_0012"
MAX_DOCUMENTS = 100
POLICY = "KEYWORD_ONLY"
_ACTIVE_CONFIGURATION: CorpusConfiguration | None = None
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
REQUIRED_CORPUS_TABLES = {
    "projects",
    "corpus_sources",
    "corpus_ingestion_batches",
    "corpus_documents",
    "corpus_document_versions",
    "corpus_pages",
    "corpus_chunks",
    "corpus_evidence_spans",
    "corpus_document_relationships",
    "corpus_ingestion_events",
}


def _configuration() -> CorpusConfiguration:
    artifact_override = RUNTIME_ARTIFACT_ROOT if RUNTIME_ARTIFACT_ROOT != _DEFAULT_ARTIFACT_ROOT else None
    try:
        return load_configuration(
            authorization_file=AUTHORIZATION_FILE,
            database_name=ISOLATED_DATABASE or None,
            runtime_artifact_root=artifact_override,
        )
    except CorpusConfigurationError as exc:
        raise CorpusRuntimeBlocked("CONFIGURATION_INVALID", "doctor", str(exc)) from exc


def _apply_configuration() -> CorpusConfiguration:
    global _ACTIVE_CONFIGURATION, APPROVED_ROOT, AUTHORIZATION_FILE, DEFAULT_BATCH_AUTHORIZATION
    global ISOLATED_DATABASE, MAX_DOCUMENTS, OPERATIONAL_DATABASE, POLICY, PROJECT_ID
    global RUNTIME_ARTIFACT_ROOT, SCAN_MANIFEST
    config = _configuration()
    _ACTIVE_CONFIGURATION = config
    APPROVED_ROOT = config.approved_corpus_root
    AUTHORIZATION_FILE = config.authorization_file
    ISOLATED_DATABASE = config.database_name
    OPERATIONAL_DATABASE = config.operational_database
    PROJECT_ID = config.project_id
    MAX_DOCUMENTS = config.maximum_documents
    POLICY = config.embedding_policy
    RUNTIME_ARTIFACT_ROOT = config.runtime_artifact_root
    SCAN_MANIFEST = RUNTIME_ARTIFACT_ROOT / "scan_manifest.json"
    DEFAULT_BATCH_AUTHORIZATION = RUNTIME_ARTIFACT_ROOT / "authorizations" / "batch.json"
    return config


class CorpusRuntimeBlocked(RuntimeError):
    """A sanitized, fail-closed runtime refusal."""

    def __init__(self, reason: str, stage: str, detail: str, *, write_occurred: bool = False) -> None:
        self.reason = re.sub(r"[^A-Z0-9_]+", "_", reason.upper()).strip("_") or "UNKNOWN"
        self.stage = stage
        self.detail = _sanitize(detail)
        self.write_occurred = write_occurred
        super().__init__(f"{self.reason}: {self.detail}")


def _approved_root() -> Path:
    """Return the configured corpus root; an unconfigured runtime fails closed."""
    if APPROVED_ROOT is None:
        raise CorpusRuntimeBlocked("RUNTIME_NOT_CONFIGURED", "configuration", "approved corpus root is not configured")
    return APPROVED_ROOT


def _project_id() -> str:
    """Return the configured corpus project; an unconfigured runtime fails closed."""
    if PROJECT_ID is None:
        raise CorpusRuntimeBlocked("RUNTIME_NOT_CONFIGURED", "configuration", "corpus project is not configured")
    return PROJECT_ID


def _count(connection: Any, query: str, project_id: str) -> int:
    row = connection.execute(query, (project_id,)).fetchone()
    if row is None:
        raise CorpusRuntimeBlocked("COUNT_UNAVAILABLE", "database", "row count query returned no row")
    return int(row[0])


def _sanitize(value: object) -> str:
    text = str(value)
    text = re.sub(r"(?i)(password|passwd|api[_-]?key|token|secret|dsn)\s*[=:]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)postgresql(?:\+[a-z0-9_]+)?://[^\s]+", "postgresql://[REDACTED]", text)
    return text[:600]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise CorpusRuntimeBlocked("AUTHORIZATION_UTF8_BOM", "authorization", "UTF-8 BOM is rejected")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusRuntimeBlocked("MALFORMED_JSON", "authorization", _sanitize(exc)) from exc
    if not isinstance(value, dict):
        raise CorpusRuntimeBlocked("MALFORMED_JSON", "authorization", "JSON object is required")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise CorpusRuntimeBlocked("GIT_PROVENANCE_UNAVAILABLE", "doctor", _sanitize(completed.stderr or completed.stdout))
    return completed.stdout.strip()


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
    except (AttributeError, OSError):
        attributes = 0
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)


def _validate_tree(root: Path) -> None:
    """Reject every link/reparse point in the approved tree before scanning."""
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if _is_reparse_or_symlink(current_path) and current_path != root:
            raise CorpusRuntimeBlocked("SYMLINK_REPARSE_ESCAPE", "scan", str(current_path))
        unsafe_dirs = [name for name in directories if _is_reparse_or_symlink(current_path / name)]
        unsafe_files = [name for name in files if _is_reparse_or_symlink(current_path / name)]
        if unsafe_dirs or unsafe_files:
            raise CorpusRuntimeBlocked(
                "SYMLINK_REPARSE_ESCAPE",
                "scan",
                f"unsafe entries: {unsafe_dirs + unsafe_files}",
            )


def _load_authorization() -> dict[str, Any]:
    config = _ACTIVE_CONFIGURATION or _apply_configuration()
    return config.authorization


def _settings(database: str) -> Settings:
    config = _ACTIVE_CONFIGURATION or _configuration()
    if database not in {config.database_name, config.operational_database}:
        raise CorpusRuntimeBlocked("DATABASE_TARGET_REJECTED", "doctor", database)
    base = Settings()
    return base.model_copy(
        update={
            "db_host": config.database_host,
            "db_port": config.database_port,
            "db_name": database,
            "db_user": config.database_user,
            "db_password": config.database_password,
        }
    )


def _connect(database: str) -> psycopg.Connection[Any]:
    settings = _settings(database)
    try:
        return psycopg.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            connect_timeout=3,
        )
    except psycopg.Error as exc:
        raise CorpusRuntimeBlocked("DATABASE_CONNECTION_FAILED", "doctor", _sanitize(exc)) from exc


def _database_check(database: str, expected_revision: str, *, stage: str = "doctor") -> dict[str, Any]:
    with _connect(database) as connection:
        identity = connection.execute(
            """
            SELECT current_database(), current_user,
                   pg_get_userbyid(datdba)
            FROM pg_database
            WHERE datname = current_database()
            """
        ).fetchone()
        revisions = [str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()]
        tables = {str(row[0]) for row in connection.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()}
    if identity is None or str(identity[0]) != database:
        raise CorpusRuntimeBlocked("DATABASE_IDENTITY_MISMATCH", stage, database)
    if str(identity[1]) != str(identity[2]):
        raise CorpusRuntimeBlocked("DATABASE_OWNER_MISMATCH", stage, f"current_user={identity[1]}; owner={identity[2]}")
    if revisions != [expected_revision]:
        raise CorpusRuntimeBlocked("DATABASE_REVISION_MISMATCH", stage, f"expected={expected_revision}; observed={revisions}")
    if EXCLUDED_REVISION in revisions:
        raise CorpusRuntimeBlocked("EXCLUDED_MIGRATION_PRESENT", stage, EXCLUDED_REVISION)
    missing_tables = sorted(REQUIRED_CORPUS_TABLES - tables) if database == ISOLATED_DATABASE else []
    if missing_tables:
        raise CorpusRuntimeBlocked("CORPUS_SCHEMA_INCOMPLETE", stage, ",".join(missing_tables))
    return {
        "database": database,
        "current_user": str(identity[1]),
        "owner": str(identity[2]),
        "revisions": revisions,
        "revision": expected_revision,
        "required_tables_present": not missing_tables,
        "tables": sorted(tables) if database == ISOLATED_DATABASE else [],
    }


def _provenance(auth: dict[str, Any], isolated: dict[str, Any], operational: dict[str, Any]) -> dict[str, Any]:
    config = _ACTIVE_CONFIGURATION or _apply_configuration()
    return {
        "repository": str(PROJECT_ROOT),
        "branch": _git("branch", "--show-current"),
        "commit": _git("rev-parse", "HEAD"),
        "worktree": str(PROJECT_ROOT),
        "python_executable": str(Path(sys.executable).resolve()),
        "database_host": config.database_host,
        "database_port": config.database_port,
        "isolated_database": config.database_name,
        "isolated_revision": isolated["revision"],
        "operational_database": config.operational_database,
        "operational_revision": operational["revision"],
        "approved_corpus_root": str(config.approved_corpus_root),
        "authorization_file": str(config.authorization_file),
        "project_id": auth["project_id"],
        "embedding_policy": config.embedding_policy,
        "ocr_status": "DISABLED",
        "cloud_model_status": "DISABLED",
        "source_modification": "DISABLED",
        "operational_migration": "DISABLED",
        "runtime_artifact_root": str(config.runtime_artifact_root),
    }


def doctor() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        config = _apply_configuration()
        actual_worktree = PROJECT_ROOT.resolve()
        checks.append({"name": "worktree_identity", "status": "PASS" if actual_worktree == EXPECTED_WORKTREE else "FAIL", "detail": str(actual_worktree)})
        if actual_worktree != EXPECTED_WORKTREE:
            raise CorpusRuntimeBlocked("WORKTREE_IDENTITY_MISMATCH", "doctor", str(actual_worktree))
        branch = _git("branch", "--show-current")
        commit = _git("rev-parse", "HEAD")
        checks.extend([
            {"name": "branch", "status": "PASS", "detail": branch},
            {"name": "commit", "status": "PASS", "detail": commit},
        ])
        python_ok = Path(sys.executable).resolve() == config.python_executable
        checks.append({"name": "python_executable", "status": "PASS" if python_ok else "FAIL", "detail": str(Path(sys.executable).resolve())})
        if not python_ok:
            raise CorpusRuntimeBlocked("PYTHON_EXECUTABLE_MISMATCH", "doctor", str(Path(sys.executable).resolve()))
        auth = config.authorization
        checks.append({"name": "authorization_policy", "status": "PASS", "detail": "AUTHORIZED; KEYWORD_ONLY; OCR/cloud/source/operational migration disabled"})
        _validate_tree(config.approved_corpus_root)
        checks.append({"name": "approved_root_boundary", "status": "PASS", "detail": str(config.approved_corpus_root)})
        isolated = _database_check(config.database_name, config.isolated_revision)
        checks.append({"name": "isolated_database_identity_and_revision", "status": "PASS", "detail": isolated})
        operational = _database_check(config.operational_database, config.operational_revision)
        checks.append({"name": "operational_database_read_only_revision", "status": "PASS", "detail": operational})
        projection = inspect_project_context_projection(
            project_id=config.project_id,
            operational_database=config.operational_database,
            corpus_database=config.database_name,
            operational_revision=config.operational_revision,
            connect=_connect,
        )
        checks.append({"name": "project_context_projection", "status": projection["status"], "detail": projection})
        return {
            "status": "PASS",
            "action": "doctor",
            "project_context_projection_status": projection["status"],
            "checks": checks,
            "project_context_projection": projection,
            "provenance": _provenance(auth, isolated, operational),
        }
    except CorpusRuntimeBlocked:
        raise
    except ProjectContextProjectionBlocked as exc:
        raise CorpusRuntimeBlocked(exc.reason, "doctor", exc.detail) from exc
    except (OSError, ValueError, psycopg.Error) as exc:
        raise CorpusRuntimeBlocked(type(exc).__name__.upper(), "doctor", _sanitize(exc)) from exc


def _doctor_or_raise() -> dict[str, Any]:
    return doctor()


def _known_records(connection: psycopg.Connection[Any], project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT d.id::text, d.status, v.content_hash, v.source_path
        FROM corpus_documents d
        JOIN corpus_document_versions v ON v.document_id = d.id
        WHERE d.project_id = %s
        ORDER BY v.created_at DESC
        """,
        (project_id,),
    ).fetchall()
    return [{"document_id": str(row[0]), "status": str(row[1]), "content_hash": str(row[2]), "source_path": str(row[3])} for row in rows]


def _scan_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, _, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in sorted(names, key=str.casefold):
            path = current_path / name
            if path.suffix.casefold() == ".pdf":
                try:
                    files.append(resolve_child_path(root, path.relative_to(root)))
                except CorpusBoundaryError as exc:
                    raise CorpusRuntimeBlocked("APPROVED_ROOT_BOUNDARY_VIOLATION", "scan", str(exc)) from exc
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())


def scan(*, output: Path | None = None) -> dict[str, Any]:
    doctor_result = _doctor_or_raise()
    output = _resolve_output(str(output) if output is not None else None, SCAN_MANIFEST)
    auth = _load_authorization()
    root = resolve_approved_root(auth["approved_corpus_root"])
    _validate_tree(root)
    files = _scan_files(root)
    if len(files) > MAX_DOCUMENTS:
        raise CorpusRuntimeBlocked("DOCUMENT_CEILING_EXCEEDED", "scan", f"discovered={len(files)}; ceiling={MAX_DOCUMENTS}")
    with _connect(ISOLATED_DATABASE) as connection:
        known = _known_records(connection, _project_id())
    by_path: dict[str, list[dict[str, Any]]] = {}
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for record in known:
        by_path.setdefault(record["source_path"], []).append(record)
        by_hash.setdefault(record["content_hash"], []).append(record)
    hash_groups: dict[str, list[str]] = {}
    for path in files:
        hash_groups.setdefault(_sha256(path), []).append(path.relative_to(root).as_posix())
    documents: list[dict[str, Any]] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        content_hash = _sha256(path)
        try:
            with path.open("rb") as handle:
                handle.read(1)
            readability = "READABLE"
        except (OSError, PermissionError) as exc:
            readability = "UNREADABLE"
            raise CorpusRuntimeBlocked("FILE_UNREADABLE", "scan", f"{relative}: {type(exc).__name__}") from exc
        path_known = by_path.get(relative, [])
        hash_known = by_hash.get(content_hash, [])
        if path_known and any(item["content_hash"] == content_hash for item in path_known):
            change_status = "UNCHANGED"
        elif path_known:
            change_status = "CHANGED"
        else:
            change_status = "NEW"
        duplicate_group = None
        if len(hash_groups[content_hash]) > 1:
            duplicate_group = "EXACT_DUPLICATE:" + content_hash
        elif hash_known:
            duplicate_group = "EXISTING_CONTENT_HASH:" + content_hash
        documents.append(
            {
                "canonical_absolute_path": str(path),
                "relative_path": relative,
                "file_size": path.stat().st_size,
                "modified_timestamp": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                "sha256": content_hash,
                "readability": readability,
                "duplicate_group": duplicate_group,
                "known_status": "KNOWN" if path_known or hash_known else "UNKNOWN",
                "change_status": change_status,
                "existing_document_ids": sorted({item["document_id"] for item in path_known + hash_known}),
            }
        )
    result = {
        "manifest_type": "P6.15_CONTROLLED_RUNTIME_SCAN",
        "status": "PASS",
        "generated_at": _now(),
        "project_id": PROJECT_ID,
        "approved_root": str(root),
        "isolated_database": ISOLATED_DATABASE,
        "maximum_documents": MAX_DOCUMENTS,
        "parse_performed": False,
        "documents": documents,
        "document_count": len(documents),
        "provenance": doctor_result["provenance"],
    }
    _write_json(output, result)
    return result


def _resolve_output(path: str | None, default: Path) -> Path:
    candidate = Path(path) if path else default
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def review(*, scan_manifest: Path | None = None, selected: Iterable[str] = ()) -> dict[str, Any]:
    doctor_result = _doctor_or_raise()
    scan_manifest = _resolve_output(str(scan_manifest) if scan_manifest is not None else None, SCAN_MANIFEST)
    auth = _load_authorization()
    scan_data = _read_json(scan_manifest)
    if scan_data.get("status") != "PASS" or scan_data.get("approved_root") != str(APPROVED_ROOT):
        raise CorpusRuntimeBlocked("SCAN_MANIFEST_INVALID", "review", str(scan_manifest))
    by_relative = {str(item["relative_path"]): item for item in scan_data.get("documents", [])}
    requested = list(selected) or sorted(by_relative)
    if not requested:
        raise CorpusRuntimeBlocked("EMPTY_BATCH", "review", "no PDF selected")
    if len(requested) > int(auth["maximum_documents"]):
        raise CorpusRuntimeBlocked("DOCUMENT_CEILING_EXCEEDED", "review", f"selected={len(requested)}")
    selected_files: list[dict[str, Any]] = []
    for relative in requested:
        if relative not in by_relative:
            raise CorpusRuntimeBlocked("FILE_NOT_IN_SCAN_MANIFEST", "review", relative)
        selected_files.append(dict(by_relative[relative]))
    batch_id = str(UUID(bytes=hashlib.sha256((_project_id() + "|" + "|".join(item["relative_path"] for item in selected_files) + "|" + "|".join(item["sha256"] for item in selected_files)).encode()).digest()[:16]))
    manifest = {
        "manifest_type": "P6.15_CONTROLLED_RUNTIME_BATCH",
        "status": "PENDING_HUMAN_AUTHORIZATION",
        "generated_at": _now(),
        "batch_id": batch_id,
        "project_id": PROJECT_ID,
        "approved_root": str(APPROVED_ROOT),
        "selected_files": selected_files,
        "selected_count": len(selected_files),
        "total_bytes": sum(int(item["file_size"]) for item in selected_files),
        "parser_policy": "TEXT_FIRST_PYPDF",
        "ocr_policy": "DISABLED",
        "embedding_policy": POLICY,
        "cloud_model_policy": "DISABLED",
        "database_target": ISOLATED_DATABASE,
        "operational_database_prohibition": OPERATIONAL_DATABASE,
        "operational_migration_prohibition": EXCLUDED_REVISION,
        "source_modification_prohibition": True,
        "provenance": doctor_result["provenance"],
    }
    path = RUNTIME_ARTIFACT_ROOT / "batches" / f"{batch_id}.json"
    _write_json(path, manifest)
    return {"status": "PASS", "action": "review", "manifest_path": str(path), "manifest": manifest}


def authorize_batch(*, manifest_path: Path, output: Path | None = None, authorizer: str, confirm: bool, expires_hours: int = 24) -> dict[str, Any]:
    doctor_result = _doctor_or_raise()
    output = _resolve_output(str(output) if output is not None else None, DEFAULT_BATCH_AUTHORIZATION)
    auth = _load_authorization()
    if not confirm:
        raise CorpusRuntimeBlocked("EXPLICIT_BATCH_AUTHORIZATION_REQUIRED", "authorize-batch", "pass --confirm")
    if not authorizer.strip():
        raise CorpusRuntimeBlocked("AUTHORIZER_REQUIRED", "authorize-batch", "authorizer is empty")
    manifest_bytes = manifest_path.read_bytes()
    manifest = _read_json(manifest_path)
    if manifest.get("status") != "PENDING_HUMAN_AUTHORIZATION":
        raise CorpusRuntimeBlocked("MANIFEST_NOT_PENDING", "authorize-batch", str(manifest_path))
    if manifest.get("project_id") != PROJECT_ID or manifest.get("approved_root") != str(APPROVED_ROOT) or manifest.get("database_target") != ISOLATED_DATABASE:
        raise CorpusRuntimeBlocked("MANIFEST_SCOPE_MISMATCH", "authorize-batch", str(manifest_path))
    if not 1 <= int(manifest.get("selected_count", 0)) <= int(auth["maximum_documents"]):
        raise CorpusRuntimeBlocked("DOCUMENT_CEILING_EXCEEDED", "authorize-batch", str(manifest.get("selected_count")))
    selected_hashes: dict[str, str] = {}
    for item in manifest["selected_files"]:
        path = resolve_child_path(_approved_root(), str(item["relative_path"]))
        current_hash = _sha256(path)
        if current_hash != item["sha256"]:
            raise CorpusRuntimeBlocked("FILE_HASH_CHANGED", "authorize-batch", str(item["relative_path"]))
        selected_hashes[str(item["relative_path"])] = current_hash
    if expires_hours < 1 or expires_hours > 168:
        raise CorpusRuntimeBlocked("AUTHORIZATION_EXPIRY_INVALID", "authorize-batch", str(expires_hours))
    authorized_at = datetime.now(UTC)
    receipt = {
        "receipt_type": "P6.15_CONTROLLED_RUNTIME_BATCH_AUTHORIZATION",
        "status": "AUTHORIZED",
        "authorized_at": authorized_at.isoformat(),
        "expires_at": (authorized_at + timedelta(hours=expires_hours)).isoformat(),
        "authorizer": authorizer.strip(),
        "batch_id": manifest["batch_id"],
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "selected_file_hashes": selected_hashes,
        "project_id": PROJECT_ID,
        "approved_root": str(APPROVED_ROOT),
        "isolated_database": ISOLATED_DATABASE,
        "execution_ceiling": int(manifest["selected_count"]),
        "embedding_policy": POLICY,
        "allow_ocr": False,
        "allow_cloud_models": False,
        "allow_source_modification": False,
        "allow_operational_migration": False,
        "provenance": doctor_result["provenance"],
    }
    _write_json(output, receipt)
    return {"status": "PASS", "action": "authorize-batch", "authorization_path": str(output), "authorization": receipt}


def _verify_authorization(manifest_path: Path, authorization_path: Path) -> tuple[dict[str, Any], dict[str, Any], list[Path]]:
    auth = _load_authorization()
    manifest_bytes = manifest_path.read_bytes()
    manifest = _read_json(manifest_path)
    receipt = _read_json(authorization_path)
    if receipt.get("status") != "AUTHORIZED":
        raise CorpusRuntimeBlocked("BATCH_NOT_AUTHORIZED", "ingest", str(authorization_path))
    if receipt.get("manifest_sha256") != _sha256_bytes(manifest_bytes):
        raise CorpusRuntimeBlocked("MANIFEST_HASH_MISMATCH", "ingest", str(manifest_path))
    manifest_scope = {
        "project_id": manifest.get("project_id"),
        "approved_root": manifest.get("approved_root"),
        "isolated_database": manifest.get("database_target"),
        "embedding_policy": manifest.get("embedding_policy"),
    }
    for key, expected in (("project_id", PROJECT_ID), ("approved_root", str(APPROVED_ROOT)), ("isolated_database", ISOLATED_DATABASE), ("embedding_policy", POLICY)):
        if receipt.get(key) != expected or manifest_scope.get(key) != expected:
            raise CorpusRuntimeBlocked("BATCH_AUTHORIZATION_SCOPE_MISMATCH", "ingest", key)
    try:
        expires_at = datetime.fromisoformat(str(receipt["expires_at"]))
    except (KeyError, ValueError) as exc:
        raise CorpusRuntimeBlocked("BATCH_AUTHORIZATION_MALFORMED", "ingest", "expires_at") from exc
    if expires_at <= datetime.now(UTC):
        raise CorpusRuntimeBlocked("BATCH_AUTHORIZATION_EXPIRED", "ingest", str(expires_at))
    if receipt.get("batch_id") != manifest.get("batch_id"):
        raise CorpusRuntimeBlocked("BATCH_ID_MISMATCH", "ingest", str(manifest_path))
    selected: list[Path] = []
    manifest_hashes = {str(item["relative_path"]): str(item["sha256"]) for item in manifest.get("selected_files", [])}
    if receipt.get("selected_file_hashes") != manifest_hashes:
        raise CorpusRuntimeBlocked("SELECTED_HASH_BINDING_MISMATCH", "ingest", str(manifest_path))
    if len(manifest_hashes) > int(auth["maximum_documents"]) or len(manifest_hashes) > int(receipt.get("execution_ceiling", 0)):
        raise CorpusRuntimeBlocked("EXECUTION_CEILING_EXCEEDED", "ingest", str(len(manifest_hashes)))
    for relative, expected_hash in manifest_hashes.items():
        try:
            path = resolve_child_path(_approved_root(), relative)
        except CorpusBoundaryError as exc:
            raise CorpusRuntimeBlocked("AUTHORIZED_FILE_OUTSIDE_ROOT", "ingest", relative) from exc
        if path.suffix.casefold() != ".pdf":
            raise CorpusRuntimeBlocked("UNAUTHORIZED_FILE_TYPE", "ingest", relative)
        if _sha256(path) != expected_hash:
            raise CorpusRuntimeBlocked("FILE_HASH_CHANGED", "ingest", relative)
        selected.append(path)
    return manifest, receipt, selected


def _row_counts(connection: psycopg.Connection[Any], project_id: str) -> dict[str, int]:
    queries = {
        "documents": "SELECT count(*) FROM corpus_documents WHERE project_id = %s",
        "versions": "SELECT count(*) FROM corpus_document_versions v JOIN corpus_documents d ON d.id = v.document_id WHERE d.project_id = %s",
        "pages": "SELECT count(*) FROM corpus_pages p JOIN corpus_documents d ON d.id = p.document_id WHERE d.project_id = %s",
        "chunks": "SELECT count(*) FROM corpus_chunks c JOIN corpus_documents d ON d.id = c.document_id WHERE d.project_id = %s",
        "evidence_spans": "SELECT count(*) FROM corpus_evidence_spans e JOIN corpus_documents d ON d.id = e.document_id WHERE d.project_id = %s",
        "ingestion_events": "SELECT count(*) FROM corpus_ingestion_events e JOIN corpus_ingestion_batches b ON b.id = e.batch_id WHERE b.project_id = %s",
        "batches": "SELECT count(*) FROM corpus_ingestion_batches WHERE project_id = %s",
        "quarantined_documents": "SELECT count(*) FROM corpus_documents WHERE project_id = %s AND status LIKE 'QUARANTINED%%'",
        "duplicate_documents": "SELECT count(*) FROM corpus_documents WHERE project_id = %s AND status = 'DUPLICATE'",
    }
    return {name: _count(connection, query, project_id) for name, query in queries.items()}


def ingest(*, manifest_path: Path, authorization_path: Path) -> dict[str, Any]:
    doctor_result = _doctor_or_raise()
    manifest, _receipt, selected = _verify_authorization(manifest_path, authorization_path)
    try:
        projection = ensure_project_context_projection(
            project_id=str(PROJECT_ID),
            operational_database=OPERATIONAL_DATABASE,
            corpus_database=ISOLATED_DATABASE,
            operational_revision=OPERATIONAL_REVISION,
            connect=_connect,
        )
    except ProjectContextProjectionBlocked as exc:
        raise CorpusRuntimeBlocked(exc.reason, "project-context-projection", exc.detail) from exc
    before_hashes = {str(path): _sha256(path) for path in selected}
    with _connect(ISOLATED_DATABASE) as connection:
        before_counts = _row_counts(connection, _project_id())
    settings = _settings(ISOLATED_DATABASE)
    result = run_fixture_pilot(
        _approved_root(),
        project_id=PROJECT_ID,
        max_documents=len(selected),
        embedding_policy=POLICY,
        selected_paths=selected,
    )
    try:
        with session_scope(settings) as session:
            persisted_batch_id = persist_pilot_result(session, result, corpus_root=_approved_root(), project_id=_project_id())
    except Exception as exc:
        raise CorpusRuntimeBlocked(type(exc).__name__.upper(), "ingest", _sanitize(exc), write_occurred=False) from exc
    after_hashes = {str(path): _sha256(path) for path in selected}
    if before_hashes != after_hashes:
        raise CorpusRuntimeBlocked("SOURCE_MODIFIED", "ingest", "approved source hash changed", write_occurred=True)
    with _connect(ISOLATED_DATABASE) as connection:
        after_counts = _row_counts(connection, _project_id())
    unchanged = before_counts == after_counts
    receipt_data = result["receipt"]
    idempotency = receipt_data.get("idempotency", "NEW_BATCH_PUBLISHED" if not unchanged else "EXACT_DUPLICATE_EXISTING_VERSION")
    return {
        "status": "PASS",
        "action": "ingest",
        "batch_id": manifest["batch_id"],
        "persisted_batch_id": persisted_batch_id,
        "authorization_receipt": str(authorization_path.resolve()),
        "manifest_sha256": _sha256(manifest_path),
        "selected_count": len(selected),
        "pipeline_receipt": receipt_data,
        "idempotency": idempotency,
        "counts_before": before_counts,
        "counts_after": after_counts,
        "counts_unchanged": unchanged,
        "source_hashes_before": before_hashes,
        "source_hashes_after": after_hashes,
        "project_context_projection": projection,
        "provenance": doctor_result["provenance"],
    }


def search(*, query: str, project_id: str | None = None, limit: int = 25) -> dict[str, Any]:
    normalized = query.strip()
    if not normalized:
        raise CorpusRuntimeBlocked("QUERY_REQUIRED", "search", "query is empty")
    doctor_result = _doctor_or_raise()
    active_project_id = PROJECT_ID
    if project_id is not None and project_id != active_project_id:
        raise CorpusRuntimeBlocked("PROJECT_SCOPE_MISMATCH", "search", project_id)
    limit = min(max(limit, 1), 100)
    like = f"%{normalized}%"
    with _connect(ISOLATED_DATABASE) as connection:
        rows = connection.execute(
            """
            SELECT d.id::text, v.id::text, v.source_path, p.page_number,
                   c.id::text, e.id::text, e.quoted_text, c.normalized_text,
                   e.char_start, e.char_end
            FROM corpus_evidence_spans e
            JOIN corpus_chunks c ON c.id = e.chunk_id
            JOIN corpus_pages p ON p.id = e.page_id
            JOIN corpus_documents d ON d.id = e.document_id
            JOIN corpus_document_versions v ON v.id = e.document_version_id
            WHERE d.project_id = %s
              AND c.embedding_status = 'PUBLISHED'
              AND (c.normalized_text ILIKE %s OR e.quoted_text ILIKE %s)
            ORDER BY d.id::text, v.id::text, p.page_number, c.chunk_order, e.char_start
            LIMIT %s
            """,
            (active_project_id, like, like, limit),
        ).fetchall()
    results: list[dict[str, Any]] = []
    needle = normalized.casefold()
    for row in rows:
        excerpt = str(row[6] or row[7] or "")[:600]
        match_count = str(row[7] or "").casefold().count(needle)
        results.append(
            {
                "project_id": active_project_id,
                "source_id": str(row[0]),
                "source_type": "PDF",
                "origin": str(row[2]),
                "retrieval_status": "PUBLISHED",
                "normalization_status": "NORMALIZED",
                "content_version": str(row[1]),
                "selection_status": "SELECTED",
                "document_id": str(row[0]),
                "document_version_id": str(row[1]),
                "source_filename": Path(str(row[2])).name,
                "source_path": str(row[2]),
                "page_number": int(row[3]),
                "chunk_id": str(row[4]),
                "evidence_span_id": str(row[5]),
                "evidence_id": str(row[5]),
                "evidence_span": {
                    "text": excerpt,
                    "char_start": int(row[8]),
                    "char_end": int(row[9]),
                    "page_number": int(row[3]),
                },
                "matched_text_excerpt": excerpt,
                "retrieval_policy": POLICY,
                "deterministic_score": float(match_count),
                "match_explanation": {"method": "casefold_substring", "query": normalized, "match_count": match_count},
            }
        )
    return {
        "status": "PASS",
        "action": "search",
        "project_id": active_project_id,
        "query": normalized,
        "mode": "keyword",
        "retrieval_policy": POLICY,
        "result_count": len(results),
        "results": results,
        "provenance": doctor_result["provenance"],
    }


def _batch_file_counts() -> dict[str, int]:
    batch_files = list((RUNTIME_ARTIFACT_ROOT / "batches").glob("*.json")) if (RUNTIME_ARTIFACT_ROOT / "batches").exists() else []
    authorization_files = list((RUNTIME_ARTIFACT_ROOT / "authorizations").glob("*.json")) if (RUNTIME_ARTIFACT_ROOT / "authorizations").exists() else []
    if DEFAULT_BATCH_AUTHORIZATION.exists():
        authorization_files.append(DEFAULT_BATCH_AUTHORIZATION)
    authorized = 0
    seen: set[str] = set()
    for path in authorization_files:
        try:
            receipt = _read_json(path)
        except CorpusRuntimeBlocked:
            continue
        batch_id = str(receipt.get("batch_id", path))
        if receipt.get("status") == "AUTHORIZED" and batch_id not in seen:
            seen.add(batch_id)
            authorized += 1
    pending = 0
    for path in batch_files:
        try:
            batch = _read_json(path)
            pending += int(batch.get("status") == "PENDING_HUMAN_AUTHORIZATION" and str(batch.get("batch_id")) not in seen)
        except CorpusRuntimeBlocked:
            continue
    return {"pending_review_batches": pending, "authorized_batches": authorized}


def status() -> dict[str, Any]:
    doctor_result = _doctor_or_raise()
    with _connect(ISOLATED_DATABASE) as connection:
        counts = _row_counts(connection, _project_id())
    return {
        "status": "PASS",
        "action": "status",
        "runtime_mode": "P6_15_CONTROLLED_OPERATOR_RUNTIME",
        "authorization_status": "AUTHORIZED",
        "approved_root": str(APPROVED_ROOT),
        "corpus_counts": counts,
        "review_counts": _batch_file_counts(),
        "isolated_revision": ISOLATED_REVISION,
        "operational_revision": OPERATIONAL_REVISION,
        "excluded_migration": EXCLUDED_REVISION,
        "embedding_policy": POLICY,
        "ocr_status": "DISABLED",
        "cloud_model_status": "DISABLED",
        "source_modification": "DISABLED",
        "provenance": doctor_result["provenance"],
    }


def stop() -> dict[str, Any]:
    doctor_result = _doctor_or_raise()
    return {"status": "PASS", "action": "stop", "runtime_status": "STOPPED", "data_mutation": "NONE", "automatic_ingestion": "DISABLED", "provenance": doctor_result["provenance"]}


def restart() -> dict[str, Any]:
    doctor_result = _doctor_or_raise()
    return {"status": "PASS", "action": "restart", "runtime_status": "RESTARTED_NO_AUTO_INGEST", "data_mutation": "NONE", "automatic_ingestion": "DISABLED", "provenance": doctor_result["provenance"]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P6.15 bounded governed corpus runtime")
    parser.add_argument("action", choices=["doctor", "scan", "review", "authorize-batch", "ingest", "search", "status", "stop", "restart"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scan-manifest", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--authorization-file", type=Path, help="External local authorization JSON for runtime configuration.")
    parser.add_argument("--database", help="Explicit corpus database override; must match authorized scope.")
    parser.add_argument("--artifact-root", type=Path, help="Runtime artifact root override.")
    parser.add_argument("--select", action="append", default=[])
    parser.add_argument("--authorizer", default="")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expires-hours", type=int, default=24)
    parser.add_argument("--query", default="")
    parser.add_argument("--project-id")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--json", action="store_true", help="retained for launcher compatibility")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        global AUTHORIZATION_FILE, ISOLATED_DATABASE, RUNTIME_ARTIFACT_ROOT
        if args.authorization_file is not None:
            AUTHORIZATION_FILE = args.authorization_file
        if args.database:
            ISOLATED_DATABASE = args.database
        if args.artifact_root is not None:
            RUNTIME_ARTIFACT_ROOT = args.artifact_root
        if args.action == "doctor":
            result = doctor()
        elif args.action == "scan":
            result = scan(output=args.output)
        elif args.action == "review":
            result = review(scan_manifest=args.scan_manifest, selected=args.select)
        elif args.action == "authorize-batch":
            if not args.manifest:
                raise CorpusRuntimeBlocked("MANIFEST_REQUIRED", "authorize-batch", "--manifest is required")
            result = authorize_batch(
                manifest_path=_resolve_output(str(args.manifest), args.manifest),
                output=args.output,
                authorizer=args.authorizer,
                confirm=args.confirm,
                expires_hours=args.expires_hours,
            )
        elif args.action == "ingest":
            if not args.manifest or not args.authorization:
                raise CorpusRuntimeBlocked("MANIFEST_AND_AUTHORIZATION_REQUIRED", "ingest", "both paths are required")
            result = ingest(
                manifest_path=_resolve_output(str(args.manifest), args.manifest),
                authorization_path=_resolve_output(str(args.authorization), args.authorization),
            )
        elif args.action == "search":
            result = search(query=args.query, project_id=args.project_id, limit=args.limit)
        elif args.action == "status":
            result = status()
        elif args.action == "stop":
            result = stop()
        else:
            result = restart()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except CorpusRuntimeBlocked as exc:
        payload = {
            "status": f"P6_15_CONTROLLED_RUNTIME_BLOCKED_BY_{exc.reason}",
            "blocked_stage": exc.stage,
            "error_detail": exc.detail,
            "affected_artifact": str(args.manifest or args.authorization or "none"),
            "write_occurred": exc.write_occurred,
            "isolated_database": ISOLATED_DATABASE,
            "operational_database": OPERATIONAL_DATABASE,
            "isolated_revision": ISOLATED_REVISION,
            "operational_revision": OPERATIONAL_REVISION,
            "safe_next_action": "correct the named boundary or authorization artifact and rerun the same bounded operation",
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports a typed BLOCKED status
        payload = {
            "status": f"P6_15_CONTROLLED_RUNTIME_BLOCKED_BY_{type(exc).__name__.upper()}",
            "blocked_stage": args.action,
            "error_detail": _sanitize(exc),
            "affected_artifact": str(args.manifest or args.authorization or "none"),
            "write_occurred": False,
            "isolated_database": ISOLATED_DATABASE,
            "operational_database": OPERATIONAL_DATABASE,
            "isolated_revision": ISOLATED_REVISION,
            "operational_revision": OPERATIONAL_REVISION,
            "safe_next_action": "inspect the sanitized error and rerun only after the cause is resolved",
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
