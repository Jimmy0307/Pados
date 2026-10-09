# SPDX-License-Identifier: AGPL-3.0-only
"""Shared, fail-closed configuration for the P6.15 operator application."""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from pathlib import Path

from pados.config import (
    CORPUS_REVISION_DEFAULT,
    EXCLUDED_REVISION_DEFAULT,
    OPERATIONAL_REVISION_DEFAULT,
    Settings,
    get_settings,
)

KEYWORD_POLICY = "KEYWORD_ONLY"
MAX_DOCUMENTS = 100
DEFAULT_TIMEOUT_SECONDS = 3
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024


class OperatorConfigurationError(ValueError):
    """Raised when the operator boundary cannot be resolved safely."""


@dataclass(frozen=True)
class OperatorConfiguration:
    repository_root: Path
    operational_database: str
    corpus_database: str
    database_host: str
    database_port: int
    database_user: str
    database_password: str = field(repr=False)
    artifact_root: Path
    staging_root: Path
    operator_name: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_documents: int = MAX_DOCUMENTS
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    embedding_policy: str = KEYWORD_POLICY
    allow_ocr: bool = False
    allow_cloud_models: bool = False
    allow_source_modification: bool = False
    allow_operational_migration: bool = False
    operational_revision: str = OPERATIONAL_REVISION_DEFAULT
    corpus_revision: str = CORPUS_REVISION_DEFAULT
    excluded_revision: str = EXCLUDED_REVISION_DEFAULT


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _configured_path(value: str | None, default: Path, repository_root: Path) -> Path:
    candidate = Path(value).expanduser() if value else default
    if not candidate.is_absolute():
        candidate = repository_root / candidate
    return candidate.resolve()


def resolve_operator_configuration(settings: Settings | None = None) -> OperatorConfiguration:
    """Resolve one configuration used by API, launcher, and operator tests."""

    active = settings or get_settings()
    repository_root = _repository_root()
    corpus_database = (os.environ.get("PADOS_CORPUS_DATABASE_NAME") or active.corpus_database_name).strip()
    operational_database = (
        os.environ.get("PADOS_CORPUS_OPERATIONAL_DATABASE") or active.operational_database_name
    ).strip()
    if not operational_database or not corpus_database or operational_database == corpus_database:
        raise OperatorConfigurationError("operational and Corpus database targets must be distinct")

    host = (os.environ.get("PADOS_CORPUS_DB_HOST") or active.db_host).strip()
    port_raw = os.environ.get("PADOS_CORPUS_DB_PORT") or str(active.db_port)
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise OperatorConfigurationError("Corpus database port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise OperatorConfigurationError("Corpus database port is out of range")

    timeout_raw = os.environ.get("PADOS_CORPUS_TIMEOUT_SECONDS") or str(DEFAULT_TIMEOUT_SECONDS)
    max_file_raw = os.environ.get("PADOS_CORPUS_MAX_FILE_BYTES") or str(DEFAULT_MAX_FILE_BYTES)
    try:
        timeout_seconds = int(timeout_raw)
        max_file_bytes = int(max_file_raw)
    except ValueError as exc:
        raise OperatorConfigurationError("Corpus timeout and file ceiling must be integers") from exc
    if not 1 <= timeout_seconds <= 60:
        raise OperatorConfigurationError("Corpus timeout must be between 1 and 60 seconds")
    if not 1 <= max_file_bytes <= 500 * 1024 * 1024:
        raise OperatorConfigurationError("Corpus file ceiling is out of range")

    maximum_documents_raw = os.environ.get("PADOS_CORPUS_MAX_DOCUMENTS") or str(MAX_DOCUMENTS)
    try:
        maximum_documents = int(maximum_documents_raw)
    except ValueError as exc:
        raise OperatorConfigurationError("Corpus document ceiling must be an integer") from exc
    if not 1 <= maximum_documents <= MAX_DOCUMENTS:
        raise OperatorConfigurationError("Corpus document ceiling must be between 1 and 100")

    artifact_root = _configured_path(
        os.environ.get("PADOS_CORPUS_RUNTIME_ARTIFACT_ROOT"),
        repository_root / "runtime" / "artifacts" / "p6_15_operator",
        repository_root,
    )
    staging_root = _configured_path(
        os.environ.get("PADOS_CORPUS_APPROVED_STAGING_ROOT"),
        artifact_root / "staging",
        repository_root,
    )
    operator_name = (os.environ.get("PADOS_CORPUS_OPERATOR_NAME") or getpass.getuser()).strip()
    if not operator_name:
        raise OperatorConfigurationError("local operator identity is unavailable")

    return OperatorConfiguration(
        repository_root=repository_root,
        operational_database=operational_database,
        corpus_database=corpus_database,
        database_host=host,
        database_port=port,
        database_user=active.db_user,
        database_password=active.db_password,
        artifact_root=artifact_root,
        staging_root=staging_root,
        operator_name=operator_name,
        timeout_seconds=timeout_seconds,
        max_documents=maximum_documents,
        operational_revision=active.operational_revision,
        corpus_revision=active.corpus_revision,
        excluded_revision=active.excluded_revision,
    )


def corpus_settings(config: OperatorConfiguration, base: Settings | None = None) -> Settings:
    """Return backend-only settings bound to the dedicated Corpus database."""

    active = base or get_settings()
    return active.model_copy(
        update={
            "db_host": config.database_host,
            "db_port": config.database_port,
            "db_name": config.corpus_database,
            "db_user": config.database_user,
            "db_password": config.database_password,
        }
    )


def operational_settings(config: OperatorConfiguration, base: Settings | None = None) -> Settings:
    """Return backend-only settings bound to the operational database."""

    active = base or get_settings()
    return active.model_copy(
        update={
            "db_host": config.database_host,
            "db_port": config.database_port,
            "db_name": config.operational_database,
            "db_user": config.database_user,
            "db_password": config.database_password,
        }
    )


def masked_provenance(config: OperatorConfiguration) -> dict[str, object]:
    """Return safe runtime provenance; never include credentials or auth files."""

    return {
        "operational_database": config.operational_database,
        "corpus_database": config.corpus_database,
        "database_host": config.database_host,
        "database_port": config.database_port,
        "artifact_root": str(config.artifact_root),
        "staging_root": str(config.staging_root),
        "timeout_seconds": config.timeout_seconds,
        "maximum_documents": config.max_documents,
        "embedding_policy": config.embedding_policy,
        "ocr": "DISABLED",
        "cloud_models": "DISABLED",
        "source_modification": "DISABLED",
        "operational_migration": "DISABLED",
    }
