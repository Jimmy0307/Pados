# SPDX-License-Identifier: AGPL-3.0-only
"""Optional compute-node checks for the existing PADOS doctor.

``PADOS_COMPUTE_NODES_CONFIG`` points to the PADOS-side node registry. Unset
means no nodes are configured, which never affects Core readiness.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from pados.compute_node.manifest import ModelAvailability, NodeDescriptor, NodeState
from pados.compute_node.provider import ROUTER_CAPABILITY
from pados.compute_node.registry import HttpFactory, NodeProber, NodeRegistry
from pados.enterprise.secrets import SecretResolver


def compute_nodes_status(config_path: str, *, core_ready: bool, resolver: SecretResolver | None = None,
                         http: HttpFactory | None = None) -> dict[str, Any]:
    base = {"core": "CORE_READY" if core_ready else "CORE_NOT_READY", "core_readiness_affected": False}
    if not config_path:
        return {**base, "status": "COMPUTE_NODES_NOT_CONFIGURED", "nodes": []}
    try:
        registry = NodeRegistry.load(config_path)
    except (OSError, ValueError, ValidationError) as exc:
        reason = type(exc).__name__ if isinstance(exc, (OSError, ValidationError, json.JSONDecodeError)) else str(exc)
        return {**base, "status": "COMPUTE_NODES_INVALID", "reason": reason[:128], "nodes": []}
    prober = NodeProber(registry, resolver=resolver, http=http)
    nodes = []
    for registration in registry.nodes:
        evidence = prober.probe(registration)
        descriptor = NodeDescriptor.model_validate(evidence["descriptor"]) if evidence["descriptor"] else None
        available = [m for m in descriptor.models if m.availability is ModelAvailability.AVAILABLE] if descriptor else []
        compatible = sorted({t.value for m in available for t in m.tasks
                             if t in ROUTER_CAPABILITY and t in registration.allowed_capabilities})
        nodes.append({
            "node_id": registration.node_id, "node_name": registration.node_name, "endpoint": registration.endpoint,
            "checks": {
                "configuration": "OK",
                "endpoint": "OK" if evidence["reachable"] else "UNREACHABLE",
                "authentication": "OK" if evidence["identity_verified"] else
                                  "FAILED" if evidence["reachable"] else "NOT_CHECKED",
                "health": evidence["state"],
                "capability_manifest": "OK" if evidence["manifest_valid"] else "NOT_VERIFIED",
                "model_availability": f"{len(available)}/{len(descriptor.models)}" if descriptor else "NOT_VERIFIED",
                "provider_compatibility": compatible or "NONE",
            },
            "state": evidence["state"], "authorized": registration.authorized, "reasons": evidence["reasons"],
            "last_successful_check": evidence.get("last_successful_check"),
        })
    states = {node["state"] for node in nodes}
    status = ("COMPUTE_NODES_NOT_CONFIGURED" if not nodes else
              "COMPUTE_NODES_READY" if states == {NodeState.ONLINE.value} else "COMPUTE_NODES_DEGRADED")
    return {**base, "status": status, "nodes": nodes}
