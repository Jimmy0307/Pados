# SPDX-License-Identifier: AGPL-3.0-only
"""Execution receipts and artifact handoff checks.

A receipt binds one execution attempt to its contract and the artifacts it
produced, sealed by a content hash. Receipts are evidence only: this module
has no database or semantic mutation API, and a valid hash proves integrity,
not who produced it. Acceptance happens only through validation and the
semantic commit elsewhere.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TypeGuard

from pados.execution.context import canonical_hash, redact
from pados.execution.safe_fs import workspace_fs

MAX_ARTIFACTS = 8
MAX_HANDOFF_FILE_BYTES = 400_000
EVIDENCE_KINDS = frozenset({"DETERMINISTIC_HOST", "SIMULATION", "LIVE_PROVIDER"})
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _valid_path(path: Any) -> TypeGuard[str]:
    if not isinstance(path, str) or not path:
        return False
    if path.startswith(("/", "\\")) or "\\" in path or ":" in path:
        return False
    return ".." not in path.split("/")


def artifact_handoff(artifacts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate artifact metadata and return it normalized and sorted by path."""
    items = list(artifacts)
    if len(items) > MAX_ARTIFACTS:
        raise ValueError("ARTIFACT_BUDGET_EXCEEDED")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in items:
        path = item.get("path")
        if not _valid_path(path) or path in seen:
            raise ValueError("INVALID_ARTIFACT_PATH")
        seen.add(path)
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if (
            not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ValueError("INVALID_ARTIFACT_IDENTITY")
        if redact(path) != path:
            raise ValueError("SECRET_IN_ARTIFACT_METADATA")
        normalized.append({"path": path, "sha256": digest, "size_bytes": size})
    return sorted(normalized, key=lambda entry: entry["path"])


def execution_receipt(
    *,
    run_id: str,
    attempt: int,
    contract: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    provider_id: str,
    evidence_kind: str,
    ok: bool,
    execution_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a sealed receipt for one execution attempt."""
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError("EXECUTION_EVIDENCE_REQUIRED")
    if (
        not run_id
        or not provider_id
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or not 1 <= attempt <= 3
    ):
        raise ValueError("INVALID_EXECUTION_BINDING")
    payload: dict[str, Any] = {
        "schema_version": "1",
        "run_id": run_id,
        "attempt": attempt,
        "work_item_id": contract["work_contract_id"],
        "contract_hash": canonical_hash(contract),
        "provider_id": provider_id,
        "evidence_kind": evidence_kind,
        "ok": ok,
        "artifacts": artifact_handoff(artifacts),
        "semantic_authority": False,
    }
    if execution_binding is not None:
        payload["execution_binding"] = _validated_execution_binding(execution_binding)
    digest = canonical_hash(payload)
    return {**payload, "receipt_id": f"execution-sha256:{digest}", "content_hash": digest}


def verify_execution_receipt(
    receipt: Mapping[str, Any] | None,
    *,
    run_id: str,
    attempt: int,
    contract: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    execution_binding: Mapping[str, Any] | None = None,
) -> bool:
    """Return True only for an intact, successful, non-simulated receipt of this binding."""
    if receipt is None:
        return False
    try:
        expected = execution_receipt(
            run_id=run_id,
            attempt=attempt,
            contract=contract,
            artifacts=artifacts,
            provider_id=receipt["provider_id"],
            evidence_kind=receipt["evidence_kind"],
            ok=receipt["ok"],
            execution_binding=execution_binding,
        )
        return (
            dict(receipt) == expected
            and receipt["ok"] is True
            and receipt["evidence_kind"] != "SIMULATION"
        )
    except (KeyError, TypeError, ValueError):
        return False


def _validated_execution_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the P6.10 bridge payload to bounded identity and provenance metadata."""
    required = {
        "task_id",
        "goal_id",
        "plan_id",
        "step_id",
        "loop_id",
        "worker_id",
        "worker_type",
        "capability",
        "provider_resource",
        "project_ref",
        "classification",
        "scope_version_ref",
        "authorization_ref",
        "execution_status",
        "started_at",
        "completed_at",
        "provenance_hash",
        "protocol_version",
        "schema_version",
    }
    optional = {"residual_id"}
    if not required.issubset(binding) or set(binding) - required - optional:
        raise ValueError("INVALID_EXECUTION_BINDING_METADATA")
    normalized: dict[str, Any] = {}
    for key, value in binding.items():
        if value is None and key == "residual_id":
            normalized[key] = None
            continue
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("INVALID_EXECUTION_BINDING_METADATA")
        normalized[key] = value
    if normalized["classification"] not in {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"}:
        raise ValueError("INVALID_EXECUTION_BINDING_METADATA")
    if normalized["execution_status"] not in {"SUCCEEDED", "FAILED", "BLOCKED", "TIMED_OUT"}:
        raise ValueError("INVALID_EXECUTION_BINDING_METADATA")
    if normalized["schema_version"] != "pados.execution-receipt.v1":
        raise ValueError("INVALID_EXECUTION_BINDING_METADATA")
    for key in ("started_at", "completed_at"):
        try:
            timestamp = datetime.fromisoformat(normalized[key])
        except ValueError as exc:
            raise ValueError("INVALID_EXECUTION_BINDING_METADATA") from exc
        if timestamp.tzinfo is None:
            raise ValueError("INVALID_EXECUTION_BINDING_METADATA")
    if not _SHA256.fullmatch(normalized["provenance_hash"]):
        raise ValueError("INVALID_EXECUTION_BINDING_METADATA")
    return normalized


def verify_artifact_bytes(artifact: Mapping[str, Any], content: bytes) -> bool:
    """Check that ``content`` matches the artifact's declared digest and size."""
    return (
        artifact.get("sha256") == hashlib.sha256(content).hexdigest()
        and artifact.get("size_bytes") == len(content)
    )


def verify_handoff_files(
    root: str | Path,
    artifacts: Sequence[Mapping[str, Any]],
    expected_paths: Iterable[str],
) -> bool:
    """Re-read every declared artifact from the workspace and confirm its identity."""
    try:
        resolved = Path(root).resolve(strict=True)
        normalized = artifact_handoff(artifacts)
        if {item["path"] for item in normalized} != set(expected_paths):
            return False
        files = workspace_fs(resolved)
        for item in normalized:
            size = item["size_bytes"]
            if size > MAX_HANDOFF_FILE_BYTES:
                return False
            content = files.read_file(item["path"], max_bytes=size)
            if content is None or not verify_artifact_bytes(item, content):
                return False
        return True
    except Exception:  # noqa: BLE001 - any failure to prove the handoff is a refusal
        return False
