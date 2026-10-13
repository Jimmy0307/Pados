# SPDX-License-Identifier: AGPL-3.0-only
"""OpenSSH-backed ``ConnectionProbe``: authentication only, no session, no remote command.

* ``ssh-keyscan`` reads the presented host key. A host that does not present the
  pinned key is never logged into.
* The ordinary connection test runs ``ssh -N`` (no remote command, no session or
  command channel; nothing runs through the account's shell) with
  ``BatchMode=yes``, ``StrictHostKeyChecking=yes``, the PADOS-managed
  ``UserKnownHostsFile``, ``HostKeyAlias=<machine_id>``, user configuration,
  global known_hosts, multiplexing, forwarding and password /
  keyboard-interactive authentication disabled and a hard deadline.
  Authentication success is an unforgeable local signal, not server text:
  ``PermitLocalCommand=yes`` + ``LocalCommand=echo pados-auth-<nonce>`` with a
  fresh 128-bit hex nonce per run. OpenSSH runs LocalCommand on *this* machine
  only after successful authentication, writing to ssh's stdout, which the
  server cannot write to with ``-N`` (no channel; banners go to stderr).
  AUTHENTICATED only if stdout is exactly that nonce line; the process group is
  then killed. Anything else fails closed (``AUTH_REQUIRED``; server-influenced
  stderr can only make a result worse). LocalCommand runs through the local
  user's shell; only the validated hex nonce is interpolated. Any shell-backed
  remote check would be a PRIVILEGED_OPERATION.
* Credential use needs the operator ``CredentialPolicy`` (``pados.machines.policy``):
  without a policy, or when it does not allow the profile's reference for that
  machine and username, no credential is resolved and nothing is attempted.
  ``SSH_KEY`` references resolve (through ``SecretBoundary``) to an identity
  file path used with ``IdentitiesOnly=yes`` and ``IdentityAgent=none``.
  ssh-agent use is restricted to the policy's pinned agent keys
  (``IdentityAgent=<socket>`` + ``IdentitiesOnly=yes`` + ``IdentityFile=<public key file>``).
* A confirmed hostname is resolved first and every resolved address must pass
  the machine-target rules (DNS rebinding defence). Process output is mapped to
  local codes and never stored or logged.

Tests never run this against a host: they inject ``runner``, ``spawn`` and ``resolver``.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary, SecretResolver
from pados.machines.connection import AuthOutcome, ProbeObservation
from pados.machines.errors import MachineError
from pados.machines.fsguard import replace_owner_only
from pados.machines.identity import HostKey
from pados.machines.policy import CredentialDecision, CredentialPolicy
from pados.machines.profile import AuthMethod, MachineProfile
from pados.machines.targets import is_ip_literal, validate_address

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Resolver = Callable[[str], Sequence[str]]
HerdrCheck = Callable[[MachineProfile], bool]
DEFAULT_AGENT_REF = "ssh-agent://default"
KEY_PREFERENCE = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
AUTH_SIGNAL_PREFIX = "pados-auth-"
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_REJECTED = (b"host key verification failed", b"remote host identification has changed")
_AUTH_FAILED = (b"permission denied", b"too many authentication failures", b"no supported authentication methods")
_UNREACHABLE = (b"connection refused", b"connection timed out", b"operation timed out", b"no route to host",
                b"network is unreachable", b"could not resolve", b"connection closed by remote host")
MAX_KEYSCAN_LINES = 16
MAX_OUTPUT = 64 * 1024
POLL_SECONDS = 0.2
_UNSAFE_PATH_CHARS = frozenset("%$\n\r\t ")


class SshProcess(Protocol):
    pid: int

    def communicate(self, input: None = None, timeout: float | None = None) -> tuple[bytes | None, bytes | None]:
        """(stdout, stderr)."""
        ...

    def kill(self) -> None:
        ...


Spawn = Callable[[list[str], dict[str, str]], SshProcess]


def _system_resolver(host: str) -> Sequence[str]:
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})


def _system_spawn(argv: list[str], env: dict[str, str]) -> SshProcess:
    if sys.platform == "win32":
        return subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, start_new_session=True)


def _terminate(process: SshProcess) -> None:
    """Kill the whole process group (POSIX) or the process (Windows / injected doubles)."""
    try:
        if sys.platform != "win32" and isinstance(process, subprocess.Popen):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass


def parse_keyscan(output: str) -> list[HostKey]:
    keys: list[HostKey] = []
    for line in output.splitlines()[:MAX_KEYSCAN_LINES]:
        parts = line.split()
        if len(parts) < 3 or line.startswith("#"):
            continue
        try:
            keys.append(HostKey.from_openssh(f"{parts[1]} {parts[2]}"))
        except MachineError:
            continue
    return keys


def preferred(keys: Sequence[HostKey]) -> HostKey | None:
    for key_type in KEY_PREFERENCE:
        for key in keys:
            if key.key_type == key_type:
                return key
    return None


def authentication_line(nonce: str) -> str:
    return f"{AUTH_SIGNAL_PREFIX}{nonce}"


def _stdout_lines(stdout: bytes) -> list[str]:
    return [line.strip() for line in stdout.decode("ascii", errors="replace").splitlines() if line.strip()]


def classify_ssh_output(stdout: bytes, stderr: bytes, *, nonce: str,
                        exited: bool) -> tuple[AuthOutcome, bool, bool, str | None]:
    """(auth, reachable, host_key_rejected, code).

    AUTHENTICATED only when ssh's own stdout carries exactly the fresh nonce line written by
    ``LocalCommand`` (run locally, only after successful authentication; the server cannot
    write to this stream). stderr (server-influenced: banners) can only ever make the result
    worse, never ``AUTHENTICATED``.
    """
    lines = _stdout_lines(stdout)
    if lines == [authentication_line(nonce)]:
        return AuthOutcome.AUTHENTICATED, True, False, None
    if lines:
        return AuthOutcome.AUTH_REQUIRED, True, False, "SSH_AUTHENTICATION_SIGNAL_INVALID"
    text = stderr.lower()
    if any(marker in text for marker in _REJECTED):
        return AuthOutcome.NOT_ATTEMPTED, True, True, "HOST_KEY_MISMATCH"
    if any(marker in text for marker in _AUTH_FAILED):
        return AuthOutcome.AUTH_REQUIRED, True, False, "MACHINE_AUTH_REQUIRED"
    if any(marker in text for marker in _UNREACHABLE):
        return AuthOutcome.NOT_ATTEMPTED, False, False, "SSH_CONNECTION_FAILED"
    code = "SSH_OUTPUT_UNRECOGNIZED" if exited else "SSH_DEADLINE_WITHOUT_AUTHENTICATION"
    return AuthOutcome.AUTH_REQUIRED, True, False, code


class OpenSshProbe:
    def __init__(self, *, policy: CredentialPolicy | None = None, ssh_executable: str = "ssh",
                 keyscan_executable: str = "ssh-keyscan", timeout_seconds: int = 10, runner: Runner | None = None,
                 spawn: Spawn | None = None, resolver: Resolver | None = None,
                 secret_resolver: SecretResolver | None = None, herdr_check: HerdrCheck | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise MachineError("PROBE_TIMEOUT_INVALID")
        self.policy = policy
        self.ssh_executable = ssh_executable
        self.keyscan_executable = keyscan_executable
        self.timeout_seconds = timeout_seconds
        self._runner: Runner = runner or subprocess.run
        self._spawn: Spawn = spawn or _system_spawn
        self._resolver: Resolver = resolver or _system_resolver
        self._boundary = SecretBoundary(secret_resolver or EnvironmentSecretResolver())
        self._herdr_check = herdr_check
        self._clock = clock

    def resolve(self, host: str) -> str:
        """One allowed literal address for ``host``; any forbidden resolved address refuses all."""
        if is_ip_literal(host):
            return validate_address(host)
        try:
            addresses = list(self._resolver(host))
        except OSError:
            raise MachineError("HOST_UNRESOLVED") from None
        if not addresses:
            raise MachineError("HOST_UNRESOLVED")
        return [validate_address(address) for address in addresses][0]

    def keyscan_argv(self, address: str, port: int) -> list[str]:
        return [self.keyscan_executable, "-T", str(self.timeout_seconds), "-p", str(port),
                "-t", "ed25519,ecdsa,rsa", address]

    def ssh_argv(self, profile: MachineProfile, address: str, known_hosts: Path, identity: list[str],
                 nonce: str) -> list[str]:
        """Authentication-only argv: ``-N`` and no remote command, ever.

        ``LocalCommand`` is a fixed ``echo`` of ``pados-auth-<nonce>`` (32 lower-case hex digits,
        validated; nothing else is interpolated), run by OpenSSH on this machine only after
        successful authentication.
        """
        if not _NONCE.fullmatch(nonce):
            raise MachineError("SSH_AUTH_NONCE_INVALID")
        options = ["BatchMode=yes", "StrictHostKeyChecking=yes", f"UserKnownHostsFile={known_hosts}",
                   f"GlobalKnownHostsFile={os.devnull}", f"HostKeyAlias={profile.machine_id}",
                   f"ConnectTimeout={self.timeout_seconds}", "PasswordAuthentication=no",
                   "KbdInteractiveAuthentication=no", "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes",
                   "ControlMaster=no", "ControlPath=none", "RequestTTY=no", "LogLevel=ERROR",
                   "PermitLocalCommand=yes", f"LocalCommand=echo {authentication_line(nonce)}", *identity]
        argv = [self.ssh_executable, "-F", os.devnull, "-N"]
        for option in options:
            argv += ["-o", option]
        return [*argv, "-p", str(profile.port), "-l", profile.username, "--", address]

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._runner(argv, shell=False, capture_output=True, text=True, check=False,
                                stdin=subprocess.DEVNULL, timeout=self.timeout_seconds + 5, env=self._env())
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _scan(self, address: str, port: int) -> list[HostKey] | None:
        result = self._run(self.keyscan_argv(address, port))
        if result is None or result.returncode != 0:
            return None
        return parse_keyscan(result.stdout or "")

    def scan_host_key(self, host: str, port: int) -> HostKey | None:
        keys = self._scan(self.resolve(host), port)
        return preferred(keys) if keys else None

    @staticmethod
    def _env() -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", os.defpath)}
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        return env

    def check(self, profile: MachineProfile, known_hosts: Path) -> ProbeObservation:
        try:
            address = self.resolve(profile.host)
        except MachineError as exc:
            return ProbeObservation(reachable=False, error_code=exc.code)
        keys = self._scan(address, profile.port)
        if not keys:
            return ProbeObservation(reachable=keys is not None, error_code="HOST_KEY_NOT_PRESENTED")
        pinned = profile.host_key
        presented = pinned if pinned in keys else preferred(keys)
        if presented is None or presented != pinned:
            # Never attempt a login against a host that does not present the pinned key.
            return ProbeObservation(reachable=True, host_key=presented, error_code="HOST_KEY_MISMATCH")
        if self.policy is None:
            return self._refused(presented, "CREDENTIAL_POLICY_REQUIRED")
        try:
            decision = self.policy.decide(profile)
        except MachineError as exc:
            return self._refused(presented, exc.code)
        return self._login(profile, address, known_hosts, presented, decision)

    @staticmethod
    def _refused(key: HostKey, code: str) -> ProbeObservation:
        return ProbeObservation(reachable=True, host_key=key, auth=AuthOutcome.AUTH_REQUIRED, error_code=code)

    def _login(self, profile: MachineProfile, address: str, known_hosts: Path, key: HostKey,
               decision: CredentialDecision) -> ProbeObservation:
        method = profile.effective_auth_method
        if method is AuthMethod.SSH_AGENT:
            socket_path = os.environ.get("SSH_AUTH_SOCK", "")
            if decision.auth_ref != DEFAULT_AGENT_REF or not socket_path:
                return self._refused(key, "SSH_AGENT_UNAVAILABLE")
            if _UNSAFE_PATH_CHARS & set(socket_path):
                return self._refused(key, "SSH_AGENT_SOCKET_INVALID")
            files = self._agent_key_files(known_hosts.parent, decision.agent_public_keys)
            identity = [f"IdentityAgent={socket_path}", "IdentitiesOnly=yes", *(f"IdentityFile={f}" for f in files)]
            return self._authenticate(profile, address, known_hosts, key, identity)
        if method is AuthMethod.SSH_KEY:
            refused = "AUTH_REF_UNAVAILABLE"
            try:
                with self._boundary.resolved({decision.auth_ref}, authorized_refs={decision.auth_ref}) as values:
                    path = values[decision.auth_ref].reveal()
                    if path and not path.startswith("-") and not _UNSAFE_PATH_CHARS & set(path):
                        identity = ["IdentityAgent=none", "IdentitiesOnly=yes", f"IdentityFile={path}"]
                        return self._authenticate(profile, address, known_hosts, key, identity)
                    refused = "AUTH_REF_RESOLUTION_INVALID"
            except PermissionError:
                refused = "AUTH_REF_UNAVAILABLE"
            return self._refused(key, refused)
        return self._refused(key, "OS_CREDENTIAL_REF_REQUIRES_HELPER")

    @staticmethod
    def _agent_key_files(directory: Path, public_keys: Sequence[str]) -> list[Path]:
        """Write each pinned agent public key to an owner-only file for ``IdentityFile``."""
        folder = directory / "agent_keys"
        folder.mkdir(mode=0o700, exist_ok=True)
        files = []
        for public_key in public_keys:
            path = folder / f"{hashlib.sha256(public_key.encode('ascii')).hexdigest()[:24]}.pub"
            replace_owner_only(path, (public_key + "\n").encode("ascii"), "AGENT_KEY_FILE")
            files.append(path)
        return files

    def _authenticate(self, profile: MachineProfile, address: str, known_hosts: Path, key: HostKey,
                      identity: list[str]) -> ProbeObservation:
        nonce = secrets.token_hex(16)
        try:
            process = self._spawn(self.ssh_argv(profile, address, known_hosts, identity, nonce), self._env())
        except OSError:
            return ProbeObservation(reachable=False, error_code="SSH_CLIENT_FAILED")
        stdout, stderr, exited = self._watch(process, authentication_line(nonce))
        auth, reachable, rejected, code = classify_ssh_output(stdout, stderr, nonce=nonce, exited=exited)
        if not reachable:
            return ProbeObservation(reachable=False, error_code=code)
        if rejected:
            return ProbeObservation(reachable=True, host_key=key, host_key_rejected=True, error_code=code)
        if auth is not AuthOutcome.AUTHENTICATED:
            return ProbeObservation(reachable=True, host_key=key, auth=auth, error_code=code)
        herdr = self._herdr(profile) if profile.herdr.enabled else None
        return ProbeObservation(reachable=True, host_key=key, auth=AuthOutcome.AUTHENTICATED, herdr_available=herdr)

    def _watch(self, process: SshProcess, expected: str) -> tuple[bytes, bytes, bool]:
        """Read until the nonce line appears on stdout, the process exits or the deadline; then kill the group."""
        deadline = self._clock() + self.timeout_seconds
        out, err = b"", b""
        exited = False
        while self._clock() < deadline:
            try:
                got_out, got_err = process.communicate(timeout=POLL_SECONDS)
            except subprocess.TimeoutExpired as exc:
                if isinstance(exc.stdout, bytes):
                    out = exc.stdout[:MAX_OUTPUT]
                if isinstance(exc.stderr, bytes):
                    err = exc.stderr[:MAX_OUTPUT]
                if expected in _stdout_lines(out):
                    break
                continue
            out, err, exited = (got_out or b"")[:MAX_OUTPUT], (got_err or b"")[:MAX_OUTPUT], True
            break
        if not exited:
            _terminate(process)
            try:
                got_out, got_err = process.communicate(timeout=2)
                out, err = (got_out or out)[:MAX_OUTPUT], (got_err or err)[:MAX_OUTPUT]
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
        return out, err, exited

    def _herdr(self, profile: MachineProfile) -> bool | None:
        if self._herdr_check is None:
            return None
        try:
            return bool(self._herdr_check(profile))
        except Exception:  # noqa: BLE001 - an unanswered Herdr check is "not available"
            return False
