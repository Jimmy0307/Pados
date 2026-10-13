# SPDX-License-Identifier: AGPL-3.0-only
"""Machine connection test: observed state, never assumed state.

A ``ConnectionProbe`` (``pados.machines.ssh.OpenSshProbe`` in production, a fake
in tests) reports what it observed: reachability, the host key the machine
presented, whether key-based login succeeded and, for Herdr machines, whether
Herdr answered. ``classify`` maps an observation onto one state:

* ``OFFLINE``           not reachable;
* ``UNAUTHORIZED``      the presented host key is not the pinned key
  (``HOST_KEY_MISMATCH``: possible spoof) or no host key was proven. The alert
  is sticky: it is cleared only by a human re-pin or acknowledgement, never by
  a later probe, and the machine is never silently re-enrolled;
* ``AUTH_REQUIRED``     reachable with the pinned key, but login failed;
* ``HERDR_UNAVAILABLE`` Herdr enabled for the machine but not observed available;
* ``DEGRADED``          login succeeded with a degraded observation;
* ``ONLINE``            everything observed healthy.

Probe targets come only from the operator-owned inventory:
``run_connection_test`` takes a machine id, never a host. ``last_seen`` is
updated only when the pinned host key was proven; ``state_observed_at`` drives
staleness in visibility. Every observation is bound to the probed profile's
revision digest (location, account, credential reference, host key, Herdr settings) and is
applied compare-and-set under the inventory lock: if the profile changed while
the probe ran, the observation is discarded (``OBSERVATION_STALE_REVISION``).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pados.execution.context import canonical_hash
from pados.machines.errors import MachineError
from pados.machines.identity import HostKey
from pados.machines.inventory import InventoryStore, MachineInventory, utc_now, write_known_hosts
from pados.machines.profile import ConnectionState, IdentityAlert, MachineProfile, validated
from pados.machines.targets import validate_machine_host, validate_port

logger = logging.getLogger("pados.machines")


class AuthOutcome(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


@dataclass(frozen=True)
class ProbeObservation:
    reachable: bool
    host_key: HostKey | None = None
    host_key_rejected: bool = False
    auth: AuthOutcome = AuthOutcome.NOT_ATTEMPTED
    degraded: bool = False
    herdr_available: bool | None = None
    error_code: str | None = None


class ConnectionProbe(Protocol):
    def scan_host_key(self, host: str, port: int) -> HostKey | None:
        """The host public key ``host:port`` presents, or ``None`` when unreachable."""

    def check(self, profile: MachineProfile, known_hosts: Path) -> ProbeObservation:
        """Connect with the pinned key in ``known_hosts`` (keyed by machine id) and key-based login."""


@dataclass(frozen=True)
class ConnectionResult:
    machine_id: str
    connection_state: ConnectionState
    code: str
    observed_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"machine_id": self.machine_id, "connection_state": self.connection_state.value,
                "code": self.code, "observed_at": self.observed_at.isoformat()}


_UNPROVEN = frozenset({ConnectionState.OFFLINE, ConnectionState.UNAUTHORIZED})
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
STALE_REVISION = "OBSERVATION_STALE_REVISION"


# Every profile field a probe or ``classify`` reads (directly or through ``host_key``,
# ``effective_auth_method`` and ``herdr``). ``OBSERVATION_FIELDS`` are what an observation
# writes or pure metadata. tests/machines enforce that the two sets partition the profile
# schema, so a new field must be classified before it can ship.
PROBE_RELEVANT_FIELDS = ("machine_id", "host", "port", "hostname_confirmed", "username", "auth_method",
                         "key_auth_method", "auth_ref", "host_public_key", "host_key_fingerprint", "herdr")
OBSERVATION_FIELDS = ("display_name", "platform", "roles", "enrolled_at", "connection_state",
                      "state_observed_at", "last_seen", "identity_alert", "compute_node_id")


def profile_revision(profile: MachineProfile) -> str:
    """Digest of every probe/classification input (location, account, credential, host key, Herdr)."""
    record = profile.model_dump(mode="json")
    return canonical_hash({"domain": "pados-machine-profile-revision-v2",
                           **{name: record[name] for name in PROBE_RELEVANT_FIELDS}})


def _current_at_revision(current: MachineInventory, machine_id: str, revision: str) -> MachineProfile:
    """Compare-and-set guard (under the inventory lock): the profile must still be the one probed."""
    profile = current.get(machine_id)
    if profile_revision(profile) != revision:
        logger.warning("machine.observation_discarded machine_id=%s code=%s", machine_id, STALE_REVISION)
        raise MachineError(STALE_REVISION, machine_id=machine_id)
    return profile


def classify(profile: MachineProfile, observation: ProbeObservation) -> tuple[ConnectionState, str]:
    if not observation.reachable:
        return ConnectionState.OFFLINE, "MACHINE_UNREACHABLE"
    if observation.host_key_rejected or (
            observation.host_key is not None and observation.host_key.machine_id != profile.machine_id):
        return ConnectionState.UNAUTHORIZED, IdentityAlert.HOST_KEY_MISMATCH.value
    if observation.host_key is None:
        return ConnectionState.UNAUTHORIZED, "HOST_KEY_NOT_VERIFIED"
    if observation.auth is not AuthOutcome.AUTHENTICATED:
        code = observation.error_code
        return ConnectionState.AUTH_REQUIRED, code if code and _CODE.fullmatch(code) else "MACHINE_AUTH_REQUIRED"
    if profile.herdr.enabled and observation.herdr_available is not True:
        return ConnectionState.HERDR_UNAVAILABLE, "HERDR_NOT_OBSERVED_AVAILABLE"
    if observation.degraded:
        return ConnectionState.DEGRADED, "MACHINE_DEGRADED"
    return ConnectionState.ONLINE, "MACHINE_ONLINE"


def observe(probe: ConnectionProbe, profile: MachineProfile, known_hosts: Path) -> ProbeObservation:
    """Run one probe; a probe failure is an unreachable observation, never an exception text."""
    try:
        observation = probe.check(profile, known_hosts)
    except Exception:  # noqa: BLE001 - any probe failure is OFFLINE with a local code only
        return ProbeObservation(reachable=False, error_code="PROBE_FAILED")
    if not isinstance(observation, ProbeObservation):
        return ProbeObservation(reachable=False, error_code="PROBE_RESULT_INVALID")
    return observation


def scan(probe: ConnectionProbe, host: str, port: int) -> HostKey:
    try:
        key = probe.scan_host_key(host, port)
    except MachineError:
        raise
    except Exception:  # noqa: BLE001 - mapped to a local code
        key = None
    if not isinstance(key, HostKey):
        raise MachineError("MACHINE_UNREACHABLE")
    return key


def observed_profile(profile: MachineProfile, state: ConnectionState, code: str, now: datetime) -> MachineProfile:
    """``profile`` with the observation applied (fully re-validated, never ``model_copy``)."""
    alert = profile.identity_alert
    if code == IdentityAlert.HOST_KEY_MISMATCH.value:
        alert = IdentityAlert.HOST_KEY_MISMATCH
    if alert is not None:
        state = ConnectionState.UNAUTHORIZED
    record = profile.model_dump()
    record.update(connection_state=state, state_observed_at=now, identity_alert=alert,
                  last_seen=profile.last_seen if state in _UNPROVEN else now)
    return validated(MachineProfile, record, prefix="MACHINE_PROFILE")


def run_connection_test(store: InventoryStore, machine_id: str, probe: ConnectionProbe, *,
                    now: datetime | None = None) -> ConnectionResult:
    """Probe one inventoried machine and record the observed state."""
    inventory = store.load()
    profile = inventory.get(machine_id)
    revision = profile_revision(profile)
    write_known_hosts(store.known_hosts_path, inventory.machines)
    state, code = classify(profile, observe(probe, profile, store.known_hosts_path))
    observed_at = now or utc_now()

    def apply(current: MachineInventory) -> MachineInventory:
        probed = _current_at_revision(current, machine_id, revision)
        return current.replacing(observed_profile(probed, state, code, observed_at))

    updated = store.update(apply).get(machine_id)
    if updated.identity_alert is not None:
        code = updated.identity_alert.value
    logger.info("machine.connection_test machine_id=%s state=%s code=%s", machine_id,
                updated.connection_state.value, code)
    return ConnectionResult(machine_id, updated.connection_state, code, observed_at)


def update_location(store: InventoryStore, machine_id: str, host: str, probe: ConnectionProbe, *,
                    port: int | None = None, hostname_confirmed: bool = False,
                    now: datetime | None = None) -> MachineProfile:
    """Move a machine to a new address. Only the same host key may move: identity is the key."""
    canonical = validate_machine_host(host, hostname_confirmed=hostname_confirmed)
    inventory = store.load()
    profile = inventory.get(machine_id)
    revision = profile_revision(profile)
    target_port = validate_port(profile.port if port is None else port)
    if any(p.machine_id != machine_id and (p.host, p.port) == (canonical, target_port) for p in inventory.machines):
        raise MachineError("MACHINE_LOCATION_DUPLICATE", machine_id=machine_id)
    if scan(probe, canonical, target_port).machine_id != machine_id:
        logger.warning("machine.location_refused machine_id=%s code=HOST_KEY_MISMATCH", machine_id)
        raise MachineError("HOST_KEY_MISMATCH", machine_id=machine_id)
    record = profile.model_dump()
    record.update(host=canonical, port=target_port, hostname_confirmed=hostname_confirmed)
    candidate = validated(MachineProfile, record, prefix="MACHINE_PROFILE")
    write_known_hosts(store.known_hosts_path, inventory.machines)
    state, code = classify(candidate, observe(probe, candidate, store.known_hosts_path))
    if state in _UNPROVEN:
        raise MachineError(code, machine_id=machine_id)
    observed_at = now or utc_now()

    def apply(current: MachineInventory) -> MachineInventory:
        moved = _current_at_revision(current, machine_id, revision).model_dump()
        moved.update(host=canonical, port=target_port, hostname_confirmed=hostname_confirmed)
        located = validated(MachineProfile, moved, prefix="MACHINE_PROFILE")
        return current.replacing(observed_profile(located, state, code, observed_at))

    logger.info("machine.location_updated machine_id=%s state=%s", machine_id, state.value)
    return store.update(apply).get(machine_id)
