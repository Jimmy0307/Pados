"""Visibility projection and the compute-node link (backward compatible)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from machine_support import NOW, FakeProbe, enroll, lan

from pados.compute_node.registry import NodeRegistration, NodeRegistry
from pados.machines.inventory import InventoryStore, check_compute_nodes
from pados.machines.visibility import machine_visibility

ROW_KEYS = {"machine_id", "display_name", "roles", "platform", "connection_state", "last_seen", "stale", "herdr"}


def node(node_id: str, endpoint: str, machine_id: str | None = None) -> NodeRegistration:
    data = {"node_id": node_id, "endpoint": endpoint, "auth_ref": "secretref://env/PADOS_NODE_KEY"}
    if machine_id is not None:
        data["machine_id"] = machine_id
    return NodeRegistration.model_validate(data)


def test_visibility_shape_and_no_credentials(store: InventoryStore, probe: FakeProbe) -> None:
    enroll(store, probe, auth="ssh-key", auth_ref="secretref://env/PADOS_MAC_KEY",
           roles=["HOME_BASE", "HERDR_MACHINE"], herdr_enabled=True, herdr_ref="herdr://default")
    view = machine_visibility(str(store.path), now=NOW)
    assert view["status"] == "MACHINES_OBSERVED"
    assert view["plaintext_password_persistence"] == "PROHIBITED"
    assert view["semantic_authority"] == "NONE" and view["shell_exposed"] is False
    row = view["machines"][0]
    assert ROW_KEYS <= set(row)
    assert row["herdr"] == {"enabled": True, "ref_configured": True, "ref_scheme": "herdr"}
    assert row["auth_ref_scheme"] == "secretref"
    text = json.dumps(view)
    assert lan(20) not in text and "operator" not in text
    assert "PADOS_MAC_KEY" not in text and "herdr://default" not in text


def test_visibility_statuses(tmp_path: Path) -> None:
    assert machine_visibility("")["status"] == "MACHINES_NOT_CONFIGURED"
    assert machine_visibility(str(tmp_path / "none.json"))["status"] == "MACHINE_INVENTORY_EMPTY"
    broken = tmp_path / "broken.json"
    broken.write_text("[]")
    os.chmod(broken, 0o600)
    view = machine_visibility(str(broken))
    assert view["status"] == "MACHINE_INVENTORY_INVALID" and view["machines"] == []
    assert view["plaintext_password_persistence"] == "PROHIBITED"


def test_existing_node_registries_without_machine_id_load_unchanged(tmp_path: Path) -> None:
    registry_file = tmp_path / "nodes.json"
    registry_file.write_text(json.dumps({
        "schema_version": "pados.compute-nodes.registry.v1", "state_dir": str(tmp_path / "state"),
        "nodes": [{"node_id": "lcn-" + "a" * 20, "node_name": "gpu", "endpoint": "http://" + lan(50) + ":8765",
                   "auth_ref": "secretref://env/PADOS_NODE_KEY", "authorized": True}],
    }))
    registry = NodeRegistry.load(registry_file)
    assert registry.nodes[0].machine_id is None
    assert registry.nodes[0].endpoint == "http://" + lan(50) + ":8765"


@pytest.mark.parametrize("value", ["mach-XYZ", "lcn-" + "a" * 20, "mach-" + "a" * 21, ""])
def test_node_machine_id_is_validated(value: str) -> None:
    with pytest.raises(ValueError):
        node("lcn-" + "a" * 20, "http://" + lan(50) + ":8765", value)


def test_compute_node_consistency_findings(store: InventoryStore, probe: FakeProbe) -> None:
    gpu = enroll(store, probe, name="GPU", host=lan(50), seed="gpu", roles=["LOCAL_COMPUTE_NODE"],
                 compute_node_id="lcn-" + "a" * 20, platform="LINUX")
    plain = enroll(store, probe, name="Laptop", host=lan(51), seed="laptop")
    unknown = "mach-" + "f" * 20
    nodes = [
        node("lcn-" + "a" * 20, "http://" + lan(50) + ":8765", gpu.machine_id),
        node("lcn-" + "b" * 20, "http://" + lan(60) + ":8765", gpu.machine_id),
        node("lcn-" + "c" * 20, "http://" + lan(51) + ":8765", plain.machine_id),
        node("lcn-" + "d" * 20, "http://" + lan(52) + ":8765", unknown),
        node("lcn-" + "e" * 20, "http://" + lan(53) + ":8765"),
    ]
    findings = check_compute_nodes(store.load(), nodes)
    codes = {(f["code"], f["node_id"][:5]) for f in findings}
    assert codes == {
        ("MACHINE_CLAIMED_BY_CONFLICTING_NODES", "lcn-a"),
        ("MACHINE_CLAIMED_BY_CONFLICTING_NODES", "lcn-b"),
        ("COMPUTE_NODE_MACHINE_ROLE_MISSING", "lcn-c"),
        ("COMPUTE_NODE_MACHINE_UNKNOWN", "lcn-d"),
    }
    assert check_compute_nodes(store.load(), nodes[:1]) == []
    missing = check_compute_nodes(store.load(), [])
    assert [f["code"] for f in missing] == ["MACHINE_COMPUTE_NODE_LINK_UNKNOWN"]
    mismatch = check_compute_nodes(store.load(), [node("lcn-" + "a" * 20, "http://" + lan(50) + ":8765")])
    assert [f["code"] for f in mismatch] == ["MACHINE_COMPUTE_NODE_LINK_MISMATCH"]
