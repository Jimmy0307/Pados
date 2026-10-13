# SPDX-License-Identifier: AGPL-3.0-only
"""Stable node identity and credential-bound identity proofs.

The node id is derived from a random identity key persisted in the node's
state directory, so it survives restarts and is independent of hostname or
address. Possession of the SecretRef-backed node credential is proven with an
HMAC over a PADOS-chosen nonce; a hostname or reachable address is never
sufficient identity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IDENTITY_FILE = "node_identity.json"


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    created_at: str


def load_or_create_identity(state_dir: Path, *, now: str) -> NodeIdentity:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / IDENTITY_FILE
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        key = bytes.fromhex(str(data["identity_key"]))
        if len(key) != 32:
            raise ValueError("NODE_IDENTITY_KEY_INVALID")
        return NodeIdentity(_node_id(key), str(data["created_at"]))
    key = secrets.token_bytes(32)
    payload = json.dumps({"identity_key": key.hex(), "created_at": now})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
    return NodeIdentity(_node_id(key), now)


def _node_id(key: bytes) -> str:
    return "lcn-" + hashlib.sha256(b"pados-lcn-identity\n" + key).hexdigest()[:20]


def identity_proof(credential: str, node_id: str, nonce: str) -> str:
    message = f"pados-node-identity\n{node_id}\n{nonce}".encode()
    return hmac.new(credential.encode("utf-8"), message, hashlib.sha256).hexdigest()


def record_mac(credential: str, record: dict[str, Any]) -> str:
    body = json.dumps({k: v for k, v in record.items() if k != "record_mac"}, sort_keys=True, separators=(",", ":"))
    return hmac.new(credential.encode("utf-8"), b"pados-node-record\n" + body.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(expected: str, actual: object) -> bool:
    return isinstance(actual, str) and hmac.compare_digest(expected, actual)


def credential_fingerprint(credential: str) -> str:
    """Non-reversible reference for logs and receipts."""
    return "cred:" + hashlib.sha256(b"pados-node-credential\n" + credential.encode("utf-8")).hexdigest()[:16]
