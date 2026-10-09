# SPDX-License-Identifier: AGPL-3.0-only
"""The one machine inventory (operator-owned JSON file).

One physical machine has exactly one profile; roles (Home Base, Local Compute
Node, Herdr machine, sandbox host) are attributes of that profile, never
separate inventories. Two profiles for one host key, one machine id, one
location (host + port) or one display name are refused. The file is written
atomically with owner-only permissions under a cross-process lock; every
record passes ``assert_minimized`` against the explicit profile schema on
write and on read, and a corrupt, tampered, symlinked or group/world-writable
file fails closed (``MACHINE_INVENTORY_INVALID``) instead of reading as empty.

The PADOS-managed ``known_hosts`` file is derived from the inventory: one line
per machine keyed by ``machine_id`` (used as the OpenSSH ``HostKeyAlias``), so a
host key is pinned to the machine identity and never to an address.

Compute-node registrations may reference a ``machine_id``;
``check_compute_nodes`` flags unknown machines, broken links and two nodes
claiming one machine with conflicting endpoints. It reports, never repairs.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.compute_node.identity_providers import SecurityProfile
from pados.compute_node.registry import NodeRegistration
from pados.connectivity.storage import atomic_write_json, store_lock
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.machines.errors import MachineError
from pados.machines.profile import PROFILE_KEYS, MachineProfile, MachineRole, profile_record, validated

INVENTORY_SCHEMA = "pados.machines.inventory.v1"
HEADER_KEYS = frozenset({"schema_version", "security_profile", "stale_after_seconds"})
INVENTORY_KEYS = HEADER_KEYS | {"machines"}
MAX_MACHINES = 256
MAX_INVENTORY_BYTES = 2 * 1024 * 1024
KNOWN_HOSTS_NAME = "pados_known_hosts"
# Codes a failed load reports as-is; every other failure is MACHINE_INVENTORY_INVALID.
_LOAD_CODES = ("MACHINE_INVENTORY", "MACHINE_DUPLICATE_IDENTITY", "MACHINE_LOCATION_DUPLICATE",
               "MACHINE_DISPLAY_NAME_DUPLICATE")


class MachineInventory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = INVENTORY_SCHEMA
    security_profile: SecurityProfile = SecurityProfile.TRUSTED_LAN
    stale_after_seconds: int = Field(default=300, gt=0, le=86_400)
    machines: tuple[MachineProfile, ...] = Field(default=(), max_length=MAX_MACHINES)

    @model_validator(mode="after")
    def one_profile_per_machine(self) -> MachineInventory:
        if self.schema_version != INVENTORY_SCHEMA:
            raise MachineError("MACHINE_INVENTORY_SCHEMA_INVALID")
        _refuse_duplicates(self.machines)
        return self

    def get(self, machine_id: str) -> MachineProfile:
        for profile in self.machines:
            if profile.machine_id == machine_id:
                return profile
        raise MachineError("MACHINE_NOT_FOUND")

    def find(self, name_or_id: str) -> MachineProfile:
        """Look up by machine id or (case-insensitive) display name."""
        for profile in self.machines:
            if name_or_id == profile.machine_id or profile.display_name.casefold() == name_or_id.casefold():
                return profile
        raise MachineError("MACHINE_NOT_FOUND")

    def with_profile(self, profile: MachineProfile) -> MachineInventory:
        """Add a new machine; an already-enrolled host key is refused, never duplicated."""
        if any(p.machine_id == profile.machine_id for p in self.machines):
            raise MachineError("MACHINE_ALREADY_ENROLLED", machine_id=profile.machine_id)
        return self._replace((*self.machines, profile))

    def replacing(self, profile: MachineProfile) -> MachineInventory:
        """Replace the profile of the same machine (same host key); identity never changes here."""
        current = self.get(profile.machine_id)
        if current.host_public_key != profile.host_public_key:
            raise MachineError("HOST_KEY_MISMATCH", machine_id=profile.machine_id)
        return self._replace(tuple(profile if p.machine_id == profile.machine_id else p for p in self.machines))

    def without(self, machine_id: str) -> MachineInventory:
        self.get(machine_id)
        return self._replace(tuple(p for p in self.machines if p.machine_id != machine_id))

    def _replace(self, machines: tuple[MachineProfile, ...]) -> MachineInventory:
        if len(machines) > MAX_MACHINES:
            raise MachineError("MACHINE_INVENTORY_FULL")
        _refuse_duplicates(machines)
        return MachineInventory(schema_version=self.schema_version, security_profile=self.security_profile,
                                stale_after_seconds=self.stale_after_seconds, machines=machines)


def _refuse_duplicates(machines: Iterable[MachineProfile]) -> None:
    ids: set[str] = set()
    fingerprints: set[str] = set()
    locations: set[tuple[str, int]] = set()
    names: set[str] = set()
    for profile in machines:
        if profile.machine_id in ids or profile.host_key_fingerprint in fingerprints:
            raise MachineError("MACHINE_DUPLICATE_IDENTITY", machine_id=profile.machine_id)
        if (profile.host, profile.port) in locations:
            raise MachineError("MACHINE_LOCATION_DUPLICATE", machine_id=profile.machine_id)
        if profile.display_name.casefold() in names:
            raise MachineError("MACHINE_DISPLAY_NAME_DUPLICATE", machine_id=profile.machine_id)
        ids.add(profile.machine_id)
        fingerprints.add(profile.host_key_fingerprint)
        locations.add((profile.host, profile.port))
        names.add(profile.display_name.casefold())


def inventory_record(inventory: MachineInventory) -> dict[str, Any]:
    header = inventory.model_dump(mode="json", include=set(HEADER_KEYS))
    assert_minimized(header, allowed_keys=HEADER_KEYS)
    return {**header, "machines": [profile_record(profile) for profile in inventory.machines]}


def _parse(raw: bytes) -> MachineInventory:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or set(data) - INVENTORY_KEYS:
        raise MachineError("MACHINE_INVENTORY_INVALID")
    machines = data.get("machines", [])
    if not isinstance(machines, list) or len(machines) > MAX_MACHINES:
        raise MachineError("MACHINE_INVENTORY_INVALID")
    header = {key: value for key, value in data.items() if key != "machines"}
    assert_minimized(header, allowed_keys=HEADER_KEYS)
    profiles = []
    for row in machines:
        if not isinstance(row, dict):
            raise MachineError("MACHINE_INVENTORY_INVALID")
        assert_minimized(row, allowed_keys=PROFILE_KEYS)
        profiles.append(validated(MachineProfile, row, prefix="MACHINE_PROFILE"))
    return validated(MachineInventory, {**header, "machines": profiles}, prefix="MACHINE_INVENTORY")


class InventoryStore:
    """Reads and transactionally updates the inventory file at ``path``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def known_hosts_path(self) -> Path:
        return self.path.with_name(KNOWN_HOSTS_NAME)

    def _read_bytes(self) -> bytes | None:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise MachineError("MACHINE_INVENTORY_INVALID")
        if sys.platform != "win32" and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_IRGRP | stat.S_IROTH):
            raise MachineError("MACHINE_INVENTORY_PERMISSIONS_TOO_OPEN")
        if info.st_size > MAX_INVENTORY_BYTES:
            raise MachineError("MACHINE_INVENTORY_INVALID")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read(MAX_INVENTORY_BYTES + 1)

    def load(self) -> MachineInventory:
        """The inventory; a missing file is an empty inventory, anything unreadable fails closed."""
        code: str | None = None
        try:
            raw = self._read_bytes()
            if raw is None:
                return MachineInventory()
            if len(raw) > MAX_INVENTORY_BYTES:
                raise MachineError("MACHINE_INVENTORY_INVALID")
            return _parse(raw)
        except MachineError as exc:
            code = exc.code if exc.code.startswith(_LOAD_CODES) else "MACHINE_INVENTORY_INVALID"
        except (OSError, UnicodeDecodeError, ValueError, DataMinimizationViolation):
            code = "MACHINE_INVENTORY_INVALID"
        raise MachineError(code)

    def save(self, inventory: MachineInventory) -> None:
        with store_lock(self.path):
            self._write(inventory)

    def _write(self, inventory: MachineInventory) -> None:
        atomic_write_json(self.path, inventory_record(inventory))
        write_known_hosts(self.known_hosts_path, inventory.machines)

    def update(self, change: Callable[[MachineInventory], MachineInventory]) -> MachineInventory:
        """Reload, apply ``change`` and persist as one locked transaction."""
        with store_lock(self.path):
            updated = change(self.load())
            self._write(updated)
            return updated


def known_hosts_lines(machines: Iterable[MachineProfile]) -> list[str]:
    return [f"{profile.machine_id} {profile.host_public_key}" for profile in machines]


def write_known_hosts(path: Path, machines: Iterable[MachineProfile]) -> None:
    """Atomically write the PADOS-managed known_hosts file (owner-only, never a symlink)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise MachineError("KNOWN_HOSTS_SYMLINK_REFUSED")
    content = "".join(f"{line}\n" for line in known_hosts_lines(machines)).encode("ascii")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _endpoint_host(endpoint: str) -> str:
    try:
        return httpx.URL(endpoint).host
    except (httpx.InvalidURL, ValueError):
        return ""


def check_compute_nodes(inventory: MachineInventory, nodes: Iterable[NodeRegistration]) -> list[dict[str, Any]]:
    """Consistency findings between the machine inventory and compute-node registrations."""
    registrations = list(nodes)
    known = {profile.machine_id: profile for profile in inventory.machines}
    by_id = {node.node_id: node for node in registrations}
    findings: list[dict[str, Any]] = []
    claims: dict[str, list[NodeRegistration]] = {}
    for node in registrations:
        if node.machine_id is None:
            continue
        claims.setdefault(node.machine_id, []).append(node)
        profile = known.get(node.machine_id)
        if profile is None:
            findings.append({"code": "COMPUTE_NODE_MACHINE_UNKNOWN", "node_id": node.node_id,
                             "machine_id": node.machine_id})
        elif MachineRole.LOCAL_COMPUTE_NODE not in profile.roles:
            findings.append({"code": "COMPUTE_NODE_MACHINE_ROLE_MISSING", "node_id": node.node_id,
                             "machine_id": node.machine_id})
    for machine_id, claimed in sorted(claims.items()):
        if len({_endpoint_host(node.endpoint) for node in claimed}) > 1:
            findings.extend({"code": "MACHINE_CLAIMED_BY_CONFLICTING_NODES", "node_id": node.node_id,
                             "machine_id": machine_id} for node in sorted(claimed, key=lambda n: n.node_id))
    for profile in inventory.machines:
        if profile.compute_node_id is None:
            continue
        linked = by_id.get(profile.compute_node_id)
        if linked is None:
            findings.append({"code": "MACHINE_COMPUTE_NODE_LINK_UNKNOWN", "node_id": profile.compute_node_id,
                             "machine_id": profile.machine_id})
        elif linked.machine_id != profile.machine_id:
            findings.append({"code": "MACHINE_COMPUTE_NODE_LINK_MISMATCH", "node_id": profile.compute_node_id,
                             "machine_id": profile.machine_id})
    return sorted(findings, key=lambda f: (f["code"], f["node_id"], f["machine_id"]))


def utc_now() -> datetime:
    return datetime.now(UTC)
