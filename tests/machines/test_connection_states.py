"""Connection states, spoof detection, location changes, re-pin and staleness."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest
from machine_support import NOW, FakeProbe, enroll, host_key, lan

from typer.testing import CliRunner

from pados.cli.main import app
from pados.config import get_settings
from pados.machines import connection
from pados.machines.connection import AuthOutcome, ProbeObservation, classify, profile_revision
from pados.machines.errors import MachineError
from pados.machines.inventory import InventoryStore, MachineInventory
from pados.machines.profile import ConnectionState, MachineProfile, validated
from pados.machines.ux import (
    acknowledge_host_key_alert,
    add_machine,
    check_machine,
    describe,
    describe_machine,
    repin_machine,
    scan_host_key,
)
from pados.machines.visibility import machine_visibility


@pytest.mark.parametrize(("observation", "state", "code"), [
    (ProbeObservation(reachable=False), ConnectionState.OFFLINE, "MACHINE_UNREACHABLE"),
    (ProbeObservation(reachable=True, host_key=host_key("evil")), ConnectionState.UNAUTHORIZED,
     "HOST_KEY_MISMATCH"),
    (ProbeObservation(reachable=True, host_key=host_key("mac"), host_key_rejected=True),
     ConnectionState.UNAUTHORIZED, "HOST_KEY_MISMATCH"),
    (ProbeObservation(reachable=True), ConnectionState.UNAUTHORIZED, "HOST_KEY_NOT_VERIFIED"),
    (ProbeObservation(reachable=True, host_key=host_key("mac"), auth=AuthOutcome.AUTH_REQUIRED),
     ConnectionState.AUTH_REQUIRED, "MACHINE_AUTH_REQUIRED"),
    (ProbeObservation(reachable=True, host_key=host_key("mac"), auth=AuthOutcome.AUTHENTICATED,
                      herdr_available=False), ConnectionState.HERDR_UNAVAILABLE, "HERDR_NOT_OBSERVED_AVAILABLE"),
    (ProbeObservation(reachable=True, host_key=host_key("mac"), auth=AuthOutcome.AUTHENTICATED,
                      herdr_available=None), ConnectionState.HERDR_UNAVAILABLE, "HERDR_NOT_OBSERVED_AVAILABLE"),
    (ProbeObservation(reachable=True, host_key=host_key("mac"), auth=AuthOutcome.AUTHENTICATED,
                      herdr_available=True, degraded=True), ConnectionState.DEGRADED, "MACHINE_DEGRADED"),
    (ProbeObservation(reachable=True, host_key=host_key("mac"), auth=AuthOutcome.AUTHENTICATED,
                      herdr_available=True), ConnectionState.ONLINE, "MACHINE_ONLINE"),
])
def test_state_classification(store: InventoryStore, probe: FakeProbe, observation: ProbeObservation,
                              state: ConnectionState, code: str) -> None:
    profile = enroll(store, probe, roles=["HERDR_MACHINE"], herdr_enabled=True, herdr_ref="herdr://default")
    assert classify(profile, observation) == (state, code)


def test_host_key_confirmation_is_required(store: InventoryStore, probe: FakeProbe) -> None:
    key = host_key("mac")
    probe.place(lan(20), key)
    shown = scan_host_key(lan(20), probe=probe)
    assert shown == {"host_key_fingerprint": key.fingerprint, "key_type": "ssh-ed25519",
                     "machine_id": key.machine_id}
    with pytest.raises(MachineError, match="HOST_KEY_CONFIRMATION_REQUIRED"):
        add_machine("Mac", lan(20), "operator", "ssh-agent", store=store, probe=probe, now=NOW)
    with pytest.raises(MachineError, match="HOST_KEY_MISMATCH"):
        add_machine("Mac", lan(20), "operator", "ssh-agent", store=store, probe=probe, now=NOW,
                    expected_fingerprint=host_key("other").fingerprint)
    profile = add_machine("Mac", lan(20), "operator", "ssh-agent", store=store, probe=probe, now=NOW,
                          accept_new_host_key=True, platform="MACOS")
    assert profile.machine_id == key.machine_id and profile.connection_state is ConnectionState.ONLINE


def test_spoofed_host_at_known_address_is_unauthorized_and_never_reenrolled(
        store: InventoryStore, probe: FakeProbe, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    profile = enroll(store, probe)
    probe.place(lan(20), host_key("attacker"))
    result = check_machine(store, "Mac", probe, now=NOW + timedelta(minutes=1))
    assert (result.connection_state, result.code) == (ConnectionState.UNAUTHORIZED, "HOST_KEY_MISMATCH")
    stored = store.load()
    assert [p.machine_id for p in stored.machines] == [profile.machine_id]
    assert stored.machines[0].host_public_key == profile.host_public_key
    assert stored.machines[0].identity_alert is not None
    # The real machine comes back: the alert is sticky until a human acts.
    probe.place(lan(20), host_key("mac"))
    later = check_machine(store, "Mac", probe, now=NOW + timedelta(minutes=2))
    assert (later.connection_state, later.code) == (ConnectionState.UNAUTHORIZED, "HOST_KEY_MISMATCH")
    with pytest.raises(MachineError, match="HOST_KEY_CONFIRMATION_MISMATCH"):
        acknowledge_host_key_alert(store, "Mac", confirmed_fingerprint=host_key("attacker").fingerprint, probe=probe)
    cleared = acknowledge_host_key_alert(store, "Mac", confirmed_fingerprint=profile.host_key_fingerprint,
                                         probe=probe, now=NOW + timedelta(minutes=3))
    assert cleared.identity_alert is None and cleared.connection_state is ConnectionState.ONLINE
    assert "HOST_KEY_MISMATCH" in caplog.text


def test_same_host_key_at_new_address_is_the_same_machine(store: InventoryStore, probe: FakeProbe) -> None:
    profile = enroll(store, probe)
    probe.keys.clear()
    probe.place(lan(77), host_key("mac"))
    moved = connection.update_location(store, profile.machine_id, lan(77), probe, now=NOW + timedelta(hours=1))
    assert moved.machine_id == profile.machine_id and moved.host == lan(77)
    assert moved.connection_state is ConnectionState.ONLINE
    assert len(store.load().machines) == 1


def test_different_key_at_new_address_is_refused_as_a_move(store: InventoryStore, probe: FakeProbe) -> None:
    profile = enroll(store, probe)
    probe.place(lan(78), host_key("stranger"))
    with pytest.raises(MachineError, match="HOST_KEY_MISMATCH"):
        connection.update_location(store, profile.machine_id, lan(78), probe)
    assert store.load().get(profile.machine_id).host == lan(20)


def test_human_repin_after_reinstall_creates_a_new_identity(store: InventoryStore, probe: FakeProbe) -> None:
    old = enroll(store, probe, roles=["HOME_BASE"])
    new_key = host_key("mac-reinstalled")
    probe.place(lan(20), new_key)
    with pytest.raises(MachineError, match="HOST_KEY_CONFIRMATION_MISMATCH"):
        repin_machine(store, "Mac", confirmed_fingerprint=old.host_key_fingerprint, probe=probe)
    new = repin_machine(store, "Mac", confirmed_fingerprint=new_key.fingerprint, probe=probe,
                        now=NOW + timedelta(days=1))
    assert new.machine_id == new_key.machine_id != old.machine_id
    assert [p.machine_id for p in store.load().machines] == [new.machine_id]
    assert new.connection_state is ConnectionState.ONLINE and new.roles == old.roles


def test_probe_failures_are_offline_codes_without_exception_text(store: InventoryStore, probe: FakeProbe,
                                                                  caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    enroll(store, probe)
    probe.fail = True
    result = check_machine(store, "Mac", probe, now=NOW + timedelta(minutes=1))
    assert (result.connection_state, result.code) == (ConnectionState.OFFLINE, "MACHINE_UNREACHABLE")
    assert "must not leak" not in caplog.text
    with pytest.raises(MachineError, match="MACHINE_UNREACHABLE") as caught:
        scan_host_key(lan(20), probe=probe)
    assert "must not leak" not in str(caught.value)


def test_auth_failure_and_last_seen(store: InventoryStore, probe: FakeProbe) -> None:
    profile = enroll(store, probe)
    probe.auth_ok = False
    result = check_machine(store, profile.machine_id, probe, now=NOW + timedelta(minutes=5))
    assert result.connection_state is ConnectionState.AUTH_REQUIRED
    assert store.load().get(profile.machine_id).last_seen == NOW + timedelta(minutes=5)
    probe.keys.clear()
    check_machine(store, profile.machine_id, probe, now=NOW + timedelta(minutes=9))
    after = store.load().get(profile.machine_id)
    assert after.connection_state is ConnectionState.OFFLINE
    assert after.last_seen == NOW + timedelta(minutes=5)


def test_probe_targets_come_only_from_the_inventory(store: InventoryStore, probe: FakeProbe) -> None:
    enroll(store, probe)
    with pytest.raises(MachineError, match="MACHINE_NOT_FOUND"):
        check_machine(store, "169.254.169.254", probe)
    with pytest.raises(MachineError, match="HOST_FORBIDDEN_RANGE"):
        add_machine("Metadata", "169.254.169.254", "operator", "ssh-agent", store=store, probe=probe,
                    accept_new_host_key=True)
    assert all(host != "169.254.169.254" for host, _ in probe.scans)


def test_stale_state_is_never_presented_live(store: InventoryStore, probe: FakeProbe) -> None:
    enroll(store, probe)
    fresh = machine_visibility(str(store.path), now=NOW + timedelta(seconds=60))["machines"][0]
    assert fresh["connection_state"] == "ONLINE" and fresh["stale"] is False
    old = machine_visibility(str(store.path), now=NOW + timedelta(seconds=301))["machines"][0]
    assert old["connection_state"] == "STALE" and old["last_known_state"] == "ONLINE" and old["stale"] is True
    future = machine_visibility(str(store.path), now=NOW - timedelta(hours=1))["machines"][0]
    assert future["stale"] is True and future["connection_state"] == "STALE"


def test_unauthorized_stays_visible_when_stale(store: InventoryStore, probe: FakeProbe) -> None:
    enroll(store, probe)
    probe.place(lan(20), host_key("attacker"))
    check_machine(store, "Mac", probe, now=NOW)
    row = machine_visibility(str(store.path), now=NOW + timedelta(days=2))["machines"][0]
    assert row["connection_state"] == "UNAUTHORIZED" and row["stale"] is True
    assert row["identity_alert"] == "HOST_KEY_MISMATCH"


class RacingProbe(FakeProbe):
    """Changes the profile under the probe (a concurrent operator edit) while the observation is in flight."""

    def __init__(self, store: InventoryStore, change: dict[str, object]) -> None:
        super().__init__()
        self.store, self.change, self.armed = store, change, False

    def check(self, profile: MachineProfile, known_hosts: Path) -> ProbeObservation:
        observation = super().check(profile, known_hosts)
        if self.armed:
            self.armed = False

            def edit(current: MachineInventory) -> MachineInventory:
                record = current.get(profile.machine_id).model_dump()
                record.update(self.change)
                return current.replacing(validated(MachineProfile, record, prefix="MACHINE_PROFILE"))

            self.store.update(edit)
        return observation


@pytest.mark.parametrize("change", [{"auth_method": "SSH_KEY", "auth_ref": "secretref://env/PADOS_OTHER_KEY"},
                                    {"username": "someone-else"}, {"port": 2222}])
def test_cr07_mc08_observation_of_an_old_revision_is_discarded(store: InventoryStore, change: dict[str, object],
                                                               caplog: pytest.LogCaptureFixture) -> None:
    racing = RacingProbe(store, change)
    profile = enroll(store, racing)
    racing.auth_ok = False
    check_machine(store, "Mac", racing, now=NOW + timedelta(minutes=1))  # AUTH_REQUIRED, no race
    racing.auth_ok, racing.armed = True, True
    with pytest.raises(MachineError, match="OBSERVATION_STALE_REVISION"):
        check_machine(store, "Mac", racing, now=NOW + timedelta(minutes=2))
    stored = store.load().get(profile.machine_id)
    assert stored.connection_state is ConnectionState.AUTH_REQUIRED  # never stamped ONLINE
    assert stored.state_observed_at == NOW + timedelta(minutes=1)
    assert profile_revision(stored) != profile_revision(profile)
    assert "OBSERVATION_STALE_REVISION" in caplog.text


def test_cr07_mc08_location_move_is_compare_and_set(store: InventoryStore) -> None:
    racing = RacingProbe(store, {"auth_method": "SSH_KEY", "auth_ref": "secretref://env/PADOS_OTHER_KEY"})
    profile = enroll(store, racing)
    racing.place(lan(77), host_key("mac"))
    racing.armed = True
    with pytest.raises(MachineError, match="OBSERVATION_STALE_REVISION"):
        connection.update_location(store, profile.machine_id, lan(77), racing, now=NOW + timedelta(hours=1))
    assert store.load().get(profile.machine_id).host == lan(20)


def test_cr07_mc08_revision_covers_location_account_credential_and_key(store: InventoryStore,
                                                                      probe: FakeProbe) -> None:
    profile = enroll(store, probe)
    base = profile_revision(profile)
    record = profile.model_dump()
    record.update(connection_state="OFFLINE", state_observed_at=NOW + timedelta(days=1))
    assert profile_revision(validated(MachineProfile, record, prefix="MACHINE_PROFILE")) == base


def test_cr07_mc07_describe_and_machine_show_never_present_stale_state_as_live(
        store: InventoryStore, probe: FakeProbe, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = enroll(store, probe)
    fresh = describe(profile, now=NOW + timedelta(seconds=30))
    assert "State: ONLINE (last seen" in fresh
    stale = describe_machine(store, "Mac", now=NOW + timedelta(seconds=301))
    assert "State: STALE (last known ONLINE" in stale and "State: ONLINE" not in stale
    monkeypatch.setenv("PADOS_MACHINES_CONFIG", str(store.path))
    get_settings.cache_clear()
    try:
        shown = CliRunner().invoke(app, ["machine", "show", "Mac"])
    finally:
        get_settings.cache_clear()
    assert shown.exit_code == 0 and "State: STALE (last known ONLINE" in shown.output
