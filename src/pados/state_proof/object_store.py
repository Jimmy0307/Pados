# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ObjectRef:
    """Immutable reference to bytes in the candidate proof object store."""

    object_id: str
    content_hash: str
    size_bytes: int
    media_type: str
    storage_uri: str
    object_kind: str


class ObjectStore:
    """Filesystem-backed, content-addressed storage scoped to one proof root."""

    def __init__(self, approved_root: Path) -> None:
        self.root = approved_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, payload: bytes, *, media_type: str, object_kind: str) -> ObjectRef:
        digest = hashlib.sha256(payload).hexdigest()
        target = (self.root / digest[:2] / digest[2:4] / digest).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("OBJECT_PATH_OUTSIDE_APPROVED_ROOT")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ValueError("OBJECT_HASH_COLLISION_OR_MUTATION")
        else:
            fd, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return ObjectRef(
            object_id=_new_id(),
            content_hash=digest,
            size_bytes=len(payload),
            media_type=media_type,
            storage_uri=str(target),
            object_kind=object_kind,
        )

    def put_json(self, value: dict[str, Any], *, object_kind: str) -> ObjectRef:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.put_bytes(payload, media_type="application/json", object_kind=object_kind)

    def read(self, ref: ObjectRef) -> bytes:
        target = Path(ref.storage_uri).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("OBJECT_PATH_OUTSIDE_APPROVED_ROOT")
        payload = target.read_bytes()
        if hashlib.sha256(payload).hexdigest() != ref.content_hash:
            raise ValueError("OBJECT_PAYLOAD_HASH_MISMATCH")
        return payload


def _new_id() -> str:
    from uuid import uuid4

    return str(uuid4())
