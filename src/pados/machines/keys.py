# SPDX-License-Identifier: AGPL-3.0-only
"""SecretRef-backed HMAC keys (Home Base ledger key, operator proof keys).

A key is referenced as ``secretref://...`` and resolved only inside
``pados.enterprise.secrets.SecretBoundary`` for the duration of one MAC
computation; the resolved value is never stored on an object, logged or
returned. Messages are domain-separated canonical JSON, so a MAC for one
purpose (ledger entry, ledger head, challenge, operator proof) never verifies
for another.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary, SecretResolver, is_secret_ref
from pados.machines.errors import MachineError

MIN_KEY_LENGTH = 32
Mac = Callable[[str, Any], str]


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def mac_with(key: bytes, domain: str, payload: Any) -> str:
    return hmac.new(key, domain.encode("ascii") + b"\n" + canonical_bytes(payload), hashlib.sha256).hexdigest()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SecretKey:
    """One HMAC key behind a SecretRef; ``prefix`` names the error codes (e.g. ``HOME_BASE_KEY``)."""

    def __init__(self, key_ref: str, *, resolver: SecretResolver | None = None, prefix: str) -> None:
        if not is_secret_ref(key_ref):
            raise MachineError(f"{prefix}_REF_REQUIRED")
        self.key_ref = key_ref
        self._prefix = prefix
        self._boundary = SecretBoundary(resolver or EnvironmentSecretResolver())

    @contextmanager
    def macs(self) -> Iterator[Mac]:
        """Resolve the key once for a block of MAC computations."""
        with ExitStack() as stack:
            unavailable = False
            try:
                values = stack.enter_context(
                    self._boundary.resolved({self.key_ref}, authorized_refs={self.key_ref}))
            except PermissionError:
                unavailable = True
            if unavailable:
                raise MachineError(f"{self._prefix}_UNAVAILABLE")
            key = values[self.key_ref].reveal().encode("utf-8")
            if len(key) < MIN_KEY_LENGTH:
                raise MachineError(f"{self._prefix}_TOO_SHORT")
            yield lambda domain, payload: mac_with(key, domain, payload)

    def mac(self, domain: str, payload: Any) -> str:
        with self.macs() as mac:
            return mac(domain, payload)

    def verify(self, domain: str, payload: Any, value: object) -> bool:
        return isinstance(value, str) and hmac.compare_digest(self.mac(domain, payload), value)


def home_base_key(key_ref: str, *, resolver: SecretResolver | None = None) -> SecretKey:
    return SecretKey(key_ref, resolver=resolver, prefix="HOME_BASE_KEY")
