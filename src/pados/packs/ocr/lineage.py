# SPDX-License-Identifier: AGPL-3.0-only
"""Lineage hooks: register OCR outputs as derived nodes so delete/revoke reaches every derivative.

Every stage output of a run (preprocess manifest, intermediate documents, the final document) becomes
a ``NORMALIZED_ARTIFACT`` node under the already-registered ``SOURCE`` node, and each section of the
final document becomes a ``CHUNK`` node under the final document. ``LineageGraph.plan_revocation`` on
the source therefore lists all of them. Node ids are deterministic (``derived_id``) and the returned
record is content-free (ids and hashes only) and passes ``assert_minimized``.

The caller supplies the TRUSTED identity of the source node (``trusted_sources``: source node id ->
SHA-256 of the source artifact, from its own registry); registration is refused unless it equals the
hash of the document's source, so OCR derivatives can never be hung under the wrong source. All nodes
and edges are computed and validated before the graph is touched, and the graph is restored from a
snapshot (through ``LineageGraph``'s public fields) if anything fails while they are applied, so a
registration is all-or-nothing.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pados.enterprise.lineage import LineageGraph, LineageKind, derived_id
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_minimized
from pados.packs.ocr.pipeline import PipelineResult

LINEAGE_SCHEMA = "pados.ocr.lineage.v1"
LINEAGE_KEYS = frozenset({"schema", "source_id", "plan_id", "plan_hash", "document_sha256", "document_node",
                          "intermediate_nodes", "chunk_nodes"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OcrLineageError(ValueError):
    """Registration refused or rolled back (``code`` is closed; the graph is unchanged)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OcrLineage:
    source_id: str
    document_node: str
    intermediate_nodes: tuple[str, ...]
    chunk_nodes: tuple[str, ...]
    record: dict[str, Any]

    @property
    def all_nodes(self) -> tuple[str, ...]:
        return (*self.intermediate_nodes, self.document_node, *self.chunk_nodes)


def _plan(source_id: str, result: PipelineResult) -> tuple[list[tuple[str, LineageKind, str]], OcrLineage]:
    """Every (node, kind, parent) edge of the registration, computed without touching any graph."""
    final_sha = result.document_ref.sha256
    edges: list[tuple[str, LineageKind, str]] = []
    intermediates: list[str] = []
    for stage_id, ref in result.stage_outputs.items():
        if ref.sha256 == final_sha:
            continue
        node = derived_id(LineageKind.NORMALIZED_ARTIFACT.value, source_id, "ocr", stage_id, ref.sha256)
        intermediates.append(node)
        edges.append((node, LineageKind.NORMALIZED_ARTIFACT, source_id))
    document_node = derived_id(LineageKind.NORMALIZED_ARTIFACT.value, source_id, "ocr.document", final_sha)
    edges.append((document_node, LineageKind.NORMALIZED_ARTIFACT, source_id))
    chunks = [derived_id(LineageKind.CHUNK.value, document_node, section.section_id,
                         canonical_hash(list(section.block_ids)))
              for section in result.document.sections]
    edges += [(chunk, LineageKind.CHUNK, document_node) for chunk in chunks]
    record: dict[str, Any] = {"schema": LINEAGE_SCHEMA, "source_id": source_id, "plan_id": result.plan_id,
                              "plan_hash": result.plan_hash, "document_sha256": final_sha,
                              "document_node": document_node, "intermediate_nodes": intermediates,
                              "chunk_nodes": chunks}
    assert_minimized(record, allowed_keys=LINEAGE_KEYS, max_items=4096, max_total_bytes=1024 * 1024,
                     max_string=256)
    return edges, OcrLineage(source_id=source_id, document_node=document_node,
                             intermediate_nodes=tuple(intermediates), chunk_nodes=tuple(chunks), record=record)


def _validate(graph: LineageGraph, source_id: str, result: PipelineResult,
              trusted_sources: Mapping[str, str]) -> None:
    if graph.nodes.get(source_id) is not LineageKind.SOURCE:
        raise OcrLineageError("LINEAGE_SOURCE_NOT_REGISTERED")
    if graph.is_revoked(source_id):
        raise OcrLineageError("LINEAGE_SOURCE_REVOKED")
    trusted = trusted_sources.get(source_id)
    if not isinstance(trusted, str) or not _SHA256.fullmatch(trusted):
        raise OcrLineageError("LINEAGE_SOURCE_IDENTITY_UNKNOWN")
    if trusted != result.document.source.sha256:
        raise OcrLineageError("LINEAGE_SOURCE_MISMATCH")
    if result.document_ref not in result.stage_outputs.values():
        raise OcrLineageError("LINEAGE_RESULT_INCONSISTENT")


def register_ocr_lineage(graph: LineageGraph, *, source_id: str, result: PipelineResult,
                         trusted_sources: Mapping[str, str]) -> OcrLineage:
    """Add the run's derived nodes under ``source_id`` atomically (all or nothing).

    ``trusted_sources`` maps source node ids to the SHA-256 of their source artifact, as recorded by
    the caller's own registry; it must match the document's source hash.
    """
    _validate(graph, source_id, result, trusted_sources)
    edges, lineage = _plan(source_id, result)
    planned: dict[str, LineageKind] = {}
    for node, kind, _ in edges:
        if graph.nodes.get(node, kind) is not kind or planned.setdefault(node, kind) is not kind:
            raise OcrLineageError("LINEAGE_NODE_KIND_CONFLICT")
        if graph.is_revoked(node):
            raise OcrLineageError("LINEAGE_NODE_REVOKED")
    nodes = dict(graph.nodes)
    children = {node: set(kids) for node, kids in graph.children.items()}
    revoked = set(graph.revoked)
    applied = True
    try:
        for node, kind, parent in edges:
            graph.add(node, kind, (parent,))
    except Exception:  # restore the snapshot below: a registration is all-or-nothing
        applied = False
    if not applied:
        graph.nodes.clear()
        graph.nodes.update(nodes)
        graph.children.clear()
        graph.children.update(children)
        graph.revoked.clear()
        graph.revoked.update(revoked)
        raise OcrLineageError("LINEAGE_REGISTRATION_ROLLED_BACK")
    return lineage
