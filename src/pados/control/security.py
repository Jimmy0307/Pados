# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from pados.config import Settings
from pados.memory.security import assert_no_secret
from pados.security_posture import DEFAULT_CONTROL_TOKEN_HASH


def token_hash(control_value: str) -> str:
    return hashlib.sha256(control_value.encode("utf-8")).hexdigest().upper()


CONTROL_TOKEN_HEADER = "X-PADOS-Control-Token"


def authorize_token(settings: Settings, control_value: str | None) -> None:
    # The shipped default hash is public once the source is: while it is configured, nothing authenticates,
    # whatever token is presented. ``pados init`` writes a unique token and its hash.
    if settings.control_api_token_hash.upper() == DEFAULT_CONTROL_TOKEN_HASH:
        raise PermissionError("DEFAULT_CONTROL_TOKEN_REFUSED: run `pados init` to generate a unique control token")
    if not control_value:
        raise PermissionError("missing control token")
    if not hmac.compare_digest(token_hash(control_value), settings.control_api_token_hash.upper()):
        raise PermissionError("invalid control token")


def assert_unique_control_token(settings: Settings) -> None:
    """Refuse to serve while the shipped default control-token hash is configured (nothing could authenticate)."""
    if settings.control_api_token_hash.upper() == DEFAULT_CONTROL_TOKEN_HASH:
        raise PermissionError("DEFAULT_CONTROL_TOKEN_REFUSED: run `pados init` to generate a unique control token")


def assert_localhost_binding(settings: Settings) -> None:
    if settings.api_host not in {"127.0.0.1", "localhost"}:
        raise PermissionError("control api must bind to localhost")


def authorize_control_request(settings: Settings, control_value: str | None) -> None:
    """Single reusable control-plane authorization gate.

    This is the extracted seam shared by every control surface: it keeps the
    localhost-binding assertion and the control-token comparison in one place so
    that no surface can drift into a second authentication implementation. It
    authenticates a trusted local control-plane caller only; it establishes no
    user identity, tenancy, or semantic authority.
    """
    assert_localhost_binding(settings)
    authorize_token(settings, control_value)


def assert_request_safe(payload: Any, *, max_bytes: int) -> None:
    encoded = json.dumps(payload, sort_keys=True)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("request size limit exceeded")
    assert_no_secret(encoded, field_name="control_payload")


def resolve_vault_path(vault_root: Path, relative_path: str) -> Path:
    if "\x00" in relative_path:
        raise PermissionError("invalid path")
    root = vault_root.resolve()
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("path traversal blocked")
    return target
