# SPDX-License-Identifier: AGPL-3.0-only
"""Content-derived identity for knowledge sources.

A source is identified solely by the SHA-256 of its bytes. File names, paths
and storage locations are recorded elsewhere as lineage and never take part
in identity.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_DEFAULT_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class SourceIdentity:
    """The physical identity of one immutable byte sequence."""

    content_hash: str
    byte_size: int

    @property
    def source_id(self) -> str:
        return f"sha256:{self.content_hash}"


def source_identity_from_bytes(content: bytes) -> SourceIdentity:
    """Derive the identity of ``content`` from its digest and length."""
    return SourceIdentity(hashlib.sha256(content).hexdigest(), len(content))


def bundle_source_id(member_hashes: Iterable[str]) -> str:
    """Return an order-independent identifier for a set of member digests.

    A single member keeps its own ``sha256:`` identifier, since one file is
    not a new bundle.
    """
    members = list(member_hashes)
    if not members or not all(
        isinstance(member, str) and _HEX_DIGEST.fullmatch(member) for member in members
    ):
        raise ValueError("BUNDLE_MEMBER_HASH_INVALID")
    if len(members) == 1:
        return f"sha256:{members[0]}"
    joined = "\n".join(sorted(members)).encode("ascii")
    return "bundle-sha256:" + hashlib.sha256(joined).hexdigest()


def physical_file_sha256(path: Path, *, chunk_size: int = _DEFAULT_CHUNK) -> str:
    """Hash a file incrementally so large sources never load fully into memory."""
    if chunk_size < 1:
        raise ValueError("HASH_CHUNK_SIZE_MUST_BE_POSITIVE")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()
