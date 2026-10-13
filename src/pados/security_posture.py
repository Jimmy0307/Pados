# SPDX-License-Identifier: AGPL-3.0-only
"""Deployment security posture checks (warnings only; never change Core readiness).

Labels the active deployment profile and flags configurations that are not
acceptable for a public release, e.g. the built-in default control-token hash.
"""
from __future__ import annotations

from typing import Any

from pados.config import Settings

# The historical built-in default; publicly known once the source is published.
DEFAULT_CONTROL_TOKEN_HASH = "85B427ADA27FF59781973AB29E5F90991D5ABA85771991E0B2D4F9169AB08492"


def security_posture(settings: Settings) -> dict[str, Any]:
    warnings: list[str] = []
    if settings.control_api_token_hash.upper() == DEFAULT_CONTROL_TOKEN_HASH:
        warnings.append("DEFAULT_CONTROL_TOKEN_HASH_IN_USE: run `pados init` to generate a unique control token")
    if settings.api_host not in {"127.0.0.1", "localhost"}:
        warnings.append("API_NOT_LOOPBACK: the control API refuses non-loopback binds")
    profile = "LOCAL_SINGLE_USER"
    if settings.compute_nodes_config:
        profile = "TRUSTED_LAN"  # HMAC node identity: loopback/private LAN only, never public Internet
    return {"deployment_profile": profile, "warnings": warnings, "internet_facing_supported": False,
            "readiness_affected": False}
