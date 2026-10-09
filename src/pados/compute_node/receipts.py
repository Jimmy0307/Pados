# SPDX-License-Identifier: AGPL-3.0-only
"""Node execution receipts and the PADOS-side receipt ledger.

The Core ExecutionReceipt (``pados.execution.receipts``) stays unchanged and
authoritative for validation. A NodeExecutionReceipt adds node/model/job
metadata, is bound to the Core receipt id, and carries no prompt or content,
only hashes, identities and SecretRef references.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

NODE_RECEIPT_SCHEMA = "pados.compute-node.execution-receipt.v1"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def node_execution_receipt(*, record: dict[str, Any], provider_id: str, credential_ref: str,
                           execution_receipt_id: str | None, verification: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema": NODE_RECEIPT_SCHEMA,
        "node_id": record["node_id"], "provider_id": provider_id, "model_id": record["model_id"],
        "capability": record["capability"], "work_package_id": record["work_package_id"],
        "run_id": record["run_id"], "job_id": record["job_id"], "request_id": record["request_id"],
        "started_at": record["started_at"], "finished_at": record["finished_at"], "status": record["state"],
        "classification": record["classification"], "input_sha256": record["input_sha256"],
        "input_items": record["input_items"],
        "artifacts": [{k: a[k] for k in ("path", "sha256", "size_bytes", "media_type")} for a in record["artifacts"]],
        "runtime": {**record.get("runtime", {}), "node_runtime_version": record.get("node_runtime_version"),
                    "queue_delay_ms": record.get("queue_delay_ms"), "inference_ms": record.get("inference_ms"),
                    "manifest_hash": record.get("manifest_hash")},
        "execution": record.get("execution"),  # the measured execution configuration the node ran under
        "credential_ref": credential_ref,
        "execution_receipt_id": execution_receipt_id,
        "verification": verification,
        "semantic_authority": False,
    }
    return {**body, "receipt_hash": canonical_hash(body)}


def verify_node_receipt(receipt: dict[str, Any]) -> bool:
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    return receipt.get("schema") == NODE_RECEIPT_SCHEMA and receipt.get("receipt_hash") == canonical_hash(body) \
        and receipt.get("semantic_authority") is False


class ReceiptLedger:
    """Content-addressed execution evidence (not an audit authority)."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "receipts"

    def append(self, receipt: dict[str, Any]) -> Path:
        if not verify_node_receipt(receipt):
            raise ValueError("NODE_RECEIPT_INVALID")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{receipt['receipt_hash']}.json"
        if not path.exists():
            path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        latest = self.root / f"latest-{receipt['node_id']}.json"
        latest.write_text(json.dumps({"receipt_hash": receipt["receipt_hash"], "finished_at": receipt["finished_at"]}),
                          encoding="utf-8")
        return path

    def latest(self, node_id: str) -> dict[str, Any] | None:
        pointer = self.root / f"latest-{node_id}.json"
        if not pointer.is_file():
            return None
        ref = json.loads(pointer.read_text(encoding="utf-8"))
        path = self.root / f"{ref['receipt_hash']}.json"
        if not path.is_file():
            return None
        receipt = json.loads(path.read_text(encoding="utf-8"))
        return receipt if isinstance(receipt, dict) and verify_node_receipt(receipt) else None
