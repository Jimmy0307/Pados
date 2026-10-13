"""OpenSshProbe with injected runner/spawn/resolver (no process, no network, never real SSH)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from machine_support import NOW, host_key, lan, user_public_key

from pados.enterprise.secrets import SecretValue
from pados.machines.connection import AuthOutcome, classify
from pados.machines.errors import MachineError
from pados.machines.policy import CREDENTIAL_POLICY_SCHEMA, CredentialPolicy, agent_key_fingerprint
from pados.machines.profile import ConnectionState, MachineProfile, validated
from pados.machines.ssh import OpenSshProbe, classify_ssh_output, parse_keyscan

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
AUTH = "AUTH"  # FakeProcess: echo the nonce line from this run's LocalCommand, as OpenSSH would


def nonce_of(argv: list[str]) -> str:
    command = next(item for item in argv if item.startswith("LocalCommand="))
    return command.split("pados-auth-", 1)[1]


class Runner:
    """ssh-keyscan double."""

    def __init__(self, keyscan: str, fail: bool = False) -> None:
        self.keyscan, self.fail = keyscan, fail
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self.fail:
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, stdout=self.keyscan, stderr="")


class FakeProcess:
    """``ssh -N`` double: ``hang`` keeps running until killed, otherwise it exits with its output."""

    pid = 4242

    def __init__(self, stdout: bytes, stderr: bytes, *, hang: bool) -> None:
        self.stdout, self.stderr, self.hang, self.killed = stdout, stderr, hang, False

    def communicate(self, input: None = None, timeout: float | None = None) -> tuple[bytes | None, bytes | None]:
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(["ssh"], timeout or 0, output=self.stdout, stderr=self.stderr)
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


class Spawn:
    def __init__(self, stderr: bytes = b"", *, stdout: bytes | str = AUTH, hang: bool = True,
                 fail: bool = False) -> None:
        self.stderr, self.stdout, self.hang, self.fail = stderr, stdout, hang, fail
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, argv: list[str], env: dict[str, str]) -> FakeProcess:
        self.calls.append((argv, env))
        if self.fail:
            raise FileNotFoundError("ssh")
        stdout = f"pados-auth-{nonce_of(argv)}\n".encode() if self.stdout == AUTH else self.stdout
        assert isinstance(stdout, bytes)
        self.processes.append(FakeProcess(stdout, self.stderr, hang=self.hang))
        return self.processes[-1]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class KeyPathResolver:
    def resolve(self, ref: str) -> SecretValue | None:
        return SecretValue("keys/pados_mac_ed25519") if ref == "secretref://env/PADOS_MAC_KEY" else None


def profile(**overrides: Any) -> MachineProfile:
    key = host_key("mac")
    data: dict[str, Any] = {
        "machine_id": key.machine_id, "display_name": "Mac", "host": lan(20), "username": "operator",
        "auth_method": "SSH_KEY", "auth_ref": "secretref://env/PADOS_MAC_KEY", "platform": "MACOS",
        "host_public_key": key.openssh(), "host_key_fingerprint": key.fingerprint, "enrolled_at": NOW,
        "connection_state": "ONLINE", "state_observed_at": NOW,
    }
    data.update(overrides)
    return validated(MachineProfile, data, prefix="MACHINE_PROFILE")


def policy(**rule: Any) -> CredentialPolicy:
    agent_key = user_public_key("agent")
    base: dict[str, Any] = {"username": "operator",
                            "allowed_auth_refs": ["secretref://env/PADOS_MAC_KEY", "ssh-agent://default"],
                            "allowed_agent_key_fingerprints": [agent_key_fingerprint(agent_key)],
                            "agent_public_keys": [agent_key]}
    base.update(rule)
    return validated(CredentialPolicy, {"machines": {profile().machine_id: base}}, prefix="CREDENTIAL_POLICY")


def keyscan_output(*keys: Any) -> str:
    return "# banner\n" + "".join(f"{lan(20)} {k.openssh()}\n" for k in keys)


def probe(tmp_path: Path, *, keys: Any = None, spawn: Spawn | None = None, rules: CredentialPolicy | None = None,
          no_policy: bool = False, **options: Any) -> tuple[OpenSshProbe, Runner, Spawn]:
    runner = Runner(keyscan_output(*(keys or [host_key("mac")])), fail=options.pop("scan_fail", False))
    spawn = spawn or Spawn()
    made = OpenSshProbe(policy=None if no_policy else (rules or policy()), runner=runner, spawn=spawn,
                        secret_resolver=KeyPathResolver(), clock=Clock(), **options)
    return made, runner, spawn


def known_hosts(tmp_path: Path) -> Path:
    return tmp_path / "pados_known_hosts"


# MC-06: authentication only - no session channel, no remote command.
def test_cr07_mc06_connection_test_opens_no_session_and_runs_no_remote_command(tmp_path: Path) -> None:
    made, runner, spawn = probe(tmp_path)
    observation = made.check(profile(), known_hosts(tmp_path))
    assert observation.auth is AuthOutcome.AUTHENTICATED
    assert classify(profile(), observation) == (ConnectionState.ONLINE, "MACHINE_ONLINE")
    (argv, env), = spawn.calls
    assert argv[:4] == ["ssh", "-F", os.devnull, "-N"]
    assert argv[-2:] == ["--", lan(20)]  # nothing after the destination: no remote command, no "exit 0"
    joined = " ".join(argv)
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", f"UserKnownHostsFile={known_hosts(tmp_path)}",
                   f"HostKeyAlias={profile().machine_id}", "PasswordAuthentication=no",
                   "KbdInteractiveAuthentication=no", "ControlMaster=no", "RequestTTY=no",
                   "PermitLocalCommand=yes", "ForwardAgent=no"):
        assert option in joined
    assert "exit" not in argv and "-t" not in argv and "-tt" not in argv
    assert set(env) <= {"PATH", "SYSTEMROOT"}
    assert spawn.processes[0].killed  # terminated right after the authenticated line
    (scan_argv, scan_kwargs), = runner.calls
    assert scan_argv == ["ssh-keyscan", "-T", "10", "-p", "22", "-t", "ed25519,ecdsa,rsa", lan(20)]
    assert scan_kwargs["shell"] is False and scan_kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(("stderr", "hang", "state", "code"), [
    (b"Host key verification failed.\n", False, ConnectionState.UNAUTHORIZED, "HOST_KEY_MISMATCH"),
    (b"WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n", False, ConnectionState.UNAUTHORIZED,
     "HOST_KEY_MISMATCH"),
    (b"operator@x: Permission denied (publickey).\n", False, ConnectionState.AUTH_REQUIRED,
     "MACHINE_AUTH_REQUIRED"),
    (b"ssh: connect to host x port 22: Connection refused\n", False, ConnectionState.OFFLINE,
     "MACHINE_UNREACHABLE"),
    (b"something new in a future OpenSSH\n", False, ConnectionState.AUTH_REQUIRED, "SSH_OUTPUT_UNRECOGNIZED"),
    (b"debug1: still negotiating\n", True, ConnectionState.AUTH_REQUIRED, "SSH_DEADLINE_WITHOUT_AUTHENTICATION"),
])
def test_cr07_mc06_unknown_output_fails_closed_never_online(tmp_path: Path, stderr: bytes, hang: bool,
                                                            state: ConnectionState, code: str) -> None:
    spawn = Spawn(stderr, stdout=b"", hang=hang)
    made, _, _ = probe(tmp_path, spawn=spawn)
    assert classify(profile(), made.check(profile(), known_hosts(tmp_path))) == (state, code)
    if hang:
        assert spawn.processes[0].killed  # hard deadline


def test_cr07_mc06_output_classifier() -> None:
    nonce = "ab" * 16
    assert classify_ssh_output(b"pados-auth-" + nonce.encode() + b"\n", b"", nonce=nonce,
                               exited=False)[0] is AuthOutcome.AUTHENTICATED
    assert classify_ssh_output(b"", b"", nonce=nonce, exited=True)[3] == "SSH_OUTPUT_UNRECOGNIZED"


def test_mismatched_host_key_never_attempts_login(tmp_path: Path) -> None:
    made, runner, spawn = probe(tmp_path, keys=[host_key("attacker")])
    observation = made.check(profile(), known_hosts(tmp_path))
    assert spawn.calls == [] and len(runner.calls) == 1
    assert classify(profile(), observation) == (ConnectionState.UNAUTHORIZED, "HOST_KEY_MISMATCH")


def test_pinned_key_is_matched_among_several_presented_keys(tmp_path: Path) -> None:
    made, _, _ = probe(tmp_path, keys=[host_key("other-first"), host_key("mac")])
    assert made.check(profile(), known_hosts(tmp_path)).host_key == host_key("mac")


def test_timeouts_and_missing_client_are_offline(tmp_path: Path) -> None:
    made, _, _ = probe(tmp_path, spawn=Spawn(fail=True))
    assert classify(profile(), made.check(profile(), known_hosts(tmp_path)))[0] is ConnectionState.OFFLINE
    made, _, _ = probe(tmp_path, scan_fail=True)
    assert made.scan_host_key(lan(20), 22) is None


# MC-03: the inventory alone never activates a credential.
def test_cr07_mc03_no_policy_means_no_credential_use(tmp_path: Path) -> None:
    made, _, spawn = probe(tmp_path, no_policy=True)
    observation = made.check(profile(), known_hosts(tmp_path))
    assert classify(profile(), observation) == (ConnectionState.AUTH_REQUIRED, "CREDENTIAL_POLICY_REQUIRED")
    assert spawn.calls == []


@pytest.mark.parametrize(("overrides", "rule", "code"), [
    ({}, {"username": "root"}, "CREDENTIAL_POLICY_USERNAME_MISMATCH"),
    ({"auth_ref": "secretref://env/PADOS_OTHER_KEY"}, {}, "CREDENTIAL_POLICY_REF_NOT_ALLOWED"),
    ({"auth_method": "SSH_AGENT", "auth_ref": "ssh-agent://default"},
     {"agent_public_keys": [], "allowed_agent_key_fingerprints": []}, "CREDENTIAL_POLICY_AGENT_KEY_NOT_PINNED"),
])
def test_cr07_mc03_policy_binds_reference_to_machine_and_username(tmp_path: Path, overrides: dict[str, Any],
                                                                 rule: dict[str, Any], code: str) -> None:
    made, _, spawn = probe(tmp_path, rules=policy(**rule))
    observation = made.check(profile(**overrides), known_hosts(tmp_path))
    assert classify(profile(**overrides), observation) == (ConnectionState.AUTH_REQUIRED, code)
    assert spawn.calls == []


def test_cr07_mc03_policy_for_another_machine_does_not_apply(tmp_path: Path) -> None:
    other = validated(CredentialPolicy, {"machines": {host_key("other").machine_id: {
        "username": "operator", "allowed_auth_refs": ["secretref://env/PADOS_MAC_KEY"]}}},
        prefix="CREDENTIAL_POLICY")
    made, _, spawn = probe(tmp_path, rules=other)
    assert made.check(profile(), known_hosts(tmp_path)).error_code == "CREDENTIAL_POLICY_NO_RULE"
    assert spawn.calls == []


def test_cr07_mc03_key_mode_uses_only_the_policy_allowed_identity(tmp_path: Path) -> None:
    made, _, spawn = probe(tmp_path)
    made.check(profile(), known_hosts(tmp_path))
    argv = spawn.calls[0][0]
    assert "IdentityFile=keys/pados_mac_ed25519" in argv and "IdentitiesOnly=yes" in argv
    assert "IdentityAgent=none" in argv


def test_cr07_mc03_agent_mode_is_restricted_to_pinned_agent_keys(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    agent = profile(auth_method="SSH_AGENT", auth_ref="ssh-agent://default")
    made, _, spawn = probe(tmp_path)
    assert made.check(agent, known_hosts(tmp_path)).error_code == "SSH_AGENT_UNAVAILABLE"  # no agent socket
    monkeypatch.setenv("SSH_AUTH_SOCK", "agent.sock")
    assert made.check(agent, known_hosts(tmp_path)).auth is AuthOutcome.AUTHENTICATED
    argv, env = spawn.calls[-1]
    assert "IdentityAgent=agent.sock" in argv and "IdentitiesOnly=yes" in argv
    files = [item.split("=", 1)[1] for item in argv if item.startswith("IdentityFile=")]
    assert len(files) == 1 and Path(files[0]).read_text().strip() == user_public_key("agent")
    assert "SSH_AUTH_SOCK" not in env
    monkeypatch.setenv("SSH_AUTH_SOCK", "agent sock %d")
    assert made.check(agent, known_hosts(tmp_path)).error_code == "SSH_AGENT_SOCKET_INVALID"


def test_cr07_mc03_agent_keys_must_have_allowed_fingerprints() -> None:
    with pytest.raises(MachineError, match="CREDENTIAL_POLICY_AGENT_KEY_NOT_ALLOWED"):
        policy(allowed_agent_key_fingerprints=[host_key("x").fingerprint])


def test_cr07_mc03_policy_file_is_owner_only_and_closed_schema(tmp_path: Path) -> None:
    path = tmp_path / "authority" / "machine_credential_policy.json"
    path.parent.mkdir(mode=0o700)
    data = {"schema_version": CREDENTIAL_POLICY_SCHEMA, "machines": {profile().machine_id: {
        "username": "operator", "allowed_auth_refs": ["secretref://env/PADOS_MAC_KEY"]}}}
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    assert CredentialPolicy.load(path).decide(profile()).auth_ref == "secretref://env/PADOS_MAC_KEY"
    data["machines"][profile().machine_id]["pass" + "word"] = "x"  # type: ignore[index]
    path.write_text(json.dumps(data))
    with pytest.raises(MachineError, match="CREDENTIAL_POLICY_INVALID"):
        CredentialPolicy.load(path)
    with pytest.raises(MachineError, match="CREDENTIAL_POLICY_MISSING"):
        CredentialPolicy.load(tmp_path / "authority" / "absent.json")
    if sys.platform != "win32":
        path.write_text(json.dumps({**data, "machines": {}}))
        os.chmod(path, 0o644)
        with pytest.raises(MachineError, match="CREDENTIAL_POLICY_PERMISSIONS_TOO_OPEN"):
            CredentialPolicy.load(path)


def test_agent_unresolvable_and_os_credential_refs(tmp_path: Path) -> None:
    made, _, spawn = probe(tmp_path, rules=policy(allowed_auth_refs=[
        "secretref://env/PADOS_MISSING", "oscred://keychain/pados-mac"]))
    missing = profile(auth_ref="secretref://env/PADOS_MISSING")
    assert made.check(missing, known_hosts(tmp_path)).error_code == "AUTH_REF_UNAVAILABLE"
    oscred = profile(auth_method="OS_CREDENTIAL_REF", auth_ref="oscred://keychain/pados-mac")
    assert made.check(oscred, known_hosts(tmp_path)).error_code == "OS_CREDENTIAL_REF_REQUIRES_HELPER"
    assert spawn.calls == []


def test_confirmed_hostnames_must_resolve_only_to_allowed_addresses(tmp_path: Path) -> None:
    good, _, _ = probe(tmp_path, resolver=lambda host: [lan(20)])
    assert good.resolve("mac.lan") == lan(20)
    rebinding, _, _ = probe(tmp_path, resolver=lambda host: [lan(20), "169.254.169.254"])
    with pytest.raises(MachineError, match="HOST_FORBIDDEN_RANGE"):
        rebinding.resolve("mac.lan")
    public, runner, spawn = probe(tmp_path, resolver=lambda host: ["8.8.8.8"])
    observation = public.check(profile(host="mac.lan", hostname_confirmed=True), known_hosts(tmp_path))
    assert observation.reachable is False and observation.error_code == "HOST_MUST_BE_PRIVATE_LAN_OR_OVERLAY"
    assert runner.calls == [] and spawn.calls == []


def test_keyscan_parsing_is_bounded_and_tolerant() -> None:
    noisy = "garbage\n" + keyscan_output(host_key("mac")) + f"{lan(20)} ssh-ed25519 !!!\n"
    assert parse_keyscan(noisy) == [host_key("mac")]
    with pytest.raises(MachineError, match="PROBE_TIMEOUT_INVALID"):
        OpenSshProbe(timeout_seconds=0)
