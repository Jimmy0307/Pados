# SPDX-License-Identifier: AGPL-3.0-only
"""Application service for the P6.15 local operator workspace.

The service is intentionally HTTP-agnostic.  It owns staging, manifests,
backend-generated authorization receipts, project scoping, and the dedicated
Corpus database boundary.  The FastAPI router only translates these typed
results and errors into HTTP responses.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from sqlalchemy import MetaData, Table, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pados.config import Settings
from pados.control import service as control_service
from pados.corpus.configuration import OPERATIONAL_REVISION
from pados.corpus.persistence import persist_pilot_result
from pados.corpus.pipeline import run_fixture_pilot
from pados.corpus.project_context import (
    ProjectContextProjectionBlocked,
    ensure_project_context_projection,
)
from pados.corpus.security import CorpusBoundaryError, resolve_child_path
from pados.db import get_engine

from .operator_config import (
    KEYWORD_POLICY,
    OperatorConfiguration,
    OperatorConfigurationError,
    corpus_settings,
    masked_provenance,
    operational_settings,
    resolve_operator_configuration,
)

PUBLIC_SCHEMA = "public"
MAX_RESULT_LIMIT = 100
PENDING_STATES = {"STAGED", "SCANNED", "PENDING_HUMAN_AUTHORIZATION"}


class OperatorError(RuntimeError):
    """A safe, stable application error suitable for an API response."""

    def __init__(self, code: str, message: str, *, status_code: int = 409, write_occurred: bool = False) -> None:
        self.code = code
        self.message = message[:500]
        self.status_code = status_code
        self.write_occurred = write_occurred
        super().__init__(self.message)


@dataclass(frozen=True)
class StagedUpload:
    filename: str
    mime_type: str
    content: bytes


def configuration() -> OperatorConfiguration:
    try:
        return resolve_operator_configuration()
    except OperatorConfigurationError as exc:
        raise OperatorError("CONFIGURATION_INVALID", str(exc), status_code=503) from exc


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, bytes):
        return "[BINARY]"
    return str(value)


def _row(row: Any) -> dict[str, Any]:
    return {str(key): _safe_value(value) for key, value in row.items()}


def _table(engine: Engine, name: str) -> Table:
    return Table(name, MetaData(), schema=PUBLIC_SCHEMA, autoload_with=engine)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorError("ARTIFACT_INVALID", "operator artifact cannot be read", status_code=503) from exc
    if not isinstance(value, dict):
        raise OperatorError("ARTIFACT_INVALID", "operator artifact must be an object", status_code=503)
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise OperatorError("INVALID_ID", f"{field_name} must be a UUID", status_code=422) from exc


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _batch_path(config: OperatorConfiguration, batch_id: str) -> Path:
    _uuid(batch_id, "batch_id")
    return config.artifact_root / "batches" / f"{batch_id}.json"


def _receipt_path(config: OperatorConfiguration, batch_id: str) -> Path:
    _uuid(batch_id, "batch_id")
    return config.artifact_root / "authorizations" / f"{batch_id}.json"


def _load_manifest(config: OperatorConfiguration, batch_id: str) -> dict[str, Any]:
    path = _batch_path(config, batch_id)
    if not path.is_file():
        raise OperatorError("BATCH_NOT_FOUND", "batch not found", status_code=404)
    manifest = _read_json(path)
    if manifest.get("batch_id") != batch_id:
        raise OperatorError("BATCH_ID_MISMATCH", "batch identity does not match artifact", status_code=409)
    if manifest.get("database_target") != config.corpus_database:
        raise OperatorError("DATABASE_SCOPE_MISMATCH", "batch targets a different Corpus database", status_code=409)
    return manifest


def _manifest_hash(config: OperatorConfiguration, batch_id: str) -> str:
    return _sha256_file(_batch_path(config, batch_id))


def _validate_filename(filename: str) -> str:
    name = str(filename or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or Path(name).name != name:
        raise OperatorError("UNSAFE_FILENAME", "filename must be a simple local filename", status_code=422)
    if Path(name).suffix.casefold() != ".pdf":
        raise OperatorError("UNSUPPORTED_FILE_TYPE", "only PDF files are accepted", status_code=422)
    return name


def _batch_root(config: OperatorConfiguration, manifest: dict[str, Any]) -> Path:
    raw = Path(str(manifest.get("approved_root", ""))).resolve()
    if not _within(raw, config.staging_root) or raw == config.staging_root:
        raise OperatorError("STAGING_BOUNDARY_INVALID", "batch staging root is outside the approved staging root", status_code=503)
    if not raw.is_dir():
        raise OperatorError("STAGING_ROOT_MISSING", "batch staging root is unavailable", status_code=503)
    return raw


def _assert_batch_tree(config: OperatorConfiguration, manifest: dict[str, Any]) -> Path:
    root = _batch_root(config, manifest)
    expected = {str(item["relative_path"]): str(item["sha256"]) for item in manifest.get("files", [])}
    observed: dict[str, str] = {}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path != root and current_path.is_symlink():
            raise OperatorError("SYMLINK_REPARSE_ESCAPE", "staging tree contains a link", status_code=422)
        for name in directories:
            if (current_path / name).is_symlink():
                raise OperatorError("SYMLINK_REPARSE_ESCAPE", "staging tree contains a link", status_code=422)
        for name in files:
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise OperatorError("SYMLINK_REPARSE_ESCAPE", "staging tree contains a link", status_code=422)
            observed[relative] = _sha256_file(candidate)
    if observed != expected:
        raise OperatorError("STAGING_TREE_CHANGED", "staging files differ from the reviewed manifest", status_code=409)
    return root


@contextmanager
def _read_only_session(settings: Settings) -> Iterator[Session]:
    engine = get_engine(settings)
    session = Session(engine)
    try:
        session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _connect(config: OperatorConfiguration, database: str) -> psycopg.Connection[Any]:
    connection_options: dict[str, Any] = {
        "host": config.database_host,
        "port": config.database_port,
        "dbname": database,
        "user": config.database_user,
        "password": config.database_password,
        "connect_timeout": config.timeout_seconds,
    }
    return psycopg.connect(**connection_options)


def _operational_project(config: OperatorConfiguration, project_id: str) -> dict[str, Any]:
    project_uuid = _uuid(project_id, "project_id")
    settings = operational_settings(config)
    try:
        with _read_only_session(settings) as session:
            try:
                project = dict(control_service.get_project(session, str(project_uuid)))
            except LookupError as exc:
                raise OperatorError("PROJECT_NOT_FOUND", "project not found", status_code=404) from exc
    except OperatorError:
        raise
    except Exception as exc:
        raise OperatorError("OPERATIONAL_UNAVAILABLE", "operational project authority is unavailable", status_code=503) from exc
    return {key: _safe_value(value) for key, value in project.items()}


def _project_ids(config: OperatorConfiguration) -> list[dict[str, Any]]:
    settings = operational_settings(config)
    try:
        with _read_only_session(settings) as session:
            rows = control_service.list_projects(session)
            return [{key: _safe_value(value) for key, value in dict(row).items()} for row in rows]
    except Exception as exc:
        raise OperatorError("OPERATIONAL_UNAVAILABLE", "operational project authority is unavailable", status_code=503) from exc


def _count(session: Session, table: Table, *conditions: Any) -> int:
    statement = select(func.count()).select_from(table)
    if conditions:
        statement = statement.where(*conditions)
    return int(session.execute(statement).scalar_one())


def _project_corpus_status(config: OperatorConfiguration, project_id: str) -> dict[str, Any]:
    project_uuid = _uuid(project_id, "project_id")
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        document = _table(engine, "corpus_documents")
        version = _table(engine, "corpus_document_versions")
        page = _table(engine, "corpus_pages")
        chunk = _table(engine, "corpus_chunks")
        evidence = _table(engine, "corpus_evidence_spans")
        batch = _table(engine, "corpus_ingestion_batches")
        corpus_project = _table(engine, "projects")
        with Session(engine) as session:
            document_ids = select(document.c.id).where(document.c.project_id == project_uuid)
            counts = {
                "documents": _count(session, document, document.c.project_id == project_uuid),
                "versions": int(session.execute(select(func.count()).select_from(version).where(version.c.document_id.in_(document_ids))).scalar_one()),
                "pages": int(session.execute(select(func.count()).select_from(page).where(page.c.document_id.in_(document_ids))).scalar_one()),
                "chunks": int(session.execute(select(func.count()).select_from(chunk).where(chunk.c.document_id.in_(document_ids))).scalar_one()),
                "evidence_spans": int(session.execute(select(func.count()).select_from(evidence).where(evidence.c.document_id.in_(document_ids))).scalar_one()),
                "batches": _count(session, batch, batch.c.project_id == project_uuid),
                "duplicate": _count(session, document, document.c.project_id == project_uuid, document.c.status == "DUPLICATE"),
                "quarantined": _count(session, document, document.c.project_id == project_uuid, document.c.status.like("QUARANTINED%")),
                "failed": _count(session, document, document.c.project_id == project_uuid, document.c.status.in_(["PARSE_FAILED", "VALIDATION_FAILED", "UNSUPPORTED"])),
                "ocr_required": int(session.execute(select(func.count()).select_from(version).where(version.c.document_id.in_(document_ids), version.c.extraction_status == "OCR_REQUIRED")).scalar_one()),
            }
            projection_exists = session.execute(select(func.count()).select_from(corpus_project).where(corpus_project.c.id == project_uuid)).scalar_one()
        return {
            "status": "READY",
            "database": config.corpus_database,
            "revision": config.corpus_revision,
            "operational_database": config.operational_database,
            "operational_read_only": True,
            "embedding_policy": KEYWORD_POLICY,
            "counts": counts,
            "project_projection": bool(projection_exists),
            "pending_review_batches": _pending_manifest_count(config, project_id),
            "provenance": masked_provenance(config),
        }
    except OperatorError:
        raise
    except Exception as exc:
        raise OperatorError("CORPUS_UNAVAILABLE", "Corpus database is unavailable", status_code=503) from exc
    finally:
        engine.dispose()


def list_projects(*, include_corpus_status: bool = True) -> dict[str, Any]:
    config = configuration()
    projects = []
    for project in _project_ids(config):
        project_id = str(project.get("id"))
        if include_corpus_status:
            try:
                corpus_status = _project_corpus_status(config, project_id)
            except OperatorError as exc:
                if exc.code != "CORPUS_UNAVAILABLE":
                    raise
                corpus_status = {"status": "UNAVAILABLE", "counts": {}}
            projects.append({**project, "corpus_status": corpus_status})
        else:
            projects.append(project)
    return {"status": "ok", "projects": projects, "provenance": masked_provenance(config)}


def get_project(project_id: str) -> dict[str, Any]:
    config = configuration()
    project = _operational_project(config, project_id)
    return {"status": "ok", "project": project, "corpus_status": _project_corpus_status(config, project_id), "provenance": masked_provenance(config)}


def project_status(project_id: str) -> dict[str, Any]:
    config = configuration()
    _operational_project(config, project_id)
    return {"status": "ok", "project_id": project_id, "corpus": _project_corpus_status(config, project_id)}


def _pending_manifest_count(config: OperatorConfiguration, project_id: str) -> int:
    root = config.artifact_root / "batches"
    if not root.is_dir():
        return 0
    count = 0
    for path in root.glob("*.json"):
        try:
            manifest = _read_json(path)
        except OperatorError:
            continue
        count += int(manifest.get("project_id") == project_id and manifest.get("status") == "PENDING_HUMAN_AUTHORIZATION")
    return count


def _known_hashes(config: OperatorConfiguration, project_id: str) -> set[str]:
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        document = _table(engine, "corpus_documents")
        version = _table(engine, "corpus_document_versions")
        with Session(engine) as session:
            rows = session.execute(select(version.c.content_hash).join(document, document.c.id == version.c.document_id).where(document.c.project_id == _uuid(project_id, "project_id"))).all()
            return {str(row[0]) for row in rows}
    except Exception as exc:
        raise OperatorError("CORPUS_UNAVAILABLE", "Corpus database is unavailable", status_code=503) from exc
    finally:
        engine.dispose()


def stage_batch(project_id: str, uploads: Iterable[StagedUpload]) -> dict[str, Any]:
    config = configuration()
    _operational_project(config, project_id)
    upload_list = list(uploads)
    if not 1 <= len(upload_list) <= config.max_documents:
        raise OperatorError("DOCUMENT_CEILING_EXCEEDED", "file selection exceeds the configured document ceiling", status_code=422)
    batch_id = str(uuid4())
    root = (config.staging_root / project_id / batch_id).resolve()
    if not _within(root, config.staging_root):
        raise OperatorError("STAGING_BOUNDARY_INVALID", "batch staging root is outside the approved staging root", status_code=503)
    root.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    seen_names: dict[str, str] = {}
    try:
        for upload in upload_list:
            filename = _validate_filename(upload.filename)
            if upload.mime_type.lower() != "application/pdf":
                raise OperatorError("UNSUPPORTED_MIME_TYPE", "only application/pdf uploads are accepted", status_code=422)
            if len(upload.content) > config.max_file_bytes:
                raise OperatorError("FILE_SIZE_LIMIT_EXCEEDED", "uploaded file exceeds the configured size ceiling", status_code=422)
            digest = _sha256_bytes(upload.content)
            prior = seen_names.get(filename)
            if prior is not None and prior != digest:
                raise OperatorError("FILENAME_COLLISION", "same filename has different content", status_code=409)
            if prior == digest:
                continue
            seen_names[filename] = digest
            path = root / filename
            path.write_bytes(upload.content)
            entries.append({
                "filename": filename,
                "relative_path": filename,
                "file_size": len(upload.content),
                "sha256": digest,
                "mime_type": upload.mime_type,
                "readability": "STAGED",
                "duplicate_status": "UNKNOWN",
                "ocr_status": "DISABLED_POLICY",
            })
        if not entries:
            raise OperatorError("EMPTY_BATCH", "no unique PDF files were staged", status_code=422)
        manifest = {
            "manifest_type": "P6.15_OPERATOR_BATCH",
            "batch_id": batch_id,
            "project_id": project_id,
            "status": "STAGED",
            "created_at": _now(),
            "approved_root": str(root),
            "database_target": config.corpus_database,
            "operational_database": config.operational_database,
            "files": entries,
            "selected_files": [item["relative_path"] for item in entries],
            "selected_file_hashes": {item["relative_path"]: item["sha256"] for item in entries},
            "parser_policy": "TEXT_FIRST_PYPDF",
            "embedding_policy": KEYWORD_POLICY,
            "ocr_policy": "DISABLED",
            "cloud_model_policy": "DISABLED",
            "source_modification_policy": "DISABLED",
            "document_ceiling": config.max_documents,
            "provenance": masked_provenance(config),
        }
        _write_json(_batch_path(config, batch_id), manifest)
        return {"status": "ok", "batch": manifest, "manifest_sha256": _manifest_hash(config, batch_id)}
    except Exception:
        for path in root.glob("*"):
            path.unlink(missing_ok=True)
        root.rmdir()
        raise


def scan_batch(batch_id: str) -> dict[str, Any]:
    config = configuration()
    manifest = _load_manifest(config, batch_id)
    if manifest.get("status") not in {"STAGED", "SCANNED", "PENDING_HUMAN_AUTHORIZATION"}:
        raise OperatorError("FORBIDDEN_TRANSITION", "batch cannot be scanned in its current state", status_code=409)
    _assert_batch_tree(config, manifest)
    known_hashes = _known_hashes(config, str(manifest["project_id"]))
    for item in manifest["files"]:
        item["readability"] = "READABLE"
        item["duplicate_status"] = "EXACT_DUPLICATE_EXISTING_VERSION" if item["sha256"] in known_hashes else "NEW_CONTENT"
        item["ocr_status"] = "DETERMINED_AT_INGEST;OCR_DISABLED"  # no fake progress or OCR claim
    manifest["status"] = "SCANNED"
    manifest["scanned_at"] = _now()
    _write_json(_batch_path(config, batch_id), manifest)
    return {"status": "ok", "batch": manifest, "manifest_sha256": _manifest_hash(config, batch_id)}


def review_batch(batch_id: str, selected_files: Iterable[str] = ()) -> dict[str, Any]:
    config = configuration()
    manifest = _load_manifest(config, batch_id)
    if manifest.get("status") not in {"SCANNED", "PENDING_HUMAN_AUTHORIZATION"}:
        raise OperatorError("FORBIDDEN_TRANSITION", "batch is not ready for human review", status_code=409)
    available = {str(item["relative_path"]): item for item in manifest.get("files", [])}
    selected = list(selected_files) or sorted(available)
    if not selected or any(item not in available for item in selected):
        raise OperatorError("SELECTED_FILE_MISMATCH", "selected files do not match the staged manifest", status_code=409)
    if len(selected) > config.max_documents:
        raise OperatorError("DOCUMENT_CEILING_EXCEEDED", "selected files exceed the document ceiling", status_code=422)
    manifest["selected_files"] = selected
    manifest["selected_file_hashes"] = {name: str(available[name]["sha256"]) for name in selected}
    manifest["status"] = "PENDING_HUMAN_AUTHORIZATION"
    manifest["reviewed_at"] = _now()
    _write_json(_batch_path(config, batch_id), manifest)
    return {"status": "ok", "batch": manifest, "manifest_sha256": _manifest_hash(config, batch_id)}


def _verify_selected_files(config: OperatorConfiguration, manifest: dict[str, Any]) -> list[Path]:
    root = _assert_batch_tree(config, manifest)
    expected = {str(key): str(value) for key, value in manifest.get("selected_file_hashes", {}).items()}
    files: list[Path] = []
    for relative, expected_hash in expected.items():
        try:
            path = resolve_child_path(root, relative)
        except CorpusBoundaryError as exc:
            raise OperatorError("PATH_BOUNDARY_VIOLATION", "selected file is outside the batch root", status_code=422) from exc
        if path.suffix.casefold() != ".pdf" or _sha256_file(path) != expected_hash:
            raise OperatorError("FILE_HASH_CHANGED", "selected file hash differs from the reviewed value", status_code=409)
        files.append(path)
    return files


def authorize_batch(batch_id: str, *, manifest_sha256: str, selected_file_hashes: dict[str, str], expires_hours: int = 24, ingest_after: bool = False) -> dict[str, Any]:
    config = configuration()
    manifest = _load_manifest(config, batch_id)
    if manifest.get("status") != "PENDING_HUMAN_AUTHORIZATION":
        raise OperatorError("HUMAN_AUTHORIZATION_REQUIRED", "batch is not pending human authorization", status_code=409)
    reviewed_manifest_hash = _manifest_hash(config, batch_id)
    if manifest_sha256 != reviewed_manifest_hash:
        raise OperatorError("MANIFEST_HASH_MISMATCH", "manifest changed since review", status_code=409)
    expected = {str(key): str(value) for key, value in manifest.get("selected_file_hashes", {}).items()}
    if selected_file_hashes != expected:
        raise OperatorError("SELECTED_HASH_BINDING_MISMATCH", "selected file hashes do not match the reviewed manifest", status_code=409)
    if not 1 <= expires_hours <= 168:
        raise OperatorError("AUTHORIZATION_EXPIRY_INVALID", "authorization expiry must be 1 through 168 hours", status_code=422)
    _verify_selected_files(config, manifest)
    authorized_at = dt.datetime.now(dt.UTC)
    receipt = {
        "receipt_type": "P6.15_OPERATOR_BATCH_AUTHORIZATION",
        "status": "AUTHORIZED",
        "authorized_at": authorized_at.isoformat(),
        "expires_at": (authorized_at + dt.timedelta(hours=expires_hours)).isoformat(),
        "authorizer": config.operator_name,
        "batch_id": batch_id,
        "manifest_sha256": reviewed_manifest_hash,
        "selected_file_hashes": expected,
        "project_id": manifest["project_id"],
        "approved_root": manifest["approved_root"],
        "database_target": config.corpus_database,
        "execution_ceiling": len(expected),
        "embedding_policy": KEYWORD_POLICY,
        "allow_ocr": False,
        "allow_cloud_models": False,
        "allow_source_modification": False,
        "allow_operational_migration": False,
        "provenance": masked_provenance(config),
    }
    manifest["status"] = "AUTHORIZED"
    manifest["authorized_at"] = receipt["authorized_at"]
    manifest["authorization_expires_at"] = receipt["expires_at"]
    _write_json(_batch_path(config, batch_id), manifest)
    authorized_manifest_hash = _manifest_hash(config, batch_id)
    receipt["manifest_sha256"] = authorized_manifest_hash
    _write_json(_receipt_path(config, batch_id), receipt)
    result = {"status": "ok", "batch": manifest, "authorization": {key: value for key, value in receipt.items() if key != "provenance"}, "manifest_sha256": authorized_manifest_hash}
    if ingest_after:
        result["ingest"] = ingest_batch(batch_id)
    return result


def _verified_receipt(config: OperatorConfiguration, manifest: dict[str, Any]) -> dict[str, Any]:
    path = _receipt_path(config, str(manifest["batch_id"]))
    if not path.is_file():
        raise OperatorError("UNAUTHORIZED_INGEST", "a backend authorization receipt is required before ingest", status_code=403)
    receipt = _read_json(path)
    if receipt.get("status") != "AUTHORIZED":
        raise OperatorError("UNAUTHORIZED_INGEST", "authorization receipt is not active", status_code=403)
    if receipt.get("manifest_sha256") != _manifest_hash(config, str(manifest["batch_id"])):
        raise OperatorError("MANIFEST_HASH_MISMATCH", "manifest changed after authorization", status_code=409)
    if receipt.get("batch_id") != manifest.get("batch_id") or receipt.get("project_id") != manifest.get("project_id"):
        raise OperatorError("BATCH_SCOPE_MISMATCH", "authorization receipt is bound to another batch or project", status_code=409)
    if receipt.get("database_target") != config.corpus_database or receipt.get("embedding_policy") != KEYWORD_POLICY:
        raise OperatorError("POLICY_SCOPE_MISMATCH", "authorization policy is not valid for this runtime", status_code=409)
    try:
        expires_at = dt.datetime.fromisoformat(str(receipt["expires_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise OperatorError("AUTHORIZATION_MALFORMED", "authorization expiry is malformed", status_code=409) from exc
    if expires_at <= dt.datetime.now(dt.UTC):
        raise OperatorError("AUTHORIZATION_EXPIRED", "authorization receipt has expired", status_code=403)
    if receipt.get("selected_file_hashes") != manifest.get("selected_file_hashes"):
        raise OperatorError("SELECTED_HASH_BINDING_MISMATCH", "authorized file selection changed", status_code=409)
    return receipt


def _ingest_counts(config: OperatorConfiguration, project_id: str) -> dict[str, int]:
    result = _project_corpus_status(config, project_id)
    counts = result.get("counts")
    if not isinstance(counts, dict):
        raise OperatorError("COUNT_RESULT_MALFORMED", "Corpus count result is malformed", status_code=500)
    return {str(key): int(value) for key, value in counts.items()}


def ingest_batch(batch_id: str) -> dict[str, Any]:
    config = configuration()
    manifest = _load_manifest(config, batch_id)
    if manifest.get("status") == "PUBLISHED":
        return {"status": "ok", "batch_id": batch_id, "state": "PUBLISHED", "idempotency": "SAFE_RESUME_ALREADY_PUBLISHED"}
    if manifest.get("status") not in {"AUTHORIZED", "FAILED", "INGESTING"}:
        raise OperatorError("UNAUTHORIZED_INGEST", "batch must be authorized before ingest", status_code=403)
    receipt = _verified_receipt(config, manifest)
    selected = _verify_selected_files(config, manifest)
    project_id = str(manifest["project_id"])
    before = _ingest_counts(config, project_id)
    manifest["status"] = "INGESTING"
    manifest["ingesting_at"] = _now()
    _write_json(_batch_path(config, batch_id), manifest)
    try:
        projection = ensure_project_context_projection(
            project_id=project_id,
            operational_database=config.operational_database,
            corpus_database=config.corpus_database,
            operational_revision=OPERATIONAL_REVISION,
            connect=lambda database: _connect(config, database),
        )
        settings = corpus_settings(config)
        root = _batch_root(config, manifest)
        result = run_fixture_pilot(
            root,
            project_id=project_id,
            max_documents=len(selected),
            embedding_policy=KEYWORD_POLICY,
            selected_paths=selected,
            batch_id=batch_id,
        )
        engine = get_engine(settings)
        try:
            with Session(engine) as session:
                persisted_batch_id = persist_pilot_result(session, result, corpus_root=root, project_id=project_id)
        finally:
            engine.dispose()
        after = _ingest_counts(config, project_id)
        source_hashes_after = {str(path): _sha256_file(path) for path in selected}
        source_hashes_before = {
            str(path): str(manifest["selected_file_hashes"][path.relative_to(_batch_root(config, manifest)).as_posix()])
            for path in selected
        }
        if source_hashes_before != source_hashes_after:
            raise OperatorError("SOURCE_MODIFIED", "approved source hash changed during ingest", status_code=409, write_occurred=True)
        idempotency = str(result["receipt"].get("idempotency", "NEW_BATCH_PUBLISHED"))
        manifest["status"] = "PUBLISHED"
        manifest["published_at"] = _now()
        manifest["idempotency"] = idempotency
        manifest["persisted_batch_id"] = persisted_batch_id
        manifest["counts_before"] = before
        manifest["counts_after"] = after
        manifest["source_hashes_before"] = source_hashes_before
        manifest["source_hashes_after"] = source_hashes_after
        manifest["project_context_projection"] = projection
        _write_json(_batch_path(config, batch_id), manifest)
        return {
            "status": "ok",
            "batch_id": batch_id,
            "state": "PUBLISHED",
            "idempotency": idempotency,
            "persisted_batch_id": persisted_batch_id,
            "counts_before": before,
            "counts_after": after,
            "counts_unchanged": before == after,
            "source_hashes_before": source_hashes_before,
            "source_hashes_after": source_hashes_after,
            "authorization_expires_at": receipt["expires_at"],
            "project_context_projection": projection,
        }
    except OperatorError:
        manifest["status"] = "FAILED"
        manifest["failed_at"] = _now()
        _write_json(_batch_path(config, batch_id), manifest)
        raise
    except ProjectContextProjectionBlocked as exc:
        manifest["status"] = "FAILED"
        manifest["failed_at"] = _now()
        _write_json(_batch_path(config, batch_id), manifest)
        raise OperatorError("PROJECT_CONTEXT_BLOCKED", "project context projection was blocked", status_code=409) from exc
    except (OSError, ValueError, psycopg.Error) as exc:
        manifest["status"] = "FAILED"
        manifest["failed_at"] = _now()
        _write_json(_batch_path(config, batch_id), manifest)
        raise OperatorError("INGEST_FAILED", "Corpus ingest failed closed", status_code=503) from exc
    except Exception as exc:
        manifest["status"] = "FAILED"
        manifest["failed_at"] = _now()
        _write_json(_batch_path(config, batch_id), manifest)
        raise OperatorError("INGEST_FAILED", "Corpus ingest failed closed", status_code=503) from exc


def retry_batch(batch_id: str) -> dict[str, Any]:
    config = configuration()
    manifest = _load_manifest(config, batch_id)
    if manifest.get("status") == "PUBLISHED":
        return ingest_batch(batch_id)
    if manifest.get("status") not in {"FAILED", "AUTHORIZED"}:
        raise OperatorError("FORBIDDEN_TRANSITION", "only an authorized or failed batch can be retried", status_code=409)
    return ingest_batch(batch_id)


def get_batch(batch_id: str) -> dict[str, Any]:
    config = configuration()
    manifest_path = _batch_path(config, batch_id)
    if manifest_path.is_file():
        manifest = _load_manifest(config, batch_id)
        receipt_path = _receipt_path(config, batch_id)
        return {"status": "ok", "batch": manifest, "authorization_present": receipt_path.is_file(), "manifest_sha256": _manifest_hash(config, batch_id)}
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        from pados.corpus.service import get_batch as get_corpus_batch

        try:
            return get_corpus_batch(engine, batch_id)
        except LookupError as exc:
            raise OperatorError("BATCH_NOT_FOUND", "batch not found", status_code=404) from exc
    finally:
        engine.dispose()


def list_batches(project_id: str, *, page: int = 1, page_size: int = 25) -> dict[str, Any]:
    config = configuration()
    _operational_project(config, project_id)
    manifests: list[dict[str, Any]] = []
    root = config.artifact_root / "batches"
    if root.is_dir():
        for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                manifest = _read_json(path)
            except OperatorError:
                continue
            if manifest.get("project_id") == project_id:
                manifests.append(manifest)
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        from pados.corpus.service import batches as get_corpus_batches

        stored = get_corpus_batches(engine, page=1, page_size=MAX_RESULT_LIMIT, project_id=project_id).get("batches", [])
    finally:
        engine.dispose()
    known_ids = {str(item.get("batch_id") or item.get("id")) for item in manifests}
    for batch in stored:
        if str(batch.get("id")) not in known_ids:
            manifests.append({"batch_id": str(batch.get("id")), "project_id": project_id, "status": batch.get("status"), "database_record": batch})
    total = len(manifests)
    start = max(0, (page - 1) * min(page_size, MAX_RESULT_LIMIT))
    bounded = min(page_size, MAX_RESULT_LIMIT)
    return {"status": "ok", "batches": manifests[start : start + bounded], "pagination": {"page": page, "page_size": bounded, "total": total, "has_next": start + bounded < total}}


def list_documents(project_id: str, *, page: int = 1, page_size: int = 25) -> dict[str, Any]:
    config = configuration()
    _operational_project(config, project_id)
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        document = _table(engine, "corpus_documents")
        version = _table(engine, "corpus_document_versions")
        page_table = _table(engine, "corpus_pages")
        chunk = _table(engine, "corpus_chunks")
        bounded = min(page_size, MAX_RESULT_LIMIT)
        with Session(engine) as session:
            rows = session.execute(select(document).where(document.c.project_id == _uuid(project_id, "project_id")).order_by(document.c.updated_at.desc()).limit(bounded).offset((page - 1) * bounded)).mappings().all()
            total = _count(session, document, document.c.project_id == _uuid(project_id, "project_id"))
            items = []
            for item in rows:
                doc_id = item["id"]
                latest = session.execute(select(version).where(version.c.document_id == doc_id).order_by(version.c.created_at.desc()).limit(1)).mappings().first()
                items.append({
                    **_row(item),
                    "latest_version": _row(latest) if latest else None,
                    "pages": _count(session, page_table, page_table.c.document_id == doc_id),
                    "chunks": _count(session, chunk, chunk.c.document_id == doc_id),
                })
        return {"status": "ok", "documents": items, "pagination": {"page": page, "page_size": bounded, "total": total, "has_next": page * bounded < total}}
    except OperatorError:
        raise
    except Exception as exc:
        raise OperatorError("CORPUS_UNAVAILABLE", "Corpus database is unavailable", status_code=503) from exc
    finally:
        engine.dispose()


def get_document(document_id: str) -> dict[str, Any]:
    config = configuration()
    doc_uuid = _uuid(document_id, "document_id")
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        document = _table(engine, "corpus_documents")
        version = _table(engine, "corpus_document_versions")
        with Session(engine) as session:
            item = session.execute(select(document).where(document.c.id == doc_uuid)).mappings().first()
            if not item:
                raise OperatorError("DOCUMENT_NOT_FOUND", "document not found", status_code=404)
            versions = session.execute(select(version).where(version.c.document_id == doc_uuid).order_by(version.c.created_at.desc())).mappings().all()
        return {"status": "ok", "document": _row(item), "versions": [_row(row) for row in versions]}
    finally:
        engine.dispose()


def get_document_page(document_id: str, page_number: int) -> dict[str, Any]:
    config = configuration()
    document_uuid = _uuid(document_id, "document_id")
    if page_number < 1:
        raise OperatorError("INVALID_PAGE", "page number must be positive", status_code=422)
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        page = _table(engine, "corpus_pages")
        evidence = _table(engine, "corpus_evidence_spans")
        with Session(engine) as session:
            page_row = session.execute(select(page).where(page.c.document_id == document_uuid, page.c.page_number == page_number)).mappings().first()
            if not page_row:
                raise OperatorError("PAGE_NOT_FOUND", "page not found", status_code=404)
            spans = session.execute(select(evidence).where(evidence.c.page_id == page_row["id"]).order_by(evidence.c.char_start)).mappings().all()
        return {"status": "ok", "page": _row(page_row), "evidence": [_row(row) for row in spans]}
    finally:
        engine.dispose()


def search(project_id: str, query: str, *, limit: int = 25) -> dict[str, Any]:
    config = configuration()
    _operational_project(config, project_id)
    normalized = query.strip()
    if not normalized:
        raise OperatorError("QUERY_REQUIRED", "keyword query is required", status_code=422)
    bounded = min(max(limit, 1), MAX_RESULT_LIMIT)
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        chunk = _table(engine, "corpus_chunks")
        page = _table(engine, "corpus_pages")
        document = _table(engine, "corpus_documents")
        version = _table(engine, "corpus_document_versions")
        evidence = _table(engine, "corpus_evidence_spans")
        like = f"%{normalized[:200]}%"
        statement = select(
            document.c.id.label("document_id"),
            document.c.canonical_title,
            version.c.id.label("document_version_id"),
            version.c.source_path,
            page.c.page_number,
            chunk.c.id.label("chunk_id"),
            chunk.c.normalized_text,
            evidence.c.id.label("evidence_span_id"),
            evidence.c.quoted_text,
            evidence.c.char_start,
            evidence.c.char_end,
        ).select_from(
            evidence.join(chunk, evidence.c.chunk_id == chunk.c.id)
            .join(page, evidence.c.page_id == page.c.id)
            .join(document, evidence.c.document_id == document.c.id)
            .join(version, evidence.c.document_version_id == version.c.id)
        ).where(
            document.c.project_id == _uuid(project_id, "project_id"),
            chunk.c.embedding_status == "PUBLISHED",
            (chunk.c.normalized_text.ilike(like) | evidence.c.quoted_text.ilike(like)),
        ).order_by(document.c.id, version.c.id, page.c.page_number, chunk.c.chunk_order, evidence.c.char_start).limit(bounded)
        with Session(engine) as session:
            rows = session.execute(statement).mappings().all()
        results = []
        needle = normalized.casefold()
        for row in rows:
            text_value = str(row["normalized_text"] or "")
            excerpt = str(row["quoted_text"] or text_value)[:600]
            match_count = text_value.casefold().count(needle)
            results.append({
                "source_id": str(row["document_id"]),
                "source_type": "PDF",
                "origin": str(row["source_path"]),
                "retrieval_status": "PUBLISHED",
                "normalization_status": "NORMALIZED",
                "content_version": str(row["document_version_id"]),
                "selection_status": "SELECTED",
                "source_filename": Path(str(row["source_path"])).name,
                "source_path_relative": str(row["source_path"]),
                "page_number": int(row["page_number"]),
                "matched_excerpt": excerpt,
                "deterministic_score": float(match_count > 0),
                "match_count": match_count,
                "match_explanation": {"method": "casefold_substring", "query": normalized, "match_count": match_count},
                "document_id": str(row["document_id"]),
                "document_version_id": str(row["document_version_id"]),
                "chunk_id": str(row["chunk_id"]),
                "evidence_span_id": str(row["evidence_span_id"]),
                "evidence_id": str(row["evidence_span_id"]),
                "evidence_span": {
                    "text": excerpt,
                    "char_start": int(row["char_start"]),
                    "char_end": int(row["char_end"]),
                    "page_number": int(row["page_number"]),
                },
                "document_title": str(row["canonical_title"]),
                "search_mode": "keyword",
            })
        return {"status": "ok", "project_id": project_id, "query": normalized, "mode": "keyword", "retrieval_policy": KEYWORD_POLICY, "result_count": len(results), "results": results}
    finally:
        engine.dispose()


def get_evidence(evidence_span_id: str) -> dict[str, Any]:
    config = configuration()
    evidence_id = _uuid(evidence_span_id, "evidence_span_id")
    settings = corpus_settings(config)
    engine = get_engine(settings)
    try:
        evidence = _table(engine, "corpus_evidence_spans")
        page = _table(engine, "corpus_pages")
        document = _table(engine, "corpus_documents")
        version = _table(engine, "corpus_document_versions")
        statement = select(evidence, page.c.page_number, document.c.project_id, document.c.canonical_title, version.c.source_path).select_from(
            evidence.join(page, evidence.c.page_id == page.c.id).join(document, evidence.c.document_id == document.c.id).join(version, evidence.c.document_version_id == version.c.id)
        ).where(evidence.c.id == evidence_id)
        with Session(engine) as session:
            row = session.execute(statement).mappings().first()
        if not row:
            raise OperatorError("EVIDENCE_NOT_FOUND", "evidence span not found", status_code=404)
        return {
            "status": "ok",
            "evidence": {
                **_row(row),
                "source_id": str(row["document_id"]),
                "source_type": "PDF",
                "origin": str(row["source_path"]),
                "content_version": str(row["document_version_id"]),
                "selection_status": "SELECTED",
                "evidence_id": str(evidence_span_id),
                "source_path_relative": str(row["source_path"]),
                "anchor_identity": str(evidence_span_id),
            },
        }
    finally:
        engine.dispose()


def runtime_info() -> dict[str, Any]:
    config = configuration()
    return {
        "status": "ok",
        "runtime": masked_provenance(config),
        "operational_revision": config.operational_revision,
        "corpus_revision": config.corpus_revision,
    }
