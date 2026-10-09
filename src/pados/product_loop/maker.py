# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pados.execution.handoff import task_envelope
from pados.execution.receipts import execution_receipt
from pados.execution.safe_fs import workspace_fs
from pados.product_loop.schemas import (
    FailureClass,
    RuntimeResult,
    RuntimeStatus,
    WorkContract,
)


class DirectMaker(Protocol):
    async def make(self, contract: WorkContract, *, run_id: str, attempt: int) -> RuntimeResult:
        """Execute one bounded implementation attempt."""


class FileDirectMaker:
    """Candidate Direct Maker for one approved isolated workspace."""

    def __init__(self, approved_root: Path, protected_roots: Sequence[Path] = ()) -> None:
        self.approved_root = approved_root.resolve()
        self.protected_roots = tuple(root.resolve() for root in protected_roots)
        self.approved_root.mkdir(parents=True, exist_ok=True)

    async def make(self, contract: WorkContract, *, run_id: str, attempt: int) -> RuntimeResult:
        started = _now()
        runtime_id = f"direct-maker-{uuid4()}"
        try:
            task_envelope(contract.model_dump(mode="json"))
            target_root = Path(contract.scope.workspace_root).resolve()
            if target_root != self.approved_root:
                raise PermissionError("WORKSPACE_ROOT_NOT_APPROVED")
            if not contract.scope.isolated:
                raise PermissionError("ISOLATED_WORKSPACE_REQUIRED")
            if "WRITE_APPROVED_WORKSPACE" not in contract.allowed_actions:
                raise PermissionError("WRITE_ACTION_NOT_AUTHORIZED")
            targets = [
                self._resolve_change(target_root, change.path, contract)
                for change in contract.changes
            ]
            changed: list[str] = []
            artifacts: list[dict[str, object]] = []
            fs = workspace_fs(target_root)
            for change, _target in zip(contract.changes, targets, strict=True):
                fs.write_file(change.path, change.content.encode("utf-8"))
                payload = fs.read_file(change.path)
                if payload is None:
                    raise PermissionError("DIRECT_MAKER_WRITE_NOT_VERIFIED")
                changed.append(change.path)
                artifacts.append(
                    {
                        "path": change.path,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                    }
                )
            completed = _now()
            return RuntimeResult(
                run_id=run_id,
                attempt=attempt,
                runtime_id=runtime_id,
                status=RuntimeStatus.COMPLETED,
                started_at=started,
                completed_at=completed,
                summary=f"Direct Maker wrote {len(changed)} approved path(s).",
                changed_paths=changed,
                artifact_refs=artifacts,
                execution_receipt=execution_receipt(
                    run_id=run_id, attempt=attempt,
                    contract=contract.model_dump(mode="json"), artifacts=artifacts,
                    provider_id="PADOS_FILE_DIRECT_MAKER",
                    evidence_kind="DETERMINISTIC_HOST", ok=True,
                ),
                validation_observations=[],
                retryable=False,
                completion_declared=True,
            )
        except PermissionError as exc:
            return RuntimeResult(
                run_id=run_id,
                attempt=attempt,
                runtime_id=runtime_id,
                status=RuntimeStatus.BLOCKED,
                started_at=started,
                completed_at=_now(),
                summary=str(exc),
                failure_classification=FailureClass.AUTHORITY_BLOCK,
                retryable=False,
                completion_declared=True,
            )
        except OSError as exc:
            return RuntimeResult(
                run_id=run_id,
                attempt=attempt,
                runtime_id=runtime_id,
                status=RuntimeStatus.FAILED,
                started_at=started,
                completed_at=_now(),
                summary=str(exc),
                failure_classification=FailureClass.RUNTIME_FAILURE,
                retryable=True,
                completion_declared=True,
            )

    def _resolve_change(self, target_root: Path, relative_path: str, contract: WorkContract) -> Path:
        candidate = (target_root / relative_path).resolve()
        if not candidate.is_relative_to(target_root):
            raise PermissionError("FILE_CHANGE_OUTSIDE_APPROVED_ROOT")
        protected = any(
            candidate == root or candidate.is_relative_to(root) for root in self.protected_roots
        )
        if protected:
            if "WRITE_FORMAL_REPO" in contract.prohibited_actions:
                raise PermissionError("FORMAL_REPOSITORY_WRITE_PROHIBITED")
            raise PermissionError("PROTECTED_ROOT_WRITE_BLOCKED")
        if relative_path not in contract.scope.allowed_paths:
            raise PermissionError("FILE_CHANGE_NOT_IN_ALLOWLIST")
        return candidate


def _now() -> str:
    return datetime.now(UTC).isoformat()
