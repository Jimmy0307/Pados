"""One-time password bootstrap: the password authenticates once and is never persisted."""
from __future__ import annotations

import gc
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

import pytest
from machine_support import (
    NOW,
    FakeProbe,
    FakeTransport,
    host_key,
    lan,
    one_time_password,
    scan_tree_for,
    user_public_key,
)

from pados.enterprise.secrets import SecretValue
from pados.machines.bootstrap import BootstrapDraft, bootstrap
from pados.machines.errors import MachineError
from pados.machines.inventory import InventoryStore
from pados.machines.profile import AuthMethod, ConnectionState, validated
from pados.machines.ux import add_machine, describe
from pados.machines.visibility import machine_visibility


def draft(**overrides: Any) -> BootstrapDraft:
    data: dict[str, Any] = {
        "display_name": "Mac", "host": lan(20), "username": "operator", "platform": "MACOS",
        "roles": ["HOME_BASE"], "host_public_key": host_key("mac").openssh(), "key_auth_method": "SSH_AGENT",
        "auth_ref": "ssh-agent://default", "public_key": user_public_key(),
    }
    data.update(overrides)
    return validated(BootstrapDraft, data, prefix="BOOTSTRAP_DRAFT")


def bootstrap_via_ux(store: InventoryStore, probe: FakeProbe, transport: FakeTransport, otp: str) -> Any:
    probe.place(lan(20), host_key("mac"))
    return add_machine("Mac", lan(20), "operator", "password-bootstrap", store=store, probe=probe,
                       expected_fingerprint=host_key("mac").fingerprint, platform="MACOS",
                       one_time_password=SecretValue(otp), transport=transport, public_key=user_public_key(),
                       key_auth="ssh-agent", now=NOW)


def test_bootstrap_installs_public_key_and_stores_only_a_key_reference(
        tmp_path: Path, store: InventoryStore, probe: FakeProbe, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    otp = one_time_password()
    transport = FakeTransport(presented=host_key("mac"))
    profile = bootstrap_via_ux(store, probe, transport, otp)
    assert transport.installed == [user_public_key()]
    assert profile.auth_method is AuthMethod.PASSWORD_BOOTSTRAP_COMPLETED
    assert profile.effective_auth_method is AuthMethod.SSH_AGENT
    assert profile.auth_ref == "ssh-agent://default"
    assert profile.connection_state is ConnectionState.ONLINE
    assert store.load().get(profile.machine_id) == profile
    assert scan_tree_for(tmp_path, otp) == []
    assert otp not in caplog.text
    assert otp not in describe(profile) and otp not in repr(profile)
    assert otp not in str(machine_visibility(str(store.path), now=NOW))
    assert "password was not stored" in describe(profile)


@pytest.mark.parametrize("failure", ["hostile_transport", "login_fails", "host_key_mismatch", "unreachable"])
def test_password_never_appears_in_files_logs_or_exceptions(
        tmp_path: Path, store: InventoryStore, probe: FakeProbe, caplog: pytest.LogCaptureFixture,
        failure: str) -> None:
    caplog.set_level(logging.DEBUG)
    otp = one_time_password()
    transport = FakeTransport(presented=host_key("mac"), hostile=failure == "hostile_transport",
                              login_ok=failure != "login_fails")
    if failure == "host_key_mismatch":
        transport.presented = host_key("attacker")
    if failure == "unreachable":
        transport.presented = None
    with pytest.raises(MachineError) as caught:
        bootstrap_via_ux(store, probe, transport, otp)
    expected = {"hostile_transport": "BOOTSTRAP_KEY_INSTALL_FAILED", "login_fails": "BOOTSTRAP_KEY_LOGIN_FAILED",
                "host_key_mismatch": "HOST_KEY_MISMATCH", "unreachable": "MACHINE_UNREACHABLE"}[failure]
    assert caught.value.code == expected
    error = caught.value
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert otp not in rendered and otp not in str(error) and otp not in repr(error)
    assert error.__cause__ is None and error.__context__ is None
    assert otp not in caplog.text
    assert scan_tree_for(tmp_path, otp) == []
    assert store.load().machines == ()
    if failure in {"host_key_mismatch", "unreachable"}:
        assert transport.password_seen is False  # never sent to an unverified host


def test_bootstrap_keeps_no_reference_to_the_password() -> None:
    otp = SecretValue(one_time_password())
    before = sys.getrefcount(otp)
    profile = bootstrap(draft(), otp, FakeTransport(presented=host_key("mac")), now=NOW)
    gc.collect()
    # Nothing bootstrap returned or kept still refers to the password object.
    assert sys.getrefcount(otp) == before
    assert not [holder for holder in gc.get_referrers(otp) if getattr(holder, "f_code", None) is None]
    assert profile.auth_method is AuthMethod.PASSWORD_BOOTSTRAP_COMPLETED


def test_password_must_be_a_secret_value_and_repr_is_redacted() -> None:
    otp = one_time_password()
    with pytest.raises(MachineError, match="BOOTSTRAP_PASSWORD_MUST_BE_SECRET_VALUE") as caught:
        bootstrap(draft(), otp, FakeTransport(presented=host_key("mac")))  # type: ignore[arg-type]
    assert otp not in str(caught.value)
    assert otp not in repr(SecretValue(otp)) and otp not in str(SecretValue(otp))


@pytest.mark.parametrize(("overrides", "code"), [
    ({"public_key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----"}, "BOOTSTRAP_DRAFT_CREDENTIAL_SHAPED_VALUE"),
    ({"public_key": "ssh-ed25519 AAAA PRIVATE"}, "PUBLIC_KEY_INVALID"),
    ({"public_key": "ssh-ed25519 notbase64!"}, "PUBLIC_KEY_INVALID"),
    ({"key_auth_method": "OS_CREDENTIAL_REF", "auth_ref": "oscred://keychain/x"},
     "BOOTSTRAP_REQUIRES_KEY_AUTH_METHOD"),
    ({"key_auth_method": "SSH_KEY", "auth_ref": "ssh-agent://default"}, "AUTH_REF_MUST_BE_REFERENCE"),
    ({"pass" + "word": "x"}, "BOOTSTRAP_DRAFT_FIELD_NOT_IN_SCHEMA"),
])
def test_bootstrap_draft_refuses_private_material(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(MachineError, match=code):
        draft(**overrides)


def test_password_is_refused_for_key_based_auth(store: InventoryStore, probe: FakeProbe) -> None:
    probe.place(lan(20), host_key("mac"))
    with pytest.raises(MachineError, match="PASSWORD_ONLY_FOR_PASSWORD_BOOTSTRAP"):
        add_machine("Mac", lan(20), "operator", "ssh-agent", store=store, probe=probe, accept_new_host_key=True,
                    one_time_password=SecretValue(one_time_password()))


def _leaks(value: object, otp: str, depth: int = 0) -> bool:
    if isinstance(value, SecretValue):
        return value.reveal() == otp
    if isinstance(value, str):
        return otp in value
    if depth < 3 and isinstance(value, (list, tuple, set, frozenset)):
        return any(_leaks(item, otp, depth + 1) for item in value)
    if depth < 3 and isinstance(value, dict):
        return any(_leaks(item, otp, depth + 1) for item in value.values())
    return False


def _pados_frames_holding(error: BaseException, otp: str) -> list[str]:
    """Names of pados.machines frames in the traceback whose locals still reach the password."""
    found = []
    tb = error.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        source = Path(frame.f_code.co_filename)
        if (source.parent.name, source.parent.parent.name) == ("machines", "pados"):
            if any(_leaks(value, otp) for value in frame.f_locals.values()):
                found.append(frame.f_code.co_name)
        tb = tb.tb_next
    return found


@pytest.mark.parametrize("failure", ["hostile_transport", "login_fails", "host_key_mismatch"])
def test_cr07_mc05_no_traceback_frame_retains_the_password(store: InventoryStore, probe: FakeProbe,
                                                          failure: str) -> None:
    otp = one_time_password()
    transport = FakeTransport(presented=host_key("attacker" if failure == "host_key_mismatch" else "mac"),
                              hostile=failure == "hostile_transport", login_ok=failure != "login_fails")
    with pytest.raises(MachineError) as caught:
        bootstrap_via_ux(store, probe, transport, otp)
    assert _pados_frames_holding(caught.value, otp) == []
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert not [f for f in traceback.extract_tb(caught.value.__traceback__) if otp in (f.line or "")]


def test_cr07_mc05_direct_bootstrap_failures_keep_no_password_in_frames() -> None:
    otp = one_time_password()
    for transport in (FakeTransport(presented=host_key("mac"), hostile=True), FakeTransport(presented=None)):
        with pytest.raises(MachineError) as caught:
            bootstrap(draft(), SecretValue(otp), transport, now=NOW)
        assert _pados_frames_holding(caught.value, otp) == []
    with pytest.raises(MachineError, match="BOOTSTRAP_PASSWORD_MUST_BE_SECRET_VALUE") as plain:
        bootstrap(draft(), otp, FakeTransport(presented=host_key("mac")))  # type: ignore[arg-type]
    assert _pados_frames_holding(plain.value, otp) == []


def test_cr07_mc05_validation_failures_do_not_keep_raw_input_in_frames() -> None:
    marker = "raw-input-" + one_time_password()
    with pytest.raises(MachineError) as caught:
        draft(username="-" + marker)
    assert _pados_frames_holding(caught.value, marker) == []
