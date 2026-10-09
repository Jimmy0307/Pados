# SPDX-License-Identifier: AGPL-3.0-only
"""Node-local immutable, content-addressed artifact store."""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    sha256: str
    size_bytes: int
    media_type: str
    path: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, payload: bytes, *, media_type: str, path: str) -> ArtifactRef:
        digest = hashlib.sha256(payload).hexdigest()
        target = self.root / digest
        if not target.exists():
            tmp = self.root / f".{digest}.tmp-{os.getpid()}"
            tmp.write_bytes(payload)
            os.chmod(tmp, 0o444)
            os.replace(tmp, target)
        return ArtifactRef(artifact_id=f"sha256:{digest}", sha256=digest, size_bytes=len(payload),
                           media_type=media_type, path=path)

    def get(self, artifact_id: str) -> bytes:
        digest = artifact_id.removeprefix("sha256:")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise KeyError("ARTIFACT_ID_INVALID")
        target = self.root / digest
        if not target.is_file():
            raise KeyError("ARTIFACT_NOT_FOUND")
        payload = target.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY_FAILED")
        return payload
