# SPDX-License-Identifier: AGPL-3.0-only
"""SecretRef boundary for enterprise capabilities.

Credentials are referenced by ``secretref://<provider>/<path>`` only (the
existing PADOS grant syntax). Resolution happens only inside
``SecretBoundary.resolved`` for references the PADOS grant authorized; the
resolved value never appears in manifests, receipts, reports or logs.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from collections.abc import Set as AbstractSet
from contextlib import contextmanager
from typing import Protocol

from pados.execution.authorization import _valid_secret_ref

REDACTED = "[REDACTED]"
# Values that look like credentials must never be written where a SecretRef belongs.
_SECRET_LIKE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|xox[abp]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:bearer)\s+[A-Za-z0-9._-]{12,})"
)


def is_secret_ref(value: object) -> bool:
    return isinstance(value, str) and _valid_secret_ref(value)


def looks_like_secret(value: str) -> bool:
    return _SECRET_LIKE.search(value) is not None


class SecretValue:
    """A resolved secret that refuses to render itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"SecretValue({REDACTED})"

    __str__ = __repr__


class SecretResolver(Protocol):
    def resolve(self, ref: str) -> SecretValue | None:
        """Resolve one reference; ``None`` when unavailable."""


class EnvironmentSecretResolver:
    """Resolves ``secretref://env/<NAME>`` from an explicit environment mapping."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def resolve(self, ref: str) -> SecretValue | None:
        if not ref.startswith("secretref://env/"):
            return None
        value = self._environ.get(ref.removeprefix("secretref://env/"))
        return SecretValue(value) if value else None


class SecretBoundary:
    def __init__(self, resolver: SecretResolver) -> None:
        self._resolver = resolver

    @contextmanager
    def resolved(self, refs: AbstractSet[str], *, authorized_refs: AbstractSet[str]) -> Iterator[dict[str, SecretValue]]:
        """Resolve authorized references for the duration of one execution."""
        if any(not is_secret_ref(ref) for ref in refs):
            raise PermissionError("SECRET_REFERENCE_REQUIRED")
        if not refs <= authorized_refs:
            raise PermissionError("SECRET_REFERENCE_NOT_AUTHORIZED")
        values: dict[str, SecretValue] = {}
        for ref in sorted(refs):
            value = self._resolver.resolve(ref)
            if value is None:
                raise PermissionError("SECRET_REFERENCE_UNAVAILABLE")
            values[ref] = value
        try:
            yield values
        finally:
            values.clear()


def redact(text: str, secrets: Mapping[str, SecretValue] | None = None) -> str:
    """Remove resolved secret material and credential-shaped values from text."""
    for value in (secrets or {}).values():
        raw = value.reveal()
        if raw:
            text = text.replace(raw, REDACTED)
    return _SECRET_LIKE.sub(REDACTED, text)
