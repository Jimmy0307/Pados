# SPDX-License-Identifier: AGPL-3.0-only
"""Mission Control / doctor projection of the machine inventory.

Operational visibility only (``semantic_authority: NONE``). A row carries the
machine id, name, roles, platform, state, ``last_seen``, staleness and Herdr
flags. It never carries the host, the username, a credential or a reference
value beyond its scheme. A state observed longer ago than
``stale_after_seconds`` (or with a future, naive or unparseable timestamp) is
``stale`` and is shown as ``STALE`` with the ``last_known_state``, never as
live; ``UNAUTHORIZED`` (host key mismatch) stays visible even when stale.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pados.connectivity.state import freshness, observation_age_seconds
from pados.machines.errors import MachineError
from pados.machines.inventory import InventoryStore, MachineInventory
from pados.machines.profile import ConnectionState, MachineProfile, auth_ref_scheme

PLAINTEXT_PASSWORD_PERSISTENCE = "PROHIBITED"


def machine_row(profile: MachineProfile, *, stale_after_seconds: int, now: datetime) -> dict[str, Any]:
    age = observation_age_seconds(profile.state_observed_at, now)
    fresh = freshness(age, stale_after_seconds)
    state = profile.connection_state
    if profile.identity_alert is not None:
        shown = ConnectionState.UNAUTHORIZED.value
    elif fresh["stale"]:
        shown = "STALE"
    else:
        shown = state.value
    return {
        "machine_id": profile.machine_id,
        "display_name": profile.display_name,
        "roles": [role.value for role in profile.roles],
        "platform": profile.platform.value,
        "connection_state": shown,
        "last_known_state": state.value,
        "last_seen": profile.last_seen.isoformat() if profile.last_seen else None,
        "state_observed_at": profile.state_observed_at.isoformat(),
        "observation_age_seconds": fresh["observation_age_seconds"],
        "stale": fresh["stale"],
        "identity_alert": profile.identity_alert.value if profile.identity_alert else None,
        "host_key_fingerprint": profile.host_key_fingerprint,
        "auth_method": profile.auth_method.value,
        "auth_ref_scheme": auth_ref_scheme(profile.auth_ref),
        "compute_node_id": profile.compute_node_id,
        "herdr": {"enabled": profile.herdr.enabled, "ref_configured": profile.herdr.ref is not None,
                  "ref_scheme": profile.herdr.ref_scheme},
    }


def machine_rows(inventory: MachineInventory, *, now: datetime) -> list[dict[str, Any]]:
    return [machine_row(profile, stale_after_seconds=inventory.stale_after_seconds, now=now)
            for profile in sorted(inventory.machines, key=lambda p: (p.display_name.casefold(), p.machine_id))]


def machine_visibility(config_path: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Machine block for Mission Control; ``config_path`` is the machine inventory file."""
    base: dict[str, Any] = {"plaintext_password_persistence": PLAINTEXT_PASSWORD_PERSISTENCE,
                            "semantic_authority": "NONE", "privileged_operations": "SEPARATE_GRANT_REQUIRED",
                            "shell_exposed": False}
    if not config_path:
        return {**base, "status": "MACHINES_NOT_CONFIGURED", "machines": []}
    try:
        inventory = InventoryStore(config_path).load()
    except MachineError as exc:
        return {**base, "status": "MACHINE_INVENTORY_INVALID", "reason": exc.code, "machines": []}
    rows = machine_rows(inventory, now=now or datetime.now(UTC))
    return {**base, "status": "MACHINES_OBSERVED" if rows else "MACHINE_INVENTORY_EMPTY",
            "security_profile": inventory.security_profile.value,
            "stale_after_seconds": inventory.stale_after_seconds, "machines": rows}
