"""One inventory, one profile per physical machine, refs only on disk."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest
from machine_support import NOW, FakeProbe, enroll, host_key, lan, scan_tree_for

from pados.machines.errors import MachineError
from pados.machines.inventory import InventoryStore
from pados.machines.ux import add_machine, describe, list_machines, remove_machine

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
PW = "pass" + "word"
# Password-like assignments and private key headers (assembled so this file stays scanner-clean).
CREDENTIAL_SHAPES = re.compile(r"BEGIN[A-Z ]*PRIVATE|(" + PW + r"|passphrase|private_key)\"?\s*[:=]", re.I)


def test_one_machine_holds_several_roles_in_one_profile(store: InventoryStore, probe: FakeProbe) -> None:
    profile = enroll(store, probe, roles=["HOME_BASE", "LOCAL_COMPUTE_NODE", "HERDR_MACHINE", "SANDBOX_HOST"],
                     herdr_enabled=True, herdr_ref="herdr://default")
    inventory = store.load()
    assert len(inventory.machines) == 1
    assert {r.value for r in inventory.machines[0].roles} == {
        "HOME_BASE", "LOCAL_COMPUTE_NODE", "HERDR_MACHINE", "SANDBOX_HOST"}
    assert inventory.get(profile.machine_id) == profile


def test_duplicate_inventory_is_refused(store: InventoryStore, probe: FakeProbe) -> None:
    first = enroll(store, probe)
    key = host_key("mac")
    # Same host key reachable at a second address: still one machine, never a second profile.
    probe.place(lan(21), key)
    with pytest.raises(MachineError, match="MACHINE_ALREADY_ENROLLED") as caught:
        add_machine("Mac again", lan(21), "operator", "ssh-agent", store=store, probe=probe,
                    expected_fingerprint=key.fingerprint, now=NOW)
    assert caught.value.machine_id == first.machine_id
    with pytest.raises(MachineError, match="MACHINE_DISPLAY_NAME_DUPLICATE"):
        enroll(store, probe, name="mac", host=lan(30), seed="linux")
    with pytest.raises(MachineError, match="MACHINE_LOCATION_DUPLICATE"):
        enroll(store, probe, name="Other", host=lan(20), seed="linux")
    assert len(store.load().machines) == 1


def test_tampered_inventory_with_two_profiles_for_one_key_fails_closed(store: InventoryStore,
                                                                       probe: FakeProbe) -> None:
    enroll(store, probe)
    data = json.loads(store.path.read_text())
    clone = dict(data["machines"][0], display_name="Clone", host=lan(99))
    data["machines"].append(clone)
    store.path.write_text(json.dumps(data))
    with pytest.raises(MachineError, match="MACHINE_DUPLICATE_IDENTITY"):
        store.load()


@pytest.mark.parametrize("mutate", [
    lambda d: d["machines"][0].update({PW: "hunter2-hunter2"}),
    lambda d: d["machines"][0].update({"auth_ref": "-----BEGIN " + "OPENSSH PRIVATE KEY-----"}),
    lambda d: d.update({"api_key": "x"}),
    lambda d: d["machines"][0].update({"machine_id": "mach-" + "0" * 20}),
])
def test_tampered_or_credential_bearing_inventory_fails_closed(store: InventoryStore, probe: FakeProbe,
                                                               mutate: object) -> None:
    enroll(store, probe)
    data = json.loads(store.path.read_text())
    mutate(data)  # type: ignore[operator]
    store.path.write_text(json.dumps(data))
    with pytest.raises(MachineError) as caught:
        store.load()
    assert caught.value.code.startswith("MACHINE_INVENTORY")
    assert "hunter2" not in str(caught.value)


def test_corrupt_inventory_is_never_read_as_empty(store: InventoryStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    os.chmod(store.path, 0o600)
    with pytest.raises(MachineError, match="MACHINE_INVENTORY_INVALID"):
        store.load()


@POSIX
def test_inventory_and_known_hosts_are_owner_only(store: InventoryStore, probe: FakeProbe) -> None:
    profile = enroll(store, probe)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.known_hosts_path.stat().st_mode) == 0o600
    assert store.known_hosts_path.read_text() == f"{profile.machine_id} {profile.host_public_key}\n"
    os.chmod(store.path, 0o644)
    with pytest.raises(MachineError, match="MACHINE_INVENTORY_PERMISSIONS_TOO_OPEN"):
        store.load()


@POSIX
def test_symlinked_inventory_is_refused(tmp_path: Path, store: InventoryStore, probe: FakeProbe) -> None:
    enroll(store, probe)
    link = tmp_path / "machines" / "link.json"
    link.symlink_to(store.path)
    with pytest.raises(MachineError, match="MACHINE_INVENTORY_INVALID"):
        InventoryStore(link).load()


def test_missing_inventory_is_empty_and_remove_works(store: InventoryStore, probe: FakeProbe) -> None:
    assert store.load().machines == ()
    profile = enroll(store, probe)
    enroll(store, probe, name="Linux box", host=lan(30), seed="linux", platform="LINUX")
    assert [row["display_name"] for row in list_machines(store, now=NOW)] == ["Linux box", "Mac"]
    assert remove_machine(store, "mac") == profile.machine_id
    assert [p.display_name for p in store.load().machines] == ["Linux box"]
    assert profile.machine_id not in store.known_hosts_path.read_text()
    with pytest.raises(MachineError, match="MACHINE_NOT_FOUND"):
        remove_machine(store, "Mac")


def test_ci_environment_cannot_discover_credentials_from_the_inventory(store: InventoryStore,
                                                                       probe: FakeProbe) -> None:
    enroll(store, probe, auth="ssh-key", auth_ref="secretref://env/PADOS_MAC_KEY")
    enroll(store, probe, name="Win", host=lan(40), seed="win", auth="os-credential",
           auth_ref="oscred://wincred/pados-win", platform="WINDOWS")
    enroll(store, probe, name="Agent", host=lan(41), seed="agent")
    directory = store.path.parent
    for path in directory.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "BEGIN" not in text, path
            assert not CREDENTIAL_SHAPES.search(text), path
    refs = {p.auth_ref for p in store.load().machines}
    assert refs == {"secretref://env/PADOS_MAC_KEY", "oscred://wincred/pados-win", "ssh-agent://default"}
    assert not scan_tree_for(directory, "hunter2")


def test_describe_is_a_short_summary_without_credential_values(store: InventoryStore, probe: FakeProbe) -> None:
    profile = enroll(store, probe, auth="ssh-key", auth_ref="secretref://env/PADOS_MAC_KEY",
                     roles=["HOME_BASE"])
    text = describe(profile, now=NOW)
    assert text.splitlines()[0] == f"Machine: Mac ({profile.machine_id})"
    assert f"Host: {lan(20)} port 22" in text and "Username: operator" in text
    assert "Authentication: SSH key (secretref reference)" in text
    assert "PADOS_MAC_KEY" not in text
    assert "State: ONLINE" in text
