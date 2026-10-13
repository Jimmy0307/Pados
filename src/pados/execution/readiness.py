# SPDX-License-Identifier: AGPL-3.0-only
"""Operator state dimensions: alive, configured, usable, ready, degraded, blocked."""
from collections.abc import Mapping
from typing import Any


def readiness(checks: Mapping[str, Any]) -> dict[str, Any]:
    alive = True  # The local CLI successfully reached this assessment.
    configured = (checks.get("runtime", {}).get("status") == "ok"
                  and checks.get("files", {}).get("alembic_ini") is True
                  and checks.get("files", {}).get("migrations") is True)
    usable = configured and checks.get("database", {}).get("status") == "ok"
    ready = usable and checks.get("status") == "ok"
    system = checks.get("system_health")
    dimensions: dict[str, Any] = {}
    live_codex = "LIVE_CODEX_UAT_PENDING"
    if isinstance(system, Mapping):
        # Doctor readiness also requires the authority-derived runtime dimensions
        # (proof migrations, object root, product loop, provider routing).
        ready = ready and system.get("ready") is True
        dimensions = dict(system.get("dimensions") or {})
        if system.get("provider_live_uat"):
            live_codex = str(system["provider_live_uat"])
    return {
        "alive": alive, "configured": configured, "usable": usable, "ready": ready,
        "degraded": usable and not ready, "blocked": not usable,
        "state": "ready" if ready else "degraded" if usable else "blocked",
        "dimensions": dimensions,
        "live_codex": live_codex,
        "execution_complete_is_semantic_acceptance": False,
    }
