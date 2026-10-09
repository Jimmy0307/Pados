# SPDX-License-Identifier: AGPL-3.0-only
"""Server-side context reconstruction for remote intents.

The remote client sends intent only. PADOS rebuilds the working context from
its own authoritative stores through the existing ``ProjectContextLoader``;
remote UI history, provider history and PADOS history are never concatenated.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pados.execution.context import ProjectContext, ProjectContextLoader, canonical_hash
from pados.remote.intent import RemoteIntentEnvelope

# Returns the authoritative semantic mapping (including ``scope_version``) for
# a project, or ``None`` when the project is unknown to PADOS.
SemanticSource = Callable[[str], Mapping[str, Any] | None]
WorkspaceResolver = Callable[[str], Path | None]


_EMPTY_VALUES = frozenset({"[]", "{}", "null", '""', ""})


class ContextReconstructionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# The only context-derived fields the intent ledger may hold (fingerprints and counts).
CONTEXT_SUMMARY_KEYS = frozenset({
    "envelope_fingerprint", "context_fingerprint", "scope_id", "scope_version", "source_count",
    "context_source", "client_history_included", "provider_history_included",
})


@dataclass(frozen=True)
class ReconstructedContext:
    envelope_fingerprint: str
    context: ProjectContext
    context_source: str = "PADOS_AUTHORITATIVE_STORE"
    client_history_included: bool = False
    provider_history_included: bool = False

    @property
    def context_fingerprint(self) -> str:
        return self.context.fingerprint

    def summary(self) -> dict[str, Any]:
        return {
            "envelope_fingerprint": self.envelope_fingerprint,
            "context_fingerprint": self.context_fingerprint,
            "scope_id": self.context.scope_id,
            "scope_version": self.context.scope_version,
            "source_count": len(self.context.sources),
            "context_source": self.context_source,
            "client_history_included": self.client_history_included,
            "provider_history_included": self.provider_history_included,
        }


class RemoteContextReconstructor:
    def __init__(
        self,
        *,
        semantic_source: SemanticSource,
        workspace_resolver: WorkspaceResolver,
        max_total_bytes: int = 64_000,
    ) -> None:
        self._semantic = semantic_source
        self._workspace = workspace_resolver
        self._max_total_bytes = max_total_bytes

    def reconstruct(self, envelope: RemoteIntentEnvelope) -> ReconstructedContext:
        semantic = self._semantic(envelope.project_id)
        if semantic is None:
            raise ContextReconstructionError("PROJECT_NOT_REGISTERED")
        workspace = self._workspace(envelope.project_id)
        if workspace is None:
            raise ContextReconstructionError("PROJECT_WORKSPACE_UNAVAILABLE")
        try:
            loader = ProjectContextLoader(workspace, max_total_bytes=self._max_total_bytes)
            context = loader.load(scope_id=envelope.project_id, semantic=semantic)
        except ValueError as exc:
            raise ContextReconstructionError(str(exc)) from exc
        return ReconstructedContext(envelope_fingerprint=envelope.fingerprint, context=context)


def duplicated_sources(context: ProjectContext) -> list[str]:
    """Content hashes that appear more than once (context duplication check).

    Empty-value placeholders (``[]``, ``{}``, ``null``, ``""``) are not content
    and are ignored.
    """
    seen: dict[str, int] = {}
    for source in context.sources:
        if source.text in _EMPTY_VALUES:
            continue
        seen[source.content_hash] = seen.get(source.content_hash, 0) + 1
    return sorted(digest for digest, count in seen.items() if count > 1)


def envelope_context_ratio(envelope: RemoteIntentEnvelope, context: ProjectContext) -> float:
    """Envelope bytes relative to reconstructed context bytes (efficiency metric)."""
    context_bytes = sum(source.bytes_read for source in context.sources) or 1
    return envelope.size_bytes / context_bytes


__all__ = [
    "ContextReconstructionError",
    "ReconstructedContext",
    "RemoteContextReconstructor",
    "canonical_hash",
    "duplicated_sources",
    "envelope_context_ratio",
]
