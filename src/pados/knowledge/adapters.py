# SPDX-License-Identifier: AGPL-3.0-only
"""Declared knowledge adapter boundaries and the library scope profile.

Each boundary names the existing module that fulfils it, or none at all when
the capability is not available. Unavailable boundaries raise instead of
offering a stand-in. Nothing here promotes memory or accepts results.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any

_LIBRARY_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_MAX_LIBRARY_SOURCE_BYTES = 100_000_000


@dataclass(frozen=True)
class AdapterBoundary:
    """One named adapter slot and where (if anywhere) it is implemented."""

    name: str
    module: str | None
    symbol: str | None
    status: str

    def resolve(self) -> Any:
        if self.module is None or self.symbol is None:
            raise RuntimeError(f"{self.name.upper()}_UNAVAILABLE")
        return getattr(importlib.import_module(self.module), self.symbol)


ADAPTER_BOUNDARIES: tuple[AdapterBoundary, ...] = (
    AdapterBoundary("DocumentParserAdapter", "pados.corpus.pdf", "parse_pdf", "IMPLEMENTED"),
    AdapterBoundary("OCRAdapter", None, None, "BLOCKED"),
    AdapterBoundary("RetrievalAdapter", "pados.memory.retrieval", "search_memory", "IMPLEMENTED"),
    AdapterBoundary("EmbeddingAdapter", None, None, "BLOCKED"),
    AdapterBoundary("RerankerAdapter", None, None, "BLOCKED"),
    AdapterBoundary(
        "GenerationAdapter", "pados.providers.factory", "build_chat_provider", "UNVERIFIED"
    ),
)


@dataclass(frozen=True)
class KnowledgeLibraryProfile:
    """A knowledge library bound to one project with a per-source byte budget."""

    library_id: str
    project_id: str
    max_source_bytes: int = 10_000_000
    private_evidence_may_leave_process: bool = False
    training_collection_enabled: bool = False
    citation_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.library_id, str) or not _LIBRARY_ID.fullmatch(self.library_id):
            raise ValueError("INVALID_LIBRARY_ID")
        if (
            not self.project_id
            or isinstance(self.max_source_bytes, bool)
            or not isinstance(self.max_source_bytes, int)
            or not 1 <= self.max_source_bytes <= _MAX_LIBRARY_SOURCE_BYTES
        ):
            raise ValueError("INVALID_LIBRARY_SCOPE_OR_BUDGET")
        if self.private_evidence_may_leave_process or self.training_collection_enabled:
            raise PermissionError("EXTERNAL_OR_TRAINING_AUTHORIZATION_NOT_IMPLEMENTED")

    def assert_source_scope(self, *, project_id: str, byte_size: int) -> None:
        """Reject a source from another project or outside the byte budget."""
        if project_id != self.project_id:
            raise PermissionError("KNOWLEDGE_PROJECT_SCOPE_MISMATCH")
        if not 0 <= byte_size <= self.max_source_bytes:
            raise ValueError("KNOWLEDGE_SOURCE_BUDGET_EXCEEDED")
