"""Identity derivation, profile schema and input refusal (SH-M01)."""
from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest
from machine_support import NOW, host_key, lan, overlay

from pados.machines.errors import MachineError
from pados.machines.identity import HostKey, derive_machine_id, is_fingerprint, is_machine_id
from pados.machines.profile import PROFILE_KEYS, AuthMethod, MachineProfile, validated
from pados.machines.targets import validate_machine_host

PW = "pass" + "word"
PRIVATE_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


def profile_data(**overrides: Any) -> dict[str, Any]:
    key = host_key("mac")
    data: dict[str, Any] = {
        "machine_id": key.machine_id, "display_name": "Mac", "host": lan(20), "port": 22, "username": "operator",
        "auth_method": "SSH_AGENT", "auth_ref": "ssh-agent://default", "platform": "MACOS",
        "roles": ["HOME_BASE", "HERDR_MACHINE"], "host_public_key": key.openssh(),
        "host_key_fingerprint": key.fingerprint, "enrolled_at": NOW.isoformat(), "connection_state": "ONLINE",
        "state_observed_at": NOW.isoformat(), "last_seen": NOW.isoformat(),
    }
    data.update(overrides)
    return data


def build(**overrides: Any) -> MachineProfile:
    return validated(MachineProfile, profile_data(**overrides), prefix="MACHINE_PROFILE")


def test_machine_id_is_derived_from_the_host_key_blob_only() -> None:
    key = host_key("mac")
    expected = "mach-" + hashlib.sha256(b"pados-machine-identity\n" + key.blob).hexdigest()[:20]
    assert key.machine_id == derive_machine_id(key.blob) == expected
    assert is_machine_id(key.machine_id)
    digest = base64.b64encode(hashlib.sha256(key.blob).digest()).decode().rstrip("=")
    assert key.fingerprint == f"SHA256:{digest}" and is_fingerprint(key.fingerprint)
    assert HostKey.from_openssh(key.openssh() + " root@mac") == key
    assert host_key("other").machine_id != key.machine_id


@pytest.mark.parametrize("line", [
    "ssh-dss AAAAB3NzaC1kc3MAAACBAP", "ssh-ed25519 not*base64", "ssh-ed25519", "",
    "ssh-rsa " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"\x00" * 36).decode(),
    PRIVATE_HEADER,
])
def test_invalid_or_private_host_keys_are_refused(line: str) -> None:
    with pytest.raises(MachineError):
        HostKey.from_openssh(line)


def test_valid_profile_with_several_roles() -> None:
    profile = build(herdr={"enabled": True, "ref": "herdr://default"})
    assert [r.value for r in profile.roles] == ["HOME_BASE", "HERDR_MACHINE"]
    assert profile.effective_auth_method is AuthMethod.SSH_AGENT


def test_there_is_no_password_field_anywhere_in_the_schema() -> None:
    lowered = {key.replace("_", "").lower() for key in PROFILE_KEYS}
    assert not any(word in key for key in lowered for word in ("password", "passphrase", "privatekey", "secret"))


@pytest.mark.parametrize("extra", [
    {PW: "hunter2-hunter2"},
    {"pass_phrase": "x"},
    {"private_key": "abc"},
    {"herdr": {"enabled": False, "ref": None, "passphrase": "x"}},
    {"display_name": PW + "=hunter2hunter2"},
    {"auth_ref": PRIVATE_HEADER},
    {"unknown_field": 1},
])
def test_password_passphrase_and_private_key_material_is_refused(extra: dict[str, Any]) -> None:
    with pytest.raises(MachineError) as caught:
        build(**extra)
    assert "hunter2" not in str(caught.value) and "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize(("method", "ref", "ok"), [
    ("SSH_KEY", "secretref://env/PADOS_MAC_KEY", True),
    ("SSH_KEY", "ssh-agent://default", False),
    ("SSH_KEY", "/keys/id_ed25519", False),
    ("SSH_AGENT", "ssh-agent://default", True),
    ("SSH_AGENT", "secretref://env/X", False),
    ("OS_CREDENTIAL_REF", "oscred://keychain/pados-mac", True),
    ("OS_CREDENTIAL_REF", "oscred://keychain/../x", False),
    ("PASSWORD_BOOTSTRAP_COMPLETED", "ssh-agent://default", False),
])
def test_auth_ref_must_be_a_reference_of_the_method_scheme(method: str, ref: str, ok: bool) -> None:
    if ok:
        assert build(auth_method=method, auth_ref=ref).auth_ref == ref
    else:
        with pytest.raises(MachineError):
            build(auth_method=method, auth_ref=ref)


def test_bootstrap_completed_profile_is_effectively_key_based() -> None:
    profile = build(auth_method="PASSWORD_BOOTSTRAP_COMPLETED", key_auth_method="SSH_KEY",
                    auth_ref="secretref://env/PADOS_MAC_KEY")
    assert profile.effective_auth_method is AuthMethod.SSH_KEY
    with pytest.raises(MachineError, match="KEY_AUTH_METHOD_ONLY_AFTER_BOOTSTRAP"):
        build(key_auth_method="SSH_AGENT")


def test_identity_tampering_is_refused() -> None:
    with pytest.raises(MachineError, match="MACHINE_IDENTITY_INCONSISTENT"):
        build(machine_id=host_key("other").machine_id)
    with pytest.raises(MachineError, match="MACHINE_IDENTITY_INCONSISTENT"):
        build(host_key_fingerprint=host_key("other").fingerprint)


@pytest.mark.parametrize(("overrides", "code"), [
    ({"roles": ["HOME_BASE", "HOME_BASE"]}, "MACHINE_ROLE_DUPLICATE"),
    ({"roles": [], "herdr": {"enabled": True, "ref": "herdr://default"}}, "HERDR_REQUIRES_HERDR_MACHINE_ROLE"),
    ({"compute_node_id": "lcn-" + "a" * 20}, "COMPUTE_NODE_LINK_REQUIRES_LOCAL_COMPUTE_NODE_ROLE"),
    ({"identity_alert": "HOST_KEY_MISMATCH"}, "IDENTITY_ALERT_REQUIRES_UNAUTHORIZED"),
    ({"herdr": {"enabled": True}}, "HERDR_REF_REQUIRED"),
    ({"herdr": {"enabled": False, "ref": "x://a b"}}, "HERDR_REF_INVALID"),
    ({"display_name": "Mac\u202e"}, "DISPLAY_NAME_INVALID"),
    ({"username": "-oProxyCommand=x"}, "USERNAME_INVALID"),
    ({"port": 0}, "PORT_INVALID"),
])
def test_profile_consistency_rules(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(MachineError, match=code):
        build(**overrides)


@pytest.mark.parametrize(("host", "code"), [
    ("169.254.169.254", "HOST_FORBIDDEN_RANGE"),
    ("224.0.0.1", "HOST_FORBIDDEN_RANGE"),
    ("0.0.0.0", "HOST_FORBIDDEN_RANGE"),
    ("255.255.255.255", "HOST_FORBIDDEN_RANGE"),
    ("127.0.0.1", "HOST_LOOPBACK_FORBIDDEN"),
    ("::1", "HOST_LOOPBACK_FORBIDDEN"),
    ("fe80::1", "HOST_FORBIDDEN_RANGE"),
    ("::ffff:c0a8:0114", "HOST_FORBIDDEN_RANGE"),
    ("8.8.8.8", "HOST_MUST_BE_PRIVATE_LAN_OR_OVERLAY"),
    ("localhost", "HOST_LOOPBACK_FORBIDDEN"),
    ("10.1", "HOST_NUMERIC_FORM_FORBIDDEN"),
    ("0x7f000001", "HOST_NUMERIC_FORM_FORBIDDEN"),
    ("-oProxyCommand", "HOST_INVALID"),
    ("mac.lan", "HOSTNAME_REQUIRES_OPERATOR_CONFIRMATION"),
])
def test_connection_targets_are_private_literals_or_confirmed_hostnames(host: str, code: str) -> None:
    with pytest.raises(MachineError, match=code):
        validate_machine_host(host, hostname_confirmed=False)


def test_allowed_targets() -> None:
    assert validate_machine_host(lan(20), hostname_confirmed=False) == lan(20)
    assert validate_machine_host(overlay(7), hostname_confirmed=False) == overlay(7)
    assert validate_machine_host("fd00::7", hostname_confirmed=False) == "fd00::7"
    assert validate_machine_host("Mac.LAN", hostname_confirmed=True) == "mac.lan"
    with pytest.raises(MachineError, match="HOST_NOT_CANONICAL"):
        build(host="Mac.LAN", hostname_confirmed=True)
    assert build(host="mac.lan", hostname_confirmed=True).host == "mac.lan"
