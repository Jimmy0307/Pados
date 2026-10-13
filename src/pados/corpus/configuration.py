# SPDX-License-Identifier: AGPL-3.0-only
"""Portable, fail-closed configuration for the bounded P6.15 runtime."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from pados.config import (
    CORPUS_DATABASE_DEFAULT,
    CORPUS_REVISION_DEFAULT,
    EXCLUDED_REVISION_DEFAULT,
    OPERATIONAL_REVISION_DEFAULT,
    Settings,
)

from .security import CorpusBoundaryError, resolve_approved_root

BASE_REVISION = OPERATIONAL_REVISION_DEFAULT
ISOLATED_REVISION = CORPUS_REVISION_DEFAULT
OPERATIONAL_REVISION = OPERATIONAL_REVISION_DEFAULT
EXCLUDED_REVISION = EXCLUDED_REVISION_DEFAULT
DEFAULT_MAX_DOCUMENTS = 100
DEFAULT_POLICY = "KEYWORD_ONLY"


class CorpusConfigurationError(ValueError):
    """Raised when an unsafe or incomplete runtime configuration is supplied."""


@dataclass(frozen=True)
class CorpusConfiguration:
    repository_root: Path
    worktree: Path
    python_executable: Path
    database_host: str
    database_port: int
    database_name: str
    database_user: str
    database_password: str = field(repr=False)
    authorization_file: Path
    authorization: dict[str, Any] = field(repr=False)
    approved_corpus_root: Path
    project_id: str
    maximum_documents: int
    embedding_policy: str
    allow_ocr: bool
    allow_cloud_models: bool
    allow_source_modification: bool
    allow_operational_migration: bool
    operational_database: str
    runtime_artifact_root: Path
    timeout_seconds: int
    isolated_revision: str = CORPUS_REVISION_DEFAULT
    operational_revision: str = OPERATIONAL_REVISION_DEFAULT
    excluded_revision: str = EXCLUDED_REVISION_DEFAULT


def repository_root() -> Path:
    """Derive the current repository root from this installed source tree."""
    return (Path(__file__).resolve().parents[3]).resolve()


def _read_authorization(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise CorpusConfigurationError(f"authorization file not found: {path}") from exc
    except OSError as exc:
        raise CorpusConfigurationError(f"authorization file cannot be read: {type(exc).__name__}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorpusConfigurationError(f"authorization JSON is invalid: line {exc.lineno}") from exc
    if not isinstance(value, dict):
        raise CorpusConfigurationError("authorization JSON must be an object")
    return value


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CorpusConfigurationError(f"authorization field {key!r} is required")
    return value.strip()


def _database_name(payload: dict[str, Any], explicit: str | None, configured_default: str) -> str:
    candidate = (
        explicit
        or os.environ.get("PADOS_CORPUS_DATABASE_NAME")
        or payload.get("database")
        or payload.get("isolated_database")
        or configured_default
    )
    if not isinstance(candidate, str) or not candidate.strip():
        raise CorpusConfigurationError("database name must be explicitly configured")
    name = candidate.strip()
    if name == "pados_local_runtime":
        raise CorpusConfigurationError("operational database is not a valid corpus write target")
    return name


def _resolve_authorization_file(repository: Path, explicit: Path | None) -> Path:
    raw = explicit or (Path(os.environ["PADOS_CORPUS_AUTHORIZATION_FILE"]) if os.environ.get("PADOS_CORPUS_AUTHORIZATION_FILE") else None)
    if raw is None:
        raise CorpusConfigurationError("authorization file path must be explicitly configured")
    path = raw.expanduser().resolve()
    try:
        path.relative_to(repository)
    except ValueError:
        return path
    raise CorpusConfigurationError("authorization file must remain outside the Git worktree")


def load_configuration(
    *,
    authorization_file: Path | None = None,
    database_name: str | None = None,
    runtime_artifact_root: Path | None = None,
    python_executable: Path | None = None,
) -> CorpusConfiguration:
    """Resolve CLI overrides, environment, authorization, and safe defaults."""
    repository = repository_root()
    authorization_path = _resolve_authorization_file(repository, authorization_file)
    authorization = _read_authorization(authorization_path)
    if authorization.get("authorization_status") != "AUTHORIZED":
        raise CorpusConfigurationError("authorization_status must be AUTHORIZED")

    project_id = _required_string(authorization, "project_id")
    try:
        UUID(project_id)
    except ValueError as exc:
        raise CorpusConfigurationError("project_id must be a UUID") from exc
    approved_root_value = _required_string(authorization, "approved_corpus_root")
    try:
        approved_root = resolve_approved_root(approved_root_value)
    except CorpusBoundaryError as exc:
        raise CorpusConfigurationError(f"approved corpus root is invalid: {exc}") from exc

    settings = Settings()
    database = _database_name(authorization, database_name, settings.corpus_database_name or CORPUS_DATABASE_DEFAULT)
    operational_database = (
        authorization.get("operational_database")
        or os.environ.get("PADOS_CORPUS_OPERATIONAL_DATABASE")
        or settings.operational_database_name
    )
    if not isinstance(operational_database, str) or not operational_database.strip():
        raise CorpusConfigurationError("operational database must be explicitly identified")
    operational_database = operational_database.strip()
    if operational_database == database:
        raise CorpusConfigurationError("operational and corpus database targets must differ")

    maximum_documents = authorization.get("maximum_documents", DEFAULT_MAX_DOCUMENTS)
    if not isinstance(maximum_documents, int) or not 1 <= maximum_documents <= DEFAULT_MAX_DOCUMENTS:
        raise CorpusConfigurationError("maximum_documents must be an integer from 1 through 100")
    policy = authorization.get("embedding_policy")
    if policy != DEFAULT_POLICY:
        raise CorpusConfigurationError("embedding_policy must be KEYWORD_ONLY")
    for key in ("allow_ocr", "allow_cloud_models", "allow_source_modification", "allow_operational_migration"):
        if authorization.get(key) is not False:
            raise CorpusConfigurationError(f"{key} must be false for P6.15")

    host = os.environ.get("PADOS_CORPUS_DB_HOST", settings.db_host)
    port_raw = os.environ.get("PADOS_CORPUS_DB_PORT", str(settings.db_port))
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise CorpusConfigurationError("database port must be an integer") from exc
    artifact_root = runtime_artifact_root or (
        Path(os.environ["PADOS_CORPUS_RUNTIME_ARTIFACT_ROOT"])
        if os.environ.get("PADOS_CORPUS_RUNTIME_ARTIFACT_ROOT")
        else repository / "runtime" / "artifacts" / "p6_15_corpus_runtime"
    )
    timeout_raw = os.environ.get("PADOS_CORPUS_TIMEOUT_SECONDS", "3")
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError as exc:
        raise CorpusConfigurationError("timeout seconds must be an integer") from exc
    if not 1 <= timeout_seconds <= 60:
        raise CorpusConfigurationError("timeout seconds must be between 1 and 60")

    executable = (python_executable or Path(os.environ.get("PADOS_CORPUS_PYTHON", sys.executable))).expanduser().resolve()
    return CorpusConfiguration(
        repository_root=repository,
        worktree=repository,
        python_executable=executable,
        database_host=host,
        database_port=port,
        database_name=database,
        database_user=settings.db_user,
        database_password=settings.db_password,
        authorization_file=authorization_path,
        authorization=authorization,
        approved_corpus_root=approved_root,
        project_id=project_id,
        maximum_documents=maximum_documents,
        embedding_policy=policy,
        allow_ocr=False,
        allow_cloud_models=False,
        allow_source_modification=False,
        allow_operational_migration=False,
        operational_database=operational_database,
        runtime_artifact_root=artifact_root.expanduser().resolve(),
        timeout_seconds=timeout_seconds,
        isolated_revision=settings.corpus_revision,
        operational_revision=settings.operational_revision,
        excluded_revision=settings.excluded_revision,
    )
