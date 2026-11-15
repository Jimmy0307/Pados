# SPDX-License-Identifier: AGPL-3.0-only
"""Create a project from the Control Center (``POST /control/projects``) through the CLI's own setup path.

The person names the project (and may describe it); they never type a path or an id. The folder is derived from the
name, ``<ascii-slug>-<8 hex of the name>``, as a direct child of the approved workspace root
(``PADOS_PRODUCT_WORKSPACE_ROOT``, written by ``pados init``); nothing outside that root is ever created or registered.
The rows are written by ``memory_service.bootstrap_project`` (the function ``pados project create`` calls, with the same
validation and duplicate checks), the classification defaults to the CLI's ``internal``, and the proof scope is then
initialized by ``project_setup.ensure_local_proof_scope`` (the CLI's function), outside the operational transaction.

Idempotency (no new table): the ``PROJECT_CREATED`` sync event of the creation records the idempotency key and the
request fingerprint. The same key with the same request returns the same project (and re-runs the idempotent proof-scope
initialization, which is also the recovery path after an interrupted setup); the same key with another request is
``PROJECT_IDEMPOTENCY_CONTENT_CONFLICT``. Creation is serialized by a transaction-scoped advisory lock.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.control.product_errors import ProductControlError
from pados.control.project_documents import atomic_write, encode_content
from pados.memory import service as memory_service
from pados.memory.security import contains_secret

PROJECT_NAME_MAX = 120
PROJECT_DESCRIPTION_MAX = 2000
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
DEFAULT_CLASSIFICATION = "internal"  # pados project create's default
CREATED_EVENT = "PROJECT_CREATED"
ACTOR = "pados-control-center"
_LOCK_KEY = int.from_bytes(hashlib.sha256(b"pados.control.project-create").digest()[:8], "big", signed=True)


class ProjectCreate(BaseModel):
    """Lengths are checked by the service so a refusal carries a closed code, not a schema error."""

    name: str = Field(max_length=1000)
    description: str = Field(default="", max_length=20_000)
    classification: str = Field(default=DEFAULT_CLASSIFICATION, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=120)


def _has_control_characters(value: str, *, allow_newlines: bool) -> bool:
    allowed = {"\n", "\t", "\r"} if allow_newlines else set()
    return any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} and ch not in allowed for ch in value)


def validate_request(payload: ProjectCreate) -> tuple[str, str, str]:
    name = payload.name.strip()
    if not name or len(name) > PROJECT_NAME_MAX or _has_control_characters(name, allow_newlines=False):
        raise ProductControlError("PROJECT_NAME_INVALID", 422)
    description = payload.description.strip()
    if len(description) > PROJECT_DESCRIPTION_MAX or _has_control_characters(description, allow_newlines=True):
        raise ProductControlError("PROJECT_DESCRIPTION_INVALID", 422)
    classification = payload.classification.strip().lower()
    if classification not in CLASSIFICATIONS:
        raise ProductControlError("PROJECT_CLASSIFICATION_INVALID", 422)
    if contains_secret(name) or contains_secret(description):
        raise ProductControlError("PROJECT_CONTENT_REJECTED", 422)
    return name, description, classification


def project_folder_name(name: str) -> str:
    """A safe folder name derived from the project name only: ASCII slug plus 8 hex of the case-folded name."""
    ascii_part = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_part).strip("-")[:40].strip("-") or "project"
    return f"{slug}-{hashlib.sha256(name.casefold().encode('utf-8')).hexdigest()[:8]}"


def approved_workspace_root(settings: Any) -> Path:
    configured = str(getattr(settings, "product_workspace_root", "") or "")
    if not configured:
        raise ProductControlError("PROJECT_WORKSPACE_ROOT_NOT_CONFIGURED", 503)
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise ProductControlError("PROJECT_WORKSPACE_ROOT_INVALID", 503)
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve(strict=True)
    except OSError as exc:
        raise ProductControlError("PROJECT_WORKSPACE_ROOT_INVALID", 503) from exc


def project_folder(workspace: Path, name: str) -> Path:
    """The project's folder: a direct child of the workspace root, never a link, never anywhere else."""
    folder = workspace / project_folder_name(name)
    if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
        raise ProductControlError("PROJECT_FOLDER_OUTSIDE_WORKSPACE", 403)
    if folder.resolve().parent != workspace:
        raise ProductControlError("PROJECT_FOLDER_OUTSIDE_WORKSPACE", 403)
    return folder


def fingerprint(name: str, description: str, classification: str) -> str:
    material = json.dumps({"name": name, "description": description, "classification": classification},
                          sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _prior_creation(session: Session, idempotency_key: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT project_id::text AS project_id, payload_json FROM control_sync_events
            WHERE event_type = :event_type AND payload_json->>'idempotency_key' = :key
            ORDER BY sequence_number ASC LIMIT 1
            """
        ),
        {"event_type": CREATED_EVENT, "key": idempotency_key},
    ).mappings().first()
    return None if row is None else dict(row)


def _readme(name: str, description: str) -> bytes:
    body = f"# {name}\n\n{description}\n" if description else f"# {name}\n"
    return encode_content(body)


def create_project(session: Session, settings: Any, payload: ProjectCreate) -> dict[str, Any]:
    """Create (or replay) the project rows and folder in the caller's transaction; returns ``project_id`` and
    ``replayed``. The caller initializes the proof scope after commit (``project_setup.ensure_local_proof_scope``)."""
    name, description, classification = validate_request(payload)
    request_hash = fingerprint(name, description, classification)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
    prior = _prior_creation(session, payload.idempotency_key)
    if prior is not None:
        if (prior["payload_json"] or {}).get("request_fingerprint") != request_hash:
            raise ProductControlError("PROJECT_IDEMPOTENCY_CONTENT_CONFLICT", 409)
        return {"project_id": str(prior["project_id"]), "replayed": True}
    taken = session.execute(text("SELECT 1 FROM projects WHERE lower(name) = lower(:name) LIMIT 1"),
                            {"name": name}).first()
    if taken is not None:
        raise ProductControlError("PROJECT_NAME_TAKEN", 409)
    workspace = approved_workspace_root(settings)
    folder = project_folder(workspace, name)
    # An empty folder, or one holding only the README this path writes (a setup whose commit failed), is reused.
    if folder.exists() and (not folder.is_dir() or any(p.name != "README.md" for p in folder.iterdir())):
        raise ProductControlError("PROJECT_FOLDER_EXISTS", 409)
    readme = _readme(name, description)
    folder.mkdir(exist_ok=True)
    try:
        project_id = memory_service.bootstrap_project(session, name=name, root_path=folder,
                                                      classification=classification, actor=ACTOR)
    except ValueError as exc:  # bootstrap_project's own duplicate checks, mapped to closed codes
        message = str(exc)
        code = "PROJECT_FOLDER_EXISTS" if "canonical root" in message else "PROJECT_NAME_TAKEN"
        raise ProductControlError(code, 409) from exc
    if description:
        session.execute(
            text("UPDATE projects SET configuration = configuration || CAST(:extra AS jsonb) "
                 "WHERE id = CAST(:id AS uuid)"),
            {"id": project_id, "extra": json.dumps({"description": description}, ensure_ascii=False)},
        )
    control_service.append_sync_event(session, project_id, CREATED_EVENT, {
        "project_id": project_id, "idempotency_key": payload.idempotency_key, "request_fingerprint": request_hash,
        "folder": folder.name, "classification": classification, "created_by": ACTOR})
    atomic_write(folder / "README.md", readme)  # last: a refusal above leaves at most an empty folder
    return {"project_id": project_id, "replayed": False}


def project_view(session: Session, project_id: str) -> dict[str, Any]:
    """The created project as the Control Center shows it (no absolute path needed by the page)."""
    row = dict(control_service.get_project(session, project_id))
    configuration = row.get("configuration") or {}
    root = Path(str(row.get("root_path") or ""))
    return {"id": str(row["id"]), "name": row["name"], "status": row["status"], "folder": root.name,
            "description": configuration.get("description", "") if isinstance(configuration, dict) else "",
            "updated_at": row.get("updated_at")}
