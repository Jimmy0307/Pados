# SPDX-License-Identifier: AGPL-3.0-only
"""Simple machine UX: add, list, test, move, re-pin, remove, describe.

The target experience is "Machine: Mac / Host: <LAN IP> / Username: <account> /
Authentication: SSH key | ssh-agent | OS credential reference | one-time
password bootstrap". ``add_machine`` validates the input, reads the host key
through the injected probe, requires the operator to confirm its fingerprint
(``expected_fingerprint``) or to accept it explicitly (``accept_new_host_key``),
derives the identity, checks the key-based login and stores the profile. The
integrator exposes ``pados machine add/list/test/remove`` from these functions.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pados.enterprise.secrets import SecretValue
from pados.machines.bootstrap import BootstrapDraft, BootstrapTransport, bootstrap
from pados.machines.connection import (
    ConnectionProbe,
    ConnectionResult,
    classify,
    observe,
    run_connection_test,
    scan,
)
from pados.machines.errors import MachineError
from pados.machines.inventory import InventoryStore, MachineInventory, utc_now, write_known_hosts
from pados.machines.profile import (
    AuthMethod,
    ConnectionState,
    HerdrMetadata,
    MachineProfile,
    MachineRole,
    Platform,
    auth_ref_scheme,
    validated,
)
from pados.machines.targets import validate_machine_host, validate_port, validate_username
from pados.machines.visibility import machine_row, machine_rows

AUTH_CHOICES: dict[str, AuthMethod] = {
    "ssh-key": AuthMethod.SSH_KEY,
    "ssh-agent": AuthMethod.SSH_AGENT,
    "os-credential": AuthMethod.OS_CREDENTIAL_REF,
}
PASSWORD_BOOTSTRAP = "password-bootstrap"
DEFAULT_STALE_AFTER_SECONDS = int(MachineInventory.model_fields["stale_after_seconds"].default)
DEFAULT_AGENT_REF = "ssh-agent://default"
_AUTH_LABELS = {
    AuthMethod.SSH_KEY: "SSH key",
    AuthMethod.SSH_AGENT: "ssh-agent",
    AuthMethod.OS_CREDENTIAL_REF: "OS credential reference",
}


def scan_host_key(host: str, *, probe: ConnectionProbe, port: int = 22,
                  hostname_confirmed: bool = False) -> dict[str, str]:
    """The fingerprint a host presents, for the operator to confirm before ``add_machine``."""
    canonical = validate_machine_host(host, hostname_confirmed=hostname_confirmed)
    key = scan(probe, canonical, validate_port(port))
    return {"host_key_fingerprint": key.fingerprint, "key_type": key.key_type, "machine_id": key.machine_id}


def _auth_method(auth: str | AuthMethod) -> AuthMethod | None:
    if isinstance(auth, AuthMethod):
        return None if auth is AuthMethod.PASSWORD_BOOTSTRAP_COMPLETED else auth
    if auth == PASSWORD_BOOTSTRAP:
        return None
    method = AUTH_CHOICES.get(auth) if isinstance(auth, str) else None
    if method is None:
        raise MachineError("AUTH_METHOD_UNKNOWN")
    return method


def add_machine(name: str, host: str, username: str, auth: str | AuthMethod, *, store: InventoryStore,
                probe: ConnectionProbe, port: int = 22, platform: Platform | str = Platform.LINUX,
                roles: Iterable[MachineRole | str] = (), auth_ref: str | None = None,
                expected_fingerprint: str | None = None, accept_new_host_key: bool = False,
                hostname_confirmed: bool = False, herdr_enabled: bool = False, herdr_ref: str | None = None,
                compute_node_id: str | None = None, one_time_password: SecretValue | None = None,
                transport: BootstrapTransport | None = None, public_key: str | None = None,
                key_auth: str | AuthMethod = "ssh-agent", now: datetime | None = None) -> MachineProfile:
    """Enroll one machine. ``auth`` is ssh-key | ssh-agent | os-credential | password-bootstrap.

    A bootstrap password is moved into a one-slot box that is emptied in
    ``finally``; a failure is re-raised as a fresh code-only error from this
    frame, so no traceback frame of this call retains the password.
    """
    box: list[SecretValue | None] = [one_time_password]
    del one_time_password
    code, machine_id = "MACHINE_ERROR", None
    try:
        return _add_machine(name, host, username, auth, store=store, probe=probe, port=port, platform=platform,
                            roles=roles, auth_ref=auth_ref, expected_fingerprint=expected_fingerprint,
                            accept_new_host_key=accept_new_host_key, hostname_confirmed=hostname_confirmed,
                            herdr_enabled=herdr_enabled, herdr_ref=herdr_ref, compute_node_id=compute_node_id,
                            password_box=box, transport=transport, public_key=public_key, key_auth=key_auth,
                            now=now)
    except MachineError as exc:
        code, machine_id = exc.code, exc.machine_id
    finally:
        box.clear()
    raise MachineError(code, machine_id=machine_id)


def _add_machine(name: str, host: str, username: str, auth: str | AuthMethod, *, store: InventoryStore,
                 probe: ConnectionProbe, port: int, platform: Platform | str, roles: Iterable[MachineRole | str],
                 auth_ref: str | None, expected_fingerprint: str | None, accept_new_host_key: bool,
                 hostname_confirmed: bool, herdr_enabled: bool, herdr_ref: str | None,
                 compute_node_id: str | None, password_box: list[SecretValue | None],
                 transport: BootstrapTransport | None, public_key: str | None, key_auth: str | AuthMethod,
                 now: datetime | None) -> MachineProfile:
    method = _auth_method(auth)
    if method is not None and (password_box[0] is not None or transport is not None):
        raise MachineError("PASSWORD_ONLY_FOR_PASSWORD_BOOTSTRAP")
    canonical = validate_machine_host(host, hostname_confirmed=hostname_confirmed)
    target_port = validate_port(port)
    validate_username(username)
    inventory = store.load()
    key = scan(probe, canonical, target_port)
    if expected_fingerprint is not None:
        if expected_fingerprint != key.fingerprint:
            raise MachineError("HOST_KEY_MISMATCH", machine_id=key.machine_id)
    elif not accept_new_host_key:
        raise MachineError("HOST_KEY_CONFIRMATION_REQUIRED", machine_id=key.machine_id)
    _refuse_duplicate(inventory, key.machine_id, canonical, target_port, name)
    herdr = validated(HerdrMetadata, {"enabled": herdr_enabled, "ref": herdr_ref}, prefix="HERDR_METADATA") if (
        herdr_enabled or herdr_ref) else HerdrMetadata()
    common: dict[str, Any] = {
        "display_name": name, "host": canonical, "hostname_confirmed": hostname_confirmed, "port": target_port,
        "username": username, "platform": platform, "roles": tuple(roles), "host_public_key": key.openssh(),
        "herdr": herdr, "compute_node_id": compute_node_id,
    }
    observed = now or utc_now()
    if method is None:
        profile = _bootstrapped(common, key_auth, auth_ref, password_box, transport, public_key, observed)
    else:
        profile = _key_enrolled(store, inventory, common, method, auth_ref, probe, key.machine_id,
                                key.fingerprint, observed)
    return store.update(lambda current: current.with_profile(profile)).get(profile.machine_id)


def _refuse_duplicate(inventory: MachineInventory, machine_id: str, host: str, port: int, name: str) -> None:
    for existing in inventory.machines:
        if existing.machine_id == machine_id:
            raise MachineError("MACHINE_ALREADY_ENROLLED", machine_id=machine_id)
        if (existing.host, existing.port) == (host, port):
            raise MachineError("MACHINE_LOCATION_DUPLICATE", machine_id=existing.machine_id)
        if existing.display_name.casefold() == name.casefold():
            raise MachineError("MACHINE_DISPLAY_NAME_DUPLICATE", machine_id=existing.machine_id)


def _bootstrapped(common: dict[str, Any], key_auth: str | AuthMethod, auth_ref: str | None,
                  password_box: list[SecretValue | None], transport: BootstrapTransport | None,
                  public_key: str | None, observed: datetime) -> MachineProfile:
    if password_box[0] is None or transport is None or public_key is None:
        raise MachineError("BOOTSTRAP_REQUIRES_PASSWORD_TRANSPORT_AND_PUBLIC_KEY")
    key_method = _auth_method(key_auth)
    if key_method not in {AuthMethod.SSH_KEY, AuthMethod.SSH_AGENT}:
        raise MachineError("BOOTSTRAP_REQUIRES_KEY_AUTH_METHOD")
    ref = auth_ref or (DEFAULT_AGENT_REF if key_method is AuthMethod.SSH_AGENT else None)
    if ref is None:
        raise MachineError("AUTH_REF_REQUIRED")
    draft = validated(BootstrapDraft, {**common, "key_auth_method": key_method, "auth_ref": ref,
                                       "public_key": public_key}, prefix="BOOTSTRAP_DRAFT")
    # The password leaves the box straight into ``bootstrap``; no local of this frame holds it.
    return bootstrap(draft, password_box.pop(), transport, now=observed)  # type: ignore[arg-type]


def _key_enrolled(store: InventoryStore, inventory: MachineInventory, common: dict[str, Any], method: AuthMethod,
                  auth_ref: str | None, probe: ConnectionProbe, machine_id: str, fingerprint: str,
                  observed: datetime) -> MachineProfile:
    ref = auth_ref or (DEFAULT_AGENT_REF if method is AuthMethod.SSH_AGENT else None)
    if ref is None:
        raise MachineError("AUTH_REF_REQUIRED")
    candidate = validated(MachineProfile, {
        **common, "machine_id": machine_id, "host_key_fingerprint": fingerprint, "auth_method": method,
        "auth_ref": ref, "enrolled_at": observed, "connection_state": ConnectionState.OFFLINE,
        "state_observed_at": observed,
    }, prefix="MACHINE_PROFILE")
    write_known_hosts(store.known_hosts_path, (*inventory.machines, candidate))
    state, code = classify(candidate, observe(probe, candidate, store.known_hosts_path))
    if state is ConnectionState.UNAUTHORIZED:
        write_known_hosts(store.known_hosts_path, inventory.machines)
        raise MachineError(code, machine_id=machine_id)
    record = candidate.model_dump()
    record.update(connection_state=state, last_seen=None if state is ConnectionState.OFFLINE else observed)
    return validated(MachineProfile, record, prefix="MACHINE_PROFILE")


def list_machines(store: InventoryStore, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Visibility rows (no host, no credential reference values)."""
    return machine_rows(store.load(), now=now or utc_now())


def check_machine(store: InventoryStore, name_or_id: str, probe: ConnectionProbe, *,
                  now: datetime | None = None) -> ConnectionResult:
    """``pados machine test``: probe one inventoried machine (targets only from the inventory)."""
    return run_connection_test(store, store.load().find(name_or_id).machine_id, probe, now=now)


def remove_machine(store: InventoryStore, name_or_id: str) -> str:
    machine_id = store.load().find(name_or_id).machine_id
    store.update(lambda current: current.without(machine_id))
    return machine_id


def acknowledge_host_key_alert(store: InventoryStore, name_or_id: str, *, confirmed_fingerprint: str,
                               probe: ConnectionProbe, now: datetime | None = None) -> MachineProfile:
    """Human clears a HOST_KEY_MISMATCH alert after confirming the *pinned* key is still correct.

    The machine must present the pinned key again; nothing about its identity changes.
    """
    profile = store.load().find(name_or_id)
    if profile.identity_alert is None:
        raise MachineError("MACHINE_HAS_NO_IDENTITY_ALERT", machine_id=profile.machine_id)
    if confirmed_fingerprint != profile.host_key_fingerprint:
        raise MachineError("HOST_KEY_CONFIRMATION_MISMATCH", machine_id=profile.machine_id)
    if scan(probe, profile.host, profile.port).machine_id != profile.machine_id:
        raise MachineError("HOST_KEY_MISMATCH", machine_id=profile.machine_id)
    observed = now or utc_now()

    def apply(current: MachineInventory) -> MachineInventory:
        record = current.get(profile.machine_id).model_dump()
        record.update(identity_alert=None, connection_state=ConnectionState.OFFLINE, state_observed_at=observed)
        return current.replacing(validated(MachineProfile, record, prefix="MACHINE_PROFILE"))

    store.update(apply)
    return _tested(store, profile.machine_id, probe, observed)


def _tested(store: InventoryStore, machine_id: str, probe: ConnectionProbe, now: datetime) -> MachineProfile:
    run_connection_test(store, machine_id, probe, now=now)
    return store.load().get(machine_id)


def repin_machine(store: InventoryStore, name_or_id: str, *, confirmed_fingerprint: str, probe: ConnectionProbe,
                  now: datetime | None = None) -> MachineProfile:
    """Human re-pin after a legitimate host key change (reinstall): a NEW identity replaces the old.

    The operator must confirm the new fingerprint out of band. The old profile is
    removed and the new one enrolled with the same settings; compute nodes that
    referenced the old ``machine_id`` are then flagged by ``check_compute_nodes``.
    """
    inventory = store.load()
    old = inventory.find(name_or_id)
    key = scan(probe, old.host, old.port)
    if key.fingerprint != confirmed_fingerprint:
        raise MachineError("HOST_KEY_CONFIRMATION_MISMATCH", machine_id=old.machine_id)
    if key.machine_id == old.machine_id:
        raise MachineError("HOST_KEY_UNCHANGED_USE_ACKNOWLEDGE", machine_id=old.machine_id)
    if any(p.machine_id == key.machine_id for p in inventory.machines):
        raise MachineError("MACHINE_ALREADY_ENROLLED", machine_id=key.machine_id)
    observed = now or utc_now()
    record = old.model_dump()
    record.update(machine_id=key.machine_id, host_public_key=key.openssh(), host_key_fingerprint=key.fingerprint,
                  enrolled_at=observed, identity_alert=None, connection_state=ConnectionState.OFFLINE,
                  state_observed_at=observed, last_seen=None)
    new = validated(MachineProfile, record, prefix="MACHINE_PROFILE")

    def apply(current: MachineInventory) -> MachineInventory:
        return current.without(old.machine_id).with_profile(new)

    store.update(apply)
    return _tested(store, new.machine_id, probe, observed)


def describe(profile: MachineProfile, *, stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
             now: datetime | None = None) -> str:
    """Short human summary for the operator; credential references show their scheme only.

    The state goes through the same freshness projection as ``machine_visibility``:
    an old observation reads ``STALE (last known <state>)``, never as live.
    """
    method = profile.effective_auth_method
    auth = f"{_AUTH_LABELS.get(method, method.value)} ({auth_ref_scheme(profile.auth_ref)} reference)"
    if profile.auth_method is AuthMethod.PASSWORD_BOOTSTRAP_COMPLETED:
        auth += ", installed by one-time password bootstrap (the password was not stored)"
    roles = ", ".join(role.value for role in profile.roles) or "none"
    seen = profile.last_seen.isoformat() if profile.last_seen else "never"
    row = machine_row(profile, stale_after_seconds=stale_after_seconds, now=now or utc_now())
    state = row["connection_state"]
    if state != row["last_known_state"]:
        state += f" (last known {row['last_known_state']}, observed {row['state_observed_at']})"
    if profile.identity_alert is not None:
        state += f" ({profile.identity_alert.value}: human re-pin or acknowledgement required)"
    lines = [
        f"Machine: {profile.display_name} ({profile.machine_id})",
        f"Host: {profile.host} port {profile.port}",
        f"Username: {profile.username}",
        f"Authentication: {auth}",
        f"Platform: {profile.platform.value}",
        f"Roles: {roles}",
        f"Host key: {profile.host_key_fingerprint}",
        f"State: {state} (last seen {seen})",
    ]
    if profile.herdr.enabled:
        lines.append(f"Herdr: enabled ({profile.herdr.ref_scheme} reference)")
    return "\n".join(lines)


def describe_machine(store: InventoryStore, name_or_id: str, *, now: datetime | None = None) -> str:
    """``pados machine show``: the summary with the inventory's own stale threshold."""
    inventory = store.load()
    return describe(inventory.find(name_or_id), stale_after_seconds=inventory.stale_after_seconds,
                    now=now or utc_now())
