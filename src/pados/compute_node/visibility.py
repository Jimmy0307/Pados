# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only Mission Control projection of registered compute nodes.

Uses persisted probe evidence and the receipt ledger; it never probes, routes
or writes, and it has no fleet-management functions. A probe whose time cannot be
trusted (missing, unparseable, naive or in the future) is reported stale, never live.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from pados.compute_node.receipts import ReceiptLedger
from pados.compute_node.registry import NodeRegistry, load_probe


def compute_node_visibility(config_path: str) -> dict[str, Any]:
    if not config_path:
        return {"status": "COMPUTE_NODES_NOT_CONFIGURED", "nodes": [], "projection_only": True}
    try:
        registry = NodeRegistry.load(config_path)
    except (OSError, ValueError, ValidationError) as exc:
        return {"status": "COMPUTE_NODES_INVALID", "reason": type(exc).__name__, "nodes": [], "projection_only": True}
    ledger = ReceiptLedger(registry.state_dir)
    nodes = []
    for registration in registry.nodes:
        probe = load_probe(registry.state_dir, registration.node_id) or {}
        descriptor = probe.get("descriptor") or {}
        stale = _age(probe.get("probed_at")) > registry.probe_max_age_seconds
        last = ledger.latest(registration.node_id)
        nodes.append({
            "node_id": registration.node_id, "node_name": registration.node_name or descriptor.get("node_name"),
            "state": probe.get("state", "UNKNOWN"), "stale": stale, "authorized": registration.authorized,
            "platform": descriptor.get("platform"), "architecture": descriptor.get("architecture"),
            "capabilities": descriptor.get("capabilities", []),
            "allowed_capabilities": [c.value for c in registration.allowed_capabilities],
            "models": [{"model_id": m["model_id"], "availability": m["availability"]} for m in descriptor.get("models", [])],
            "current_load": descriptor.get("current_load"),
            "last_successful_check": probe.get("last_successful_check"), "last_probe": probe.get("probed_at"),
            "last_execution": None if last is None else {
                "run_id": last["run_id"], "job_id": last["job_id"], "model_id": last["model_id"],
                "status": last["status"], "finished_at": last["finished_at"], "receipt_hash": last["receipt_hash"]},
            "execution_class": "LOCAL",
            "reason": ", ".join(probe.get("reasons", [])[:3]) or ("PROBE_STALE" if stale else None),
        })
    return {"status": "COMPUTE_NODES_CONFIGURED", "nodes": nodes, "projection_only": True,
            "semantic_authority": "NONE"}


# Tolerated forward clock skew between the probe writer and this reader.
MAX_CLOCK_SKEW_SECONDS = 60.0


def _age(timestamp: object) -> float:
    if not isinstance(timestamp, str):
        return float("inf")
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds()
    except (ValueError, TypeError):  # TypeError: naive timestamp
        return float("inf")
    return float("inf") if age < -MAX_CLOCK_SKEW_SECONDS else age
