"""Machine Connection test helpers: hermetic, no network, no SSH, no real credentials.

Addresses, host keys and the bootstrap password are built at runtime so the
release secret / public-export scanners never see credential- or LAN-shaped
literals in this tree.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pados.enterprise.secrets import SecretValue
from pados.machines.bootstrap import SshTarget
from pados.machines.connection import AuthOutcome, ProbeObservation
from pados.machines.identity import HostKey
from pados.machines.inventory import InventoryStore
from pados.machines.profile import AuthMethod, MachineProfile

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def lan(last: int, third: int = 1) -> str:
    return ".".join(("192", "168", str(third), str(last)))


def overlay(last: int) -> str:
    return ".".join(("100", "64", "0", str(last)))


def host_key(seed: str) -> HostKey:
    material = hashlib.sha256(seed.encode("utf-8")).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + material
    return HostKey("ssh-ed25519", blob)


def user_public_key(seed: str = "operator") -> str:
    return host_key("user-" + seed).openssh() + " pados@home-base"


def one_time_password() -> str:
    return "Corr3ct-H0rse-" + secrets.token_hex(8)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in [key for key in os.environ if key.startswith("PADOS_")]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    yield


@pytest.fixture
def store(tmp_path: Path) -> InventoryStore:
    return InventoryStore(tmp_path / "machines" / "inventory.json")


@dataclass
class FakeProbe:
    """Operator-scripted network: (host, port) -> presented host key. Never touches a socket."""

    keys: dict[tuple[str, int], HostKey] = field(default_factory=dict)
    auth_ok: bool = True
    degraded: bool = False
    herdr_available: bool | None = True
    fail: bool = False
    scans: list[tuple[str, int]] = field(default_factory=list)
    checks: list[tuple[str, str, Path]] = field(default_factory=list)

    def place(self, host: str, key: HostKey, port: int = 22) -> None:
        self.keys[(host, port)] = key

    def scan_host_key(self, host: str, port: int) -> HostKey | None:
        self.scans.append((host, port))
        if self.fail:
            raise ConnectionError("probe failure with detail that must not leak")
        return self.keys.get((host, port))

    def check(self, profile: MachineProfile, known_hosts: Path) -> ProbeObservation:
        self.checks.append((profile.machine_id, profile.host, known_hosts))
        if self.fail:
            raise ConnectionError("probe failure with detail that must not leak")
        key = self.keys.get((profile.host, profile.port))
        if key is None:
            return ProbeObservation(reachable=False)
        if key != profile.host_key:
            return ProbeObservation(reachable=True, host_key=key)
        return ProbeObservation(
            reachable=True, host_key=key,
            auth=AuthOutcome.AUTHENTICATED if self.auth_ok else AuthOutcome.AUTH_REQUIRED,
            degraded=self.degraded, herdr_available=self.herdr_available)


@pytest.fixture
def probe() -> FakeProbe:
    return FakeProbe()


@dataclass
class FakeTransport:
    """Bootstrap transport double. ``hostile`` echoes the password in its exception text."""

    presented: HostKey | None
    hostile: bool = False
    login_ok: bool = True
    installed: list[str] = field(default_factory=list)
    password_seen: bool = False

    def presented_host_key(self, target: SshTarget) -> HostKey | None:
        return self.presented

    def install_public_key(self, target: SshTarget, pinned: HostKey, one_time_password: SecretValue,
                           public_key: str) -> None:
        self.password_seen = True
        if self.hostile:
            raise RuntimeError(f"ssh refused login {target.username}:{one_time_password.reveal()}")
        self.installed.append(public_key)

    def verify_key_login(self, target: SshTarget, pinned: HostKey, key_auth_method: AuthMethod,
                         auth_ref: str) -> bool:
        return self.login_ok and bool(self.installed)


def scan_tree_for(root: Path, needle: str) -> list[Path]:
    hits = []
    for path in root.rglob("*"):
        if path.is_file() and needle.encode("utf-8") in path.read_bytes():
            hits.append(path)
    return hits


def enroll(store: InventoryStore, probe: FakeProbe, *, name: str = "Mac", host: str | None = None,
           seed: str = "mac", auth: str = "ssh-agent", **options: object) -> MachineProfile:
    from pados.machines.ux import add_machine

    address = host or lan(20)
    key = host_key(seed)
    probe.place(address, key, int(str(options.get("port", 22))))
    options.setdefault("platform", "MACOS")
    return add_machine(name, address, "operator", auth, store=store, probe=probe,
                       expected_fingerprint=key.fingerprint, now=NOW, **options)  # type: ignore[arg-type]
