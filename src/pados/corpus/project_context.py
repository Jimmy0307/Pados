# SPDX-License-Identifier: AGPL-3.0-only
"""Project context projection between the operational and Corpus databases.

The operational database remains authoritative.  This module deliberately
closes the read-only operational transaction before opening the transactional
Corpus write, because PostgreSQL cannot provide a cross-database transaction.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

PROJECTION_CONTRACT = "P6_15_PROJECT_CONTEXT_PROJECTION"
PROJECTION_CONFIGURATION_KEY = "_p6_15_project_context_projection"
PROJECT_CONTEXT_PROJECTION_PRESENT = "PROJECT_CONTEXT_PROJECTION_PRESENT"
PROJECT_CONTEXT_PROJECTION_ABSENT_CREATABLE = "PROJECT_CONTEXT_PROJECTION_ABSENT_CREATABLE"
PROJECT_CONTEXT_PROJECTION_STALE_REFRESHABLE = "PROJECT_CONTEXT_PROJECTION_STALE_REFRESHABLE"
PROJECT_CONTEXT_OPERATIONAL_PROJECT_MISSING = "PROJECT_CONTEXT_OPERATIONAL_PROJECT_MISSING"
PROJECT_CONTEXT_CONTRACT_INVALID = "PROJECT_CONTEXT_CONTRACT_INVALID"

ConnectionFactory = Callable[[str], psycopg.Connection[Any]]


class ProjectContextProjectionBlocked(RuntimeError):
    """A fail-closed projection refusal with a stable contract reason."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


def _project_uuid(project_id: str) -> UUID:
    try:
        return UUID(str(project_id))
    except (TypeError, ValueError) as exc:
        raise ProjectContextProjectionBlocked(
            "BLOCKED_PROJECT_CONTEXT_INCOMPLETE", "project_id must be a UUID"
        ) from exc


def _iso_timestamp(value: object, *, field_name: str) -> str:
    if not isinstance(value, datetime):
        raise ProjectContextProjectionBlocked(
            "BLOCKED_PROJECT_CONTEXT_INCOMPLETE", f"{field_name} is missing"
        )
    timestamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()


def _configuration(value: object, *, allow_missing: bool = False) -> dict[str, Any]:
    if value is None and allow_missing:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProjectContextProjectionBlocked(
                "BLOCKED_PROJECT_CONTEXT_INCOMPLETE", "local project configuration is invalid"
            ) from exc
        if isinstance(parsed, dict):
            return dict(parsed)
    raise ProjectContextProjectionBlocked(
        "BLOCKED_PROJECT_CONTEXT_INCOMPLETE", "local project configuration must be an object"
    )


def _database_identity(connection: psycopg.Connection[Any], expected: str) -> None:
    row = connection.execute("SELECT current_database()").fetchone()
    if row is None or str(row[0]) != expected:
        observed = "<missing>" if row is None else str(row[0])
        raise ProjectContextProjectionBlocked(
            "BLOCKED_DATABASE_IDENTITY_INVALID", f"expected={expected}; observed={observed}"
        )


def _authoritative_project(
    connection: psycopg.Connection[Any], *, project_id: UUID, operational_database: str
) -> dict[str, Any] | None:
    """Read one operational project inside an explicitly read-only transaction."""

    with connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        _database_identity(connection, operational_database)
        row = connection.execute(
            """
            SELECT id, name, root_path, default_branch, status, classification,
                   created_at, updated_at
            FROM projects
            WHERE id = %s
            """,
            (project_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "name": row[1],
        "root_path": row[2],
        "default_branch": row[3],
        "status": row[4],
        "classification": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def _local_project(connection: psycopg.Connection[Any], *, project_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, name, root_path, default_branch, status, classification,
               configuration, created_at, updated_at
        FROM projects
        WHERE id = %s
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "name": row[1],
        "root_path": row[2],
        "default_branch": row[3],
        "status": row[4],
        "classification": row[5],
        "configuration": row[6],
        "created_at": row[7],
        "updated_at": row[8],
    }


def _validate_authoritative(project: dict[str, Any], *, project_id: UUID) -> None:
    if project.get("id") != str(project_id):
        raise ProjectContextProjectionBlocked(
            "BLOCKED_PROJECT_CONTEXT_INCOMPLETE", "operational project UUID changed during lookup"
        )
    for field_name in ("name", "root_path", "default_branch", "status", "classification"):
        value = project.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ProjectContextProjectionBlocked(
                "BLOCKED_PROJECT_CONTEXT_INCOMPLETE", f"operational project field {field_name} is empty"
            )
    _iso_timestamp(project.get("created_at"), field_name="operational project created_at")
    _iso_timestamp(project.get("updated_at"), field_name="operational project updated_at")


def _projection_metadata(
    project: dict[str, Any], *, operational_database: str, operational_revision: str, synchronized_at: datetime
) -> dict[str, str]:
    source_timestamp = _iso_timestamp(project["updated_at"], field_name="operational project updated_at")
    return {
        "contract": PROJECTION_CONTRACT,
        "source_database": operational_database,
        "source_project_id": str(project["id"]),
        "source_record_timestamp": source_timestamp,
        "source_record_version": source_timestamp,
        "source_revision": operational_revision,
        "synchronized_at": synchronized_at.astimezone(UTC).isoformat(),
    }


def _projection_matches(
    local: dict[str, Any], *, project: dict[str, Any], metadata: dict[str, str]
) -> bool:
    for field_name in ("name", "root_path", "default_branch", "status", "classification"):
        if local.get(field_name) != project.get(field_name):
            return False
    local_configuration = _configuration(local.get("configuration"))
    local_metadata = local_configuration.get(PROJECTION_CONFIGURATION_KEY)
    if not isinstance(local_metadata, dict):
        return False
    # Synchronization time is expected to differ on a later refresh; source
    # identity, source version, and source timestamp define freshness.
    return all(
        local_metadata.get(key) == metadata.get(key)
        for key in (
            "contract",
            "source_database",
            "source_project_id",
            "source_record_timestamp",
            "source_record_version",
            "source_revision",
        )
    )


def _result(
    *,
    status: str,
    project_id: UUID,
    operational_database: str,
    corpus_database: str,
    project: dict[str, Any] | None,
    local: dict[str, Any] | None,
    projection_eligible: bool,
) -> dict[str, Any]:
    return {
        "status": status,
        "project_context_projection": status,
        "project_id": str(project_id),
        "operational_database": operational_database,
        "corpus_database": corpus_database,
        "operational_project_exists": project is not None,
        "local_projection_exists": local is not None,
        "projection_eligible": projection_eligible,
        "source_record_timestamp": (
            _iso_timestamp(project["updated_at"], field_name="operational project updated_at")
            if project is not None
            else None
        ),
    }


def _validate_database_boundary(operational_database: str, corpus_database: str) -> None:
    if not operational_database.strip() or not corpus_database.strip() or operational_database == corpus_database:
        raise ProjectContextProjectionBlocked(
            "BLOCKED_DATABASE_IDENTITY_INVALID",
            "operational and Corpus database identities must be distinct and non-empty",
        )


def inspect_project_context_projection(
    *,
    project_id: str,
    operational_database: str,
    corpus_database: str,
    operational_revision: str,
    connect: ConnectionFactory,
) -> dict[str, Any]:
    """Read-only doctor inspection of the operational project and local projection."""

    _validate_database_boundary(operational_database, corpus_database)
    project_uuid = _project_uuid(project_id)
    with connect(operational_database) as operational_connection:
        project = _authoritative_project(
            operational_connection, project_id=project_uuid, operational_database=operational_database
        )
    with connect(corpus_database) as corpus_connection, corpus_connection.transaction():
        corpus_connection.execute("SET TRANSACTION READ ONLY")
        _database_identity(corpus_connection, corpus_database)
        local = _local_project(corpus_connection, project_id=project_uuid)

    if project is None:
        return _result(
            status=PROJECT_CONTEXT_OPERATIONAL_PROJECT_MISSING,
            project_id=project_uuid,
            operational_database=operational_database,
            corpus_database=corpus_database,
            project=None,
            local=local,
            projection_eligible=False,
        )
    try:
        _validate_authoritative(project, project_id=project_uuid)
        metadata = _projection_metadata(
            project,
            operational_database=operational_database,
            operational_revision=operational_revision,
            synchronized_at=datetime.now(UTC),
        )
        matches = local is not None and _projection_matches(local, project=project, metadata=metadata)
    except ProjectContextProjectionBlocked:
        return _result(
            status=PROJECT_CONTEXT_CONTRACT_INVALID,
            project_id=project_uuid,
            operational_database=operational_database,
            corpus_database=corpus_database,
            project=project,
            local=local,
            projection_eligible=False,
        )
    return _result(
        status=(
            PROJECT_CONTEXT_PROJECTION_PRESENT
            if matches
            else PROJECT_CONTEXT_PROJECTION_STALE_REFRESHABLE
            if local is not None
            else PROJECT_CONTEXT_PROJECTION_ABSENT_CREATABLE
        ),
        project_id=project_uuid,
        operational_database=operational_database,
        corpus_database=corpus_database,
        project=project,
        local=local,
        projection_eligible=True,
    )


def ensure_project_context_projection(
    *,
    project_id: str,
    operational_database: str,
    corpus_database: str,
    operational_revision: str,
    connect: ConnectionFactory,
) -> dict[str, Any]:
    """Materialize or refresh the exact operational project in the Corpus DB."""

    _validate_database_boundary(operational_database, corpus_database)
    project_uuid = _project_uuid(project_id)
    with connect(operational_database) as operational_connection:
        project = _authoritative_project(
            operational_connection, project_id=project_uuid, operational_database=operational_database
        )
    if project is None:
        raise ProjectContextProjectionBlocked(
            "BLOCKED_OPERATIONAL_PROJECT_NOT_FOUND", str(project_uuid)
        )
    _validate_authoritative(project, project_id=project_uuid)

    synchronized_at = datetime.now(UTC)
    metadata = _projection_metadata(
        project,
        operational_database=operational_database,
        operational_revision=operational_revision,
        synchronized_at=synchronized_at,
    )
    with connect(corpus_database) as corpus_connection, corpus_connection.transaction():
        _database_identity(corpus_connection, corpus_database)
        local = _local_project(corpus_connection, project_id=project_uuid)
        if local is None:
            configuration = {PROJECTION_CONFIGURATION_KEY: metadata}
            corpus_connection.execute(
                """
                    INSERT INTO projects
                      (id, name, root_path, default_branch, status, classification,
                       configuration, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                (
                    project_uuid,
                    project["name"],
                    project["root_path"],
                    project["default_branch"],
                    project["status"],
                    project["classification"],
                    Jsonb(configuration),
                    project["created_at"],
                    synchronized_at,
                ),
            )
            status = "CREATED"
        elif _projection_matches(local, project=project, metadata=metadata):
            status = "UNCHANGED"
        else:
            configuration = _configuration(local.get("configuration"))
            configuration[PROJECTION_CONFIGURATION_KEY] = metadata
            corpus_connection.execute(
                """
                    UPDATE projects
                    SET name = %s,
                        root_path = %s,
                        default_branch = %s,
                        status = %s,
                        classification = %s,
                        configuration = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                (
                    project["name"],
                    project["root_path"],
                    project["default_branch"],
                    project["status"],
                    project["classification"],
                    Jsonb(configuration),
                    synchronized_at,
                    project_uuid,
                ),
            )
            status = "REFRESHED"

        verified = corpus_connection.execute(
            "SELECT id FROM projects WHERE id = %s", (project_uuid,)
        ).fetchone()
        if verified is None or str(verified[0]) != str(project_uuid):
            raise ProjectContextProjectionBlocked(
                "BLOCKED_PROJECT_CONTEXT_INCOMPLETE",
                "Corpus project projection is not available for local foreign keys",
            )

    return {
        "status": status,
        "project_context_projection": status,
        "project_id": str(project_uuid),
        "operational_database": operational_database,
        "corpus_database": corpus_database,
        "operational_project_exists": True,
        "local_projection_exists": True,
        "projection_eligible": True,
        "source_record_timestamp": _iso_timestamp(
            project["updated_at"], field_name="operational project updated_at"
        ),
        "projection_synchronized_at": synchronized_at.isoformat(),
        "foreign_key_target_verified": True,
    }
