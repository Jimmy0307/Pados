# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from pados.config import Settings
from pados.db import session_scope
from pados.graph import cognition, intent_routing, repository, url_ingest
from pados.graph import context as cognitive_context
from pados.graph.cognition import CognitiveResponseError
from pados.graph.cognitive_pipeline import (
    CognitiveGoalInterpreter,
    GapDetector,
    ProblemDecomposer,
    ScopeRelation,
    SynthesisPlanner,
)
from pados.graph.schemas import (
    ActionDisabledReason,
    AssuranceStatus,
    AutoRefreshPolicy,
    CapabilityReuseRequest,
    CognitiveOperation,
    EdgeCreate,
    GroupCreate,
    IdeaState,
    IntentRequest,
    ModelSelection,
    ModelSlot,
    NodeCreate,
    NodeType,
    NodeUpdate,
    Provenance,
    ReuseMode,
    SelectionAction,
    SystemEdgeCreate,
    SystemNodeCreate,
    SystemNodeUpdate,
    UrlIngestRequest,
    WorkspaceCreate,
)
from pados.graph.source_discovery import SourceDiscoveryError, UnconfiguredSourceDiscovery
from pados.loop import service as loop_service
from pados.providers.discovery import MOCK_MODEL_CHOICES, ai_runtime_catalog
from pados.providers.base import LLMProvider
from pados.providers.execution_binding import ExecutionBindingRefused, ExecutionIntent, ExecutionRefusal
from pados.providers.factory import (
    ProviderNotConfiguredError,
    build_chat_provider,
    check_chat_provider_configured,
)
from pados.worker.registry import capability_registry


class GraphError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        write_occurred: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.write_occurred = write_occurred
        self.details = dict(details or {})


_REVIEW_WORKSPACE_MARKERS = ("qa", "acceptance", "demo", "fixture", "validation", "review", "p6.16", "test")
_PLACEHOLDER_OUTPUT = re.compile(r"mock-response:\d+", re.IGNORECASE)
_CONTEXT_ASSEMBLER = cognitive_context.CognitiveContextAssembler()
_GOAL_INTERPRETER = CognitiveGoalInterpreter()
_PROBLEM_DECOMPOSER = ProblemDecomposer()
_GAP_DETECTOR = GapDetector()
_SYNTHESIS_PLANNER = SynthesisPlanner()
_SOURCE_DISCOVERY = UnconfiguredSourceDiscovery()


def _provider_failure_code(exc: BaseException) -> str:
    if isinstance(exc, ProviderNotConfiguredError):
        return "PROVIDER_UNAVAILABLE"
    if isinstance(exc, CognitiveResponseError):
        return exc.code
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in type(exc).__name__.casefold():
        return "TIMEOUT"
    return "GENERATION_FAILED"


def _provider_failure_message(code: str) -> str:
    return {
        "PROVIDER_UNAVAILABLE": "目前尚未設定可用的認知模型。",
        "TIMEOUT": "認知模型在時間限制內沒有完成回應。",
        "INVALID_RESPONSE": "認知模型回傳的內容不是有效 JSON。",
        "INVALID_SCHEMA": "認知模型回傳的結構不符合 PADOS 認知合約。",
        "CONTEXT_LIMIT": "認知模型拒絕了目前的脈絡大小。",
        "GENERATION_FAILED": "認知模型生成失敗。",
        "MEASURED_EXECUTION_REFUSED": "認知模型沒有相符的實測執行綁定，已拒絕執行。",
    }.get(code, "認知模型處理失敗。")


_ALLOW_MOCK_ENVS = {"local", "test", "validation"}


def _measured_refusal_error(refusal: ExecutionRefusal, *, write_occurred: bool) -> GraphError:
    return GraphError(
        "MEASURED_EXECUTION_REFUSED",
        f"{_provider_failure_message('MEASURED_EXECUTION_REFUSED')}（{refusal.code}）",
        status_code=409,
        write_occurred=write_occurred,
        details={"refusal_code": refusal.code, "reasons": list(refusal.reasons)},
    )


def _workspace_project_scope(workspace: Mapping[str, Any], workspace_id: str) -> str:
    """The project scope the compute-node registration must admit (an unbound workspace is its own scope)."""
    return str(workspace.get("project_id") or f"workspace:{workspace_id}")


def _inference_project_id(settings: Settings, workspace_id: str) -> str:
    with session_scope(settings) as session:
        workspace = _workspace(session, workspace_id)
    return _workspace_project_scope(workspace, workspace_id)


def _cognitive_provider(
    settings: Settings,
    effective_settings: Settings,
    workspace_id: str,
    *,
    project_id: str | None = None,
) -> LLMProvider:
    """Mock only when explicitly selected; a real model only under a measured STRUCTURED_OUTPUT binding."""
    allow_mock = effective_settings.env in _ALLOW_MOCK_ENVS
    check_chat_provider_configured(effective_settings, allow_mock=allow_mock)
    if effective_settings.chat_provider.casefold() == "mock":
        return build_chat_provider(effective_settings, allow_mock=allow_mock)
    return build_chat_provider(
        effective_settings,
        allow_mock=allow_mock,
        intent=ExecutionIntent.STRUCTURED_OUTPUT,
        project_id=project_id or _inference_project_id(settings, workspace_id),
    )


def _measured_evidence(provider: object) -> dict[str, Any] | None:
    evidence = getattr(provider, "last_evidence", None)
    return dict(evidence) if isinstance(evidence, Mapping) else None


def _cognitive_runtime_status(effective_settings: Settings, measured: Mapping[str, Any] | None) -> str:
    if effective_settings.chat_provider.casefold() == "mock":
        return "MOCK_CONTRACT_TEST_ONLY"
    return "MEASURED_BINDING_APPLIED" if measured is not None else "REAL_MODEL_UNVALIDATED"


def _cognitive_source_ids(assembly: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    root_ids = [str(value) for value in assembly.get("root_node_ids", []) if value]
    evidence_ids = [
        str(item.get("source_id") or item.get("source_node_id"))
        for item in assembly.get("evidence", [])
        if isinstance(item, Mapping) and (item.get("source_id") or item.get("source_node_id"))
    ]
    reference_ids = [
        str(item.get("source_id") or item.get("source_node_id"))
        for item in assembly.get("resource_references", [])
        if isinstance(item, Mapping) and (item.get("source_id") or item.get("source_node_id"))
    ]
    used = list(dict.fromkeys(root_ids + evidence_ids))
    references = list(dict.fromkeys(reference_ids))
    return used, references


def _evidence_provenance(evidence: Any, response: Mapping[str, Any]) -> dict[str, Any]:
    """Map server-assigned evidence ids (E1..En) back to their source nodes; provenance stays server-owned."""

    items = (
        [item for item in evidence if isinstance(item, Mapping) and item.get("evidence_id")]
        if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes, bytearray))
        else []
    )
    sources = [
        {
            "evidence_id": str(item["evidence_id"]),
            "source_node_id": str(item.get("source_node_id") or item.get("source_id") or ""),
            "source_ref": str(item.get("source_ref") or ""),
            "source_title": str(item.get("source_title") or ""),
            "content_hash": str(item.get("content_hash") or ""),
        }
        for item in items
    ]
    by_id = {source["evidence_id"]: source["source_node_id"] for source in sources}
    refs = [str(value) for value in response.get("evidence_refs", [])]
    for claim in response.get("claims", []):
        if isinstance(claim, Mapping):
            refs.extend(str(value) for value in claim.get("evidence_refs", []))
    cited = [by_id[ref] for ref in dict.fromkeys(refs) if by_id.get(ref)]
    return {
        "evidence_ids": [source["evidence_id"] for source in sources],
        "evidence_sources": sources,
        "cited_source_node_ids": list(dict.fromkeys(cited)),
    }


def _enforce_provider_context_budget(specification: Mapping[str, Any], estimated_tokens: int) -> None:
    budget = specification.get("context_budget")
    if not isinstance(budget, Mapping):
        receipt = specification.get("context_receipt")
        budget = receipt if isinstance(receipt, Mapping) else {}
    try:
        maximum = int(
            budget.get("total")
            or budget.get("budget_tokens")
            or cognitive_context.DEFAULT_CONTEXT_TOKEN_BUDGET
        )
    except (TypeError, ValueError):
        maximum = cognitive_context.DEFAULT_CONTEXT_TOKEN_BUDGET
    if int(estimated_tokens) > maximum:
        raise CognitiveResponseError(
            "CONTEXT_LIMIT",
            f"provider context estimate {estimated_tokens} exceeds budget {maximum}",
        )


def _is_human_safe_workspace(workspace: Mapping[str, Any]) -> bool:
    identity = f"{workspace.get('name', '')} {workspace.get('description', '')}".casefold()
    return not any(marker in identity for marker in _REVIEW_WORKSPACE_MARKERS)


def _is_human_visible_node(node: Mapping[str, Any]) -> bool:
    if str(node.get("semantic_type")) != NodeType.OUTPUT.value:
        return True
    if _PLACEHOLDER_OUTPUT.search(f"{node.get('title', '')} {node.get('content', '')}"):
        return False
    if str(node.get("provenance")) == Provenance.AI_PROPOSED.value:
        metadata = node.get("metadata_json")
        return isinstance(metadata, Mapping) and (
            metadata.get("result_type") in {"COGNITIVE_RESULT", "LIVING_SYNTHESIS"}
            or bool(metadata.get("living_synthesis"))
        )
    return True


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (UUID, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_safe(item) for item in value]
    return str(value)


def _safe_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json_safe(value))


def _safe_dicts(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json_safe(value))


_SECRET_VALUE = re.compile(
    r"(?i)(password|passwd|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+"
)


def _redact_output_text(value: str) -> str:
    return _SECRET_VALUE.sub(r"\1: [REDACTED]", value)


def _output_metadata(node: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = node.get("metadata_json")
    return metadata if isinstance(metadata, Mapping) else {}


def _is_living_synthesis(node: Mapping[str, Any]) -> bool:
    metadata = _output_metadata(node)
    return bool(metadata.get("living_synthesis")) or (
        str(node.get("semantic_type")) == NodeType.OUTPUT.value
        and metadata.get("result_type") == "LIVING_SYNTHESIS"
    )


def _synthesis_version(node: Mapping[str, Any]) -> int:
    try:
        return max(1, int(_output_metadata(node).get("synthesis_version") or 1))
    except (TypeError, ValueError):
        return 1


def _root_ids_for_synthesis(node: Mapping[str, Any]) -> list[str]:
    metadata = _output_metadata(node)
    snapshot = metadata.get("source_snapshot")
    if isinstance(snapshot, Mapping):
        root_ids = snapshot.get("root_node_ids")
        if isinstance(root_ids, Sequence) and not isinstance(root_ids, (str, bytes, bytearray)):
            return [str(node_id) for node_id in root_ids]
    root_ids = metadata.get("root_node_ids") or metadata.get("source_node_ids") or []
    if isinstance(root_ids, Sequence) and not isinstance(root_ids, (str, bytes, bytearray)):
        return [str(node_id) for node_id in root_ids]
    return []


def _synthesis_summary(context: Mapping[str, Any]) -> str:
    summary = context.get("source_summary")
    if not isinstance(summary, Mapping):
        return ""
    base = (
        f"{int(summary.get('concepts') or 0)} 個概念、"
        f"{int(summary.get('resources') or 0)} 份資料、"
        f"{int(summary.get('memory') or 0)} 個記憶、"
        f"{int(summary.get('corpus') or 0)} 筆 Corpus 證據、"
        f"{int(summary.get('previous_outputs') or 0)} 個先前結果"
    )
    details: list[str] = []
    if int(summary.get("references") or 0):
        details.append(f"{int(summary['references'])} 個來源參照")
    if int(summary.get("evidence_units") or 0):
        details.append(f"{int(summary['evidence_units'])} 段來源內容")
    if int(summary.get("gaps") or 0):
        details.append(f"{int(summary['gaps'])} 個待補資訊")
    return base + ("、" + "、".join(details) if details else "")


def _synthesis_current_context(
    *,
    workspace_id: str,
    node: Mapping[str, Any],
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    metadata = _output_metadata(node)
    return _CONTEXT_ASSEMBLER.assemble(
        workspace_id=workspace_id,
        root_node_ids=_root_ids_for_synthesis(node),
        intent=str(metadata.get("intent") or "THINK"),
        action=str(metadata.get("action") or "THINK"),
        nodes=nodes,
        edges=edges,
        prior_synthesis_version=(
            int(metadata["prior_synthesis_version"])
            if metadata.get("prior_synthesis_version") is not None
            else None
        ),
    )


def _annotate_living_syntheses(
    workspace_id: str,
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project lifecycle state without mutating immutable OUTPUT receipts."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for node in nodes:
        if _is_living_synthesis(node):
            synthesis_id = str(_output_metadata(node).get("synthesis_id") or node.get("id"))
            grouped.setdefault(synthesis_id, []).append(node)
    annotated: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for node in nodes:
        if not _is_living_synthesis(node):
            annotated.append(dict(node))
            continue
        metadata = dict(_output_metadata(node))
        synthesis_id = str(metadata.get("synthesis_id") or node.get("id"))
        versions = grouped.get(synthesis_id, [node])
        latest = max(versions, key=_synthesis_version)
        projected = dict(node)
        if str(node.get("id")) != str(latest.get("id")):
            metadata["synthesis_status"] = "SUPERSEDED"
        else:
            current_context = _synthesis_current_context(
                workspace_id=workspace_id,
                node=node,
                nodes=nodes,
                edges=edges,
            )
            stale, reason = cognitive_context.compare_synthesis_context(metadata, current_context)
            metadata["synthesis_status"] = "STALE" if stale else "CURRENT"
            metadata["stale_reason"] = reason
            metadata["source_summary"] = current_context.get("source_summary", {})
            metadata["source_summary_text"] = _synthesis_summary(current_context)
        projected["metadata_json"] = metadata
        annotated.append(projected)
        history.append(
            {
                "id": str(node.get("id")),
                "synthesis_id": synthesis_id,
                "version": _synthesis_version(node),
                "title": str(node.get("title") or ""),
                "status": metadata.get("synthesis_status", "SUPERSEDED"),
                "created_at": node.get("created_at"),
                "context_fingerprint": metadata.get("context_fingerprint"),
            }
        )
    history.sort(key=lambda item: (str(item["synthesis_id"]), int(item["version"])))
    return annotated, history


def build_output_markdown(
    node: Mapping[str, Any],
    source_nodes: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Build a human-readable export without exposing graph or database IDs."""

    metadata = _output_metadata(node)
    title = _redact_output_text(str(node.get("title") or "Untitled output").strip())
    content = _redact_output_text(str(node.get("content") or "").strip())
    provider = str(metadata.get("provider") or "Unknown")
    model = str(metadata.get("model") or "Default")
    status = str(metadata.get("candidate_status") or metadata.get("lifecycle") or "PROVISIONAL")
    created = str(node.get("created_at") or metadata.get("creation_timestamp") or "Unknown")
    lines = [
        f"# {title}",
        "",
        content or "No readable result content was provided.",
        "",
        "## Result details",
        "",
        f"- Type: {metadata.get('result_type') or 'COGNITIVE_RESULT'!s}",
        f"- Created: {created}",
        f"- Provenance: {node.get('provenance') or 'AI_PROPOSED'!s}",
        f"- Provider: {provider}",
        f"- Model: {model}",
        f"- Status: {status}",
        *([f"- Synthesis version: {metadata.get('synthesis_version')}"] if metadata.get("living_synthesis") else []),
        *([f"- Context fingerprint: {metadata.get('context_fingerprint')}"] if metadata.get("living_synthesis") else []),
        *([f"- Source summary: {metadata.get('source_summary_text')}"] if metadata.get("source_summary_text") else []),
        "",
        "## Sources",
        "",
    ]
    if source_nodes:
        for source in source_nodes:
            source_metadata = source.get("metadata_json")
            source_metadata = source_metadata if isinstance(source_metadata, Mapping) else {}
            source_title = _redact_output_text(str(source.get("title") or "Untitled source"))
            source_type = str(source.get("semantic_type") or "SOURCE")
            line = f"- {source_title} ({source_type})"
            url = source_metadata.get("canonical_url") or source_metadata.get("final_url") or source_metadata.get("original_url")
            if url:
                line += f": {_redact_output_text(str(url))}"
            lines.append(line)
    else:
        lines.append("- No linked sources")
    return "\n".join(lines).rstrip() + "\n"


def _artifact_filename(title: str, artifact_dir: Path) -> Path:
    slug = re.sub(r"[^\w-]+", "-", title.strip(), flags=re.UNICODE).strip("-")[:80] or "pados-output"
    candidate = artifact_dir / f"{slug}.md"
    suffix = 2
    while candidate.exists():
        candidate = artifact_dir / f"{slug}-{suffix}.md"
        suffix += 1
    return candidate


def create_output_artifact(
    session: Session,
    workspace_id: str,
    node_id: str,
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Persist an OUTPUT as a durable Markdown Artifact without promoting it."""

    _workspace(session, workspace_id)
    try:
        node = repository.get_node(session, workspace_id, node_id)
    except LookupError as exc:
        raise GraphError("NODE_NOT_FOUND", "output node not found", status_code=404) from exc
    if node.get("semantic_type") != NodeType.OUTPUT.value:
        raise GraphError("OUTPUT_REQUIRED", "only an OUTPUT can be converted into a document", status_code=400)
    metadata = dict(_output_metadata(node))
    existing = metadata.get("artifact")
    if isinstance(existing, Mapping):
        return _safe_dict({"node": node, "artifact": existing, "human_status": "SAVED"})

    source_ids = metadata.get("source_node_ids")
    source_ids = [str(source_id) for source_id in source_ids] if isinstance(source_ids, Sequence) and not isinstance(source_ids, (str, bytes, bytearray)) else []
    source_nodes = repository.get_nodes(session, workspace_id, source_ids)
    markdown = build_output_markdown(node, source_nodes)
    artifact_dir = (artifact_root or (Path.cwd() / "runtime" / "artifacts")) / "p6_17_graph_outputs"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = _artifact_filename(str(node.get("title") or "pados-output"), artifact_dir)
    artifact_path.write_text(markdown, encoding="utf-8", newline="\n")
    relative_path = artifact_path.relative_to(Path.cwd()).as_posix()
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    artifact_row = repository.create_artifact(
        session,
        artifact_id=str(uuid4()),
        artifact_type="GRAPH_OUTPUT_MARKDOWN",
        relative_path=relative_path,
        sha256=digest,
        size_bytes=len(markdown.encode("utf-8")),
    )
    metadata["artifact"] = {
        "id": str(artifact_row["id"]),
        "artifact_type": "GRAPH_OUTPUT_MARKDOWN",
        "relative_path": relative_path,
        "sha256": digest,
        "size_bytes": len(markdown.encode("utf-8")),
        "media_type": "text/markdown",
        "title": str(node.get("title") or "Untitled output"),
        "status": "SAVED",
        "created_at": datetime.now(UTC).isoformat(),
    }
    if _is_living_synthesis(node):
        metadata["artifact"].update(
            {
                "synthesis_id": metadata.get("synthesis_id"),
                "synthesis_version": metadata.get("synthesis_version"),
                "context_fingerprint": metadata.get("context_fingerprint"),
                "frozen_content_sha256": digest,
            }
        )
    updated = repository.update_node(
        session,
        workspace_id,
        node_id,
        SystemNodeUpdate(version=int(node["version"]), metadata=metadata),
    )
    return _safe_dict({"node": updated, "artifact": artifact_row, "human_status": "SAVED"})


def create_workspace(session: Session, payload: WorkspaceCreate) -> dict[str, Any]:
    try:
        return _safe_dict(repository.create_workspace(session, payload))
    except Exception as exc:
        raise GraphError("WORKSPACE_CREATE_FAILED", "workspace could not be created") from exc


def list_workspaces(session: Session) -> list[dict[str, Any]]:
    workspaces = _safe_dicts(repository.list_workspaces(session))
    for workspace in workspaces:
        safe = _is_human_safe_workspace(workspace)
        workspace["workspace_kind"] = "PERSONAL" if safe else "REVIEW_OR_ADVANCED"
        workspace["human_safe"] = safe
    return workspaces


def workspace_bundle(session: Session, workspace_id: str) -> dict[str, Any]:
    try:
        workspace = repository.get_workspace(session, workspace_id)
    except LookupError as exc:
        raise GraphError("WORKSPACE_NOT_FOUND", "workspace not found", status_code=404) from exc
    raw_nodes = repository.list_nodes(session, workspace_id)
    raw_edges = repository.list_edges(session, workspace_id)
    nodes, synthesis_history = _annotate_living_syntheses(workspace_id, raw_nodes, raw_edges)
    if _is_human_safe_workspace(workspace):
        nodes = [node for node in nodes if _is_human_visible_node(node)]
    visible_ids = {str(node["id"]) for node in nodes}
    edges = [
        edge
        for edge in raw_edges
        if str(edge["source_node_id"]) in visible_ids and str(edge["target_node_id"]) in visible_ids
    ]
    return _safe_dict(
        {
            "workspace": workspace,
            "nodes": nodes,
            "edges": edges,
            "groups": repository.list_groups(session, workspace_id),
            "incubation_items": repository.list_cognitive_items(session, workspace_id),
            "synthesis_history": synthesis_history,
        }
    )


def create_node(session: Session, workspace_id: str, payload: NodeCreate) -> dict[str, Any]:
    _workspace(session, workspace_id)
    persisted = payload if isinstance(payload, SystemNodeCreate) else SystemNodeCreate(**payload.model_dump())
    try:
        return _safe_dict(repository.create_node(session, workspace_id, persisted))
    except LookupError as exc:
        raise GraphError("GRAPH_REFERENCE_NOT_FOUND", "node group or workspace was not found", status_code=404) from exc


def ingest_url_resource(
    session: Session,
    workspace_id: str,
    payload: UrlIngestRequest,
) -> dict[str, Any]:
    _workspace(session, workspace_id)
    fetched = retrieve_url_resource(payload)
    return persist_url_resource(session, workspace_id, payload, fetched)


def retrieve_url_resource(payload: UrlIngestRequest) -> url_ingest.UrlIngestResult:
    try:
        return url_ingest.fetch_url(payload.url)
    except url_ingest.UrlIngestError as exc:
        status_code = 400 if exc.status == "BLOCKED" else 502
        raise GraphError(exc.code, exc.message, status_code=status_code) from exc


def persist_url_resource(
    session: Session,
    workspace_id: str,
    payload: UrlIngestRequest,
    fetched: url_ingest.UrlIngestResult,
) -> dict[str, Any]:
    _workspace(session, workspace_id)
    retrieval = fetched.model_dump()
    metadata = {
        "source_type": "URL",
        "original_url": fetched.original_url,
        "final_url": fetched.final_url,
        "fetched_at": fetched.fetched_at,
        "content_type": fetched.content_type,
        "source_host": fetched.source_host,
        "retrieval_status": fetched.retrieval_status,
        "content_hash": fetched.content_hash,
        "extracted_text_hash": fetched.extracted_text_hash,
        **fetched.metadata,
    }
    for key in ("title", "author", "publication_date", "canonical_url"):
        value = getattr(fetched, key)
        if value is not None:
            metadata[key] = value
    node = create_node(
        session,
        workspace_id,
        SystemNodeCreate(
            semantic_type=NodeType.RESOURCE,
            title=(fetched.title or f"Resource from {fetched.source_host}")[:240],
            content=fetched.extracted_text[:20_000],
            x=payload.x,
            y=payload.y,
            provenance=Provenance.IMPORTED,
            assurance_status=AssuranceStatus.PROVISIONAL,
            lineage={"source_type": "URL", "source_node_ids": []},
            metadata=metadata,
        ),
    )
    return {"node": node, "retrieval": retrieval}


def ensure_workspace(session: Session, workspace_id: str) -> None:
    _workspace(session, workspace_id)


def update_node(
    session: Session,
    workspace_id: str,
    node_id: str,
    payload: NodeUpdate | SystemNodeUpdate,
) -> dict[str, Any]:
    try:
        current = repository.get_node(session, workspace_id, node_id)
        persisted = payload if isinstance(payload, SystemNodeUpdate) else SystemNodeUpdate(**payload.model_dump(exclude_unset=True))
        if persisted.idea_state is not None and current["semantic_type"] != NodeType.IDEA.value:
            raise GraphError("NOT_AN_IDEA", "idea lifecycle is valid only for IDEA nodes")
        return _safe_dict(repository.update_node(session, workspace_id, node_id, persisted))
    except LookupError as exc:
        raise GraphError(
            "NODE_VERSION_CONFLICT",
            "node was not found or its version changed; reload before editing",
        ) from exc


def delete_node(session: Session, workspace_id: str, node_id: str) -> None:
    if not repository.delete_node(session, workspace_id, node_id):
        raise GraphError("NODE_NOT_FOUND", "node not found", status_code=404)


def create_edge(session: Session, workspace_id: str, payload: EdgeCreate) -> dict[str, Any]:
    persisted = payload if isinstance(payload, SystemEdgeCreate) else SystemEdgeCreate(**payload.model_dump())
    try:
        return _safe_dict(repository.create_edge(session, workspace_id, persisted))
    except LookupError as exc:
        raise GraphError("EDGE_ENDPOINT_INVALID", "both edge endpoints must exist in this workspace") from exc
    except Exception as exc:
        raise GraphError("EDGE_CONFLICT", "this relationship already exists or is invalid") from exc


def delete_edge(session: Session, workspace_id: str, edge_id: str) -> None:
    if not repository.delete_edge(session, workspace_id, edge_id):
        raise GraphError("EDGE_NOT_FOUND", "edge not found", status_code=404)


def create_group(session: Session, workspace_id: str, payload: GroupCreate) -> dict[str, Any]:
    try:
        return _safe_dict(repository.create_group(session, workspace_id, payload))
    except LookupError as exc:
        raise GraphError("GROUP_SELECTION_INVALID", "all grouped nodes must belong to the workspace") from exc


def search(session: Session, workspace_id: str, query: str, limit: int = 25) -> list[dict[str, Any]]:
    workspace = _workspace(session, workspace_id)
    results = repository.search_nodes(session, workspace_id, query, limit)
    if _is_human_safe_workspace(workspace):
        results = [node for node in results if _is_human_visible_node(node)]
    return _safe_dicts(results)


def search_sources(session: Session, workspace_id: str, node_ids: Sequence[str]) -> dict[str, Any]:
    """Expose the future web-research seam without pretending it is configured."""

    context = selection_context(session, workspace_id, node_ids)
    nodes = context.get("nodes", [])
    query = " ".join(str(node.get("title") or node.get("content") or "") for node in nodes).strip()
    try:
        sources = _SOURCE_DISCOVERY.discover(query, limit=5)
    except SourceDiscoveryError as exc:
        raise GraphError(exc.code, exc.message, status_code=503) from exc
    return {"query": query, "sources": list(sources)}


def interpret_intent(session: Session, workspace_id: str, payload: IntentRequest) -> dict[str, Any]:
    """Create a goal-aware cognitive scope without executing external work."""

    workspace = _workspace(session, workspace_id)
    route = intent_routing.route_intent(payload.text)
    if not route.interpreted_goal:
        raise GraphError("INTENT_TEXT_REQUIRED", "Tell PADOS what you want to think about or do.", status_code=400)
    existing_nodes = repository.list_nodes(session, workspace_id)
    prior_goals = [
        {
            **dict(_output_metadata(node)),
            "id": str(node.get("id") or ""),
            "interpreted_goal": str(node.get("title") or ""),
        }
        for node in reversed(existing_nodes)
        if str(node.get("semantic_type")) == NodeType.IDEA.value
        and (_output_metadata(node).get("scope_id") or _output_metadata(node).get("intent_class"))
    ]
    goal = _GOAL_INTERPRETER.interpret(
        payload.text,
        intent_class=route.intent_class.value,
        prior_goals=prior_goals,
    )
    decomposition = _PROBLEM_DECOMPOSER.decompose(goal)
    prior_node = next(
        (
            node
            for node in reversed(existing_nodes)
            if str(_output_metadata(node).get("scope_id") or "") == str(goal.parent_scope_id or "")
        ),
        None,
    )
    node_metadata = {
        "intent_class": route.intent_class.value,
        "intent_lane": route.lane.value,
        "intent_confidence": route.confidence,
        "interpreted_goal": route.interpreted_goal,
        "next_actions": list(route.next_actions),
        "source": "UNIVERSAL_INPUT",
        "topic": goal.topic,
        "topic_terms": list(goal.topic_terms),
        "original_objective": goal.original_objective,
        "current_objective": goal.current_objective,
        "decision": goal.decision,
        "synthesis_mode": goal.synthesis_mode.value,
        "scope_id": goal.scope_id,
        "scope_title": goal.scope_title,
        "scope_relation": goal.scope_relation.value,
        "objective_changed": goal.objective_changed,
        "parent_scope_id": goal.parent_scope_id,
        "root_question": goal.original_objective,
        "current_focus": goal.current_objective,
        "decomposition_persistence": "PANEL_ONLY",
        "decomposition": decomposition,
        "cognitive_scope": goal.as_dict(),
    }
    node = create_node(
        session,
        workspace_id,
        SystemNodeCreate(
            semantic_type=NodeType.IDEA,
            title=route.interpreted_goal,
            content=route.interpreted_goal,
            x=payload.x,
            y=payload.y,
            metadata=node_metadata,
        ),
    )
    # Decomposition is a reasoning-panel structure, not user knowledge.  It
    # remains in the IDEA metadata and response contract until a human or a
    # strict provider proposal gives one semantic content worth promoting.
    decomposition_nodes: list[dict[str, Any]] = []
    if prior_node is not None and str(prior_node.get("id")) != str(node.get("id")):
        repository.create_edge(
            session,
            workspace_id,
            SystemEdgeCreate(
                source_node_id=str(prior_node["id"]),
                target_node_id=str(node["id"]),
                relationship_type="EXPANDS" if goal.objective_changed else "RELATES_TO",
                label=goal.scope_relation.value,
                provenance=Provenance.SYSTEM_PROPOSED,
                metadata={"scope_relation": goal.scope_relation.value},
            ),
        )
    all_nodes = repository.list_nodes(session, workspace_id)
    all_edges = repository.list_edges(session, workspace_id)
    assembly = _CONTEXT_ASSEMBLER.assemble(
        workspace_id=workspace_id,
        root_node_ids=[str(node["id"])],
        intent=goal.synthesis_mode.value,
        action=goal.synthesis_mode.value,
        nodes=all_nodes,
        edges=all_edges,
    )
    gaps = _GAP_DETECTOR.detect(goal, decomposition, assembly)
    assembly["gaps"] = gaps
    specification = _SYNTHESIS_PLANNER.plan(goal, decomposition, assembly)
    plan = intent_routing.cognitive_plan(route)
    plan.update(
        {
            "cognitive_scope": goal.as_dict(),
            "scope_relation": goal.scope_relation.value,
            "objective_changed": goal.objective_changed,
            "subquestions": decomposition,
            "gaps": gaps,
            "evidence_summary": {
                "reference_count": len(assembly.get("resource_references", [])),
                "evidence_count": len(assembly.get("evidence", [])),
            },
            "synthesis_mode": goal.synthesis_mode.value,
            "synthesis_spec": specification,
        }
    )
    route_payload = route.as_dict()
    route_payload.update(
        {
            "cognitive_scope": goal.as_dict(),
            "scope_relation": goal.scope_relation.value,
            "objective_changed": goal.objective_changed,
            "synthesis_mode": goal.synthesis_mode.value,
        }
    )
    return _safe_dict(
        {
            "node": node,
            "intent": route_payload,
            "next_actions": list(route.next_actions),
            "cognitive_scope": goal.as_dict(),
            "plan": plan,
            "decomposition": decomposition,
            "decomposition_nodes": decomposition_nodes,
            "gaps": gaps,
            "evidence": assembly.get("evidence", []),
            "references": assembly.get("resource_references", []),
            "linked_previous_scope": prior_node,
            "workspace_kind": "PERSONAL" if _is_human_safe_workspace(workspace) else "REVIEW_OR_ADVANCED",
        }
    )


def _workspace(session: Session, workspace_id: str) -> dict[str, Any]:
    try:
        return repository.get_workspace(session, workspace_id)
    except LookupError as exc:
        raise GraphError("WORKSPACE_NOT_FOUND", "workspace not found", status_code=404) from exc


def _route_for_nodes(nodes: Sequence[Mapping[str, Any]]) -> intent_routing.IntentRoute:
    for node in nodes:
        metadata = node.get("metadata_json")
        if isinstance(metadata, Mapping) and metadata.get("intent_class"):
            try:
                intent_class = intent_routing.IntentClass(str(metadata["intent_class"]))
                lane = intent_routing.IntentLane(str(metadata.get("intent_lane", "COGNITIVE")))
                goal = str(metadata.get("interpreted_goal") or node.get("title") or "")
                return intent_routing.IntentRoute(
                    intent_class,
                    lane,
                    goal,
                    str(metadata.get("intent_confidence", "MEDIUM")),
                    tuple(metadata.get("next_actions", intent_routing.route_intent(goal).next_actions)),
                )
            except (TypeError, ValueError):
                pass
    if any(str(node.get("semantic_type")) == NodeType.ACTION.value for node in nodes):
        return intent_routing.route_intent("run " + " ".join(str(node.get("title") or "") for node in nodes))
    return intent_routing.route_intent(" ".join(str(node.get("title") or "") for node in nodes))


def selection_context(
    session: Session,
    workspace_id: str,
    node_ids: Sequence[str],
) -> dict[str, Any]:
    _workspace(session, workspace_id)
    nodes = repository.get_nodes(session, workspace_id, node_ids)
    if len(nodes) != len(node_ids):
        raise GraphError("SELECTION_INVALID", "selection contains a node outside this workspace")
    selected = set(node_ids)
    edges = [
        edge
        for edge in repository.list_edges(session, workspace_id)
        if str(edge["source_node_id"]) in selected and str(edge["target_node_id"]) in selected
    ]
    return _safe_dict({"workspace_id": workspace_id, "nodes": nodes, "edges": edges})


def _provider_reason(settings: Settings) -> ActionDisabledReason | None:
    try:
        check_chat_provider_configured(settings, allow_mock=settings.env in _ALLOW_MOCK_ENVS)
    except ProviderNotConfiguredError:
        return ActionDisabledReason(
            code="COGNITIVE_PROVIDER_NOT_CONFIGURED",
            message="Configure an approved local/model provider before invoking cognitive actions.",
        )
    return None


def _harness_loop_from_nodes(
    session: Session,
    workspace: Mapping[str, Any],
    nodes: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, ActionDisabledReason | None]:
    loop_ids = {
        str(node.get("internal_ref_id"))
        for node in nodes
        if node.get("semantic_type") == NodeType.ACTION.value
        and node.get("internal_ref_type") == "HARNESS_LOOP"
        and node.get("internal_ref_id")
    }
    if len(loop_ids) != 1:
        return None, ActionDisabledReason(
            code="HARNESS_BINDING_REQUIRED",
            message="Run requires one selected ACTION linked to an approved PADOS Harness loop.",
        )
    try:
        loop = repository.harness_loop(session, next(iter(loop_ids)))
    except LookupError:
        return None, ActionDisabledReason(
            code="HARNESS_LOOP_NOT_FOUND",
            message="The linked Harness loop no longer exists.",
        )
    workspace_project_id = workspace.get("project_id")
    if workspace_project_id is None:
        return None, ActionDisabledReason(
            code="WORKSPACE_PROJECT_BINDING_REQUIRED",
            message="Run requires a workspace explicitly bound to the Harness loop project.",
        )
    if str(loop["project_id"]) != str(workspace_project_id):
        return None, ActionDisabledReason(
            code="HARNESS_PROJECT_SCOPE_MISMATCH",
            message="The selected Harness loop belongs to a different project.",
        )
    if loop["status"] != "READY":
        return None, ActionDisabledReason(
            code="HARNESS_LOOP_NOT_READY",
            message=f"Harness loop is {loop['status']}; only READY may be started.",
        )
    return loop, None


def selection_actions(
    session: Session,
    settings: Settings,
    workspace_id: str,
    node_ids: Sequence[str],
    *,
    control_authorized: bool = False,
) -> list[dict[str, Any]]:
    context = selection_context(session, workspace_id, node_ids)
    nodes = context["nodes"]
    workspace = _workspace(session, workspace_id)
    route = _route_for_nodes(nodes)
    provider_reason = _provider_reason(settings)
    actions: list[SelectionAction] = []
    for action in ("THINK", "EXPAND"):
        reasons = [provider_reason] if provider_reason else []
        actions.append(
            SelectionAction(
                id=action,
                label=action.title(),
                allowed=not reasons,
                disabled_reasons=reasons,
                intent_class=route.intent_class.value,
                lane=route.lane.value,
                primary_message="這會在心智圖中產生暫時性的思考結果。" if route.lane == intent_routing.IntentLane.COGNITIVE else "這會先整理執行前的工作內容。",
            )
        )
    connect_reasons: list[ActionDisabledReason] = []
    if len(nodes) < 2:
        connect_reasons.append(
            ActionDisabledReason(code="MULTI_SELECTION_REQUIRED", message="Select at least two nodes to discover a connection.")
        )
    if provider_reason:
        connect_reasons.append(provider_reason)
    actions.append(
        SelectionAction(
            id="CONNECT",
            label="Connect",
            allowed=not connect_reasons,
            disabled_reasons=connect_reasons,
            intent_class=route.intent_class.value,
            lane=route.lane.value,
            primary_message="這會把相關概念連回心智圖。",
        )
    )
    actions.append(
        SelectionAction(
            id="PLAN",
            label="Plan",
            allowed=True,
            disabled_reasons=[],
            intent_class=route.intent_class.value,
            lane=route.lane.value,
            primary_message="這會建立學習／研究路線。" if route.lane == intent_routing.IntentLane.COGNITIVE else "這會建立受治理的執行計畫。",
        )
    )
    run_reason: ActionDisabledReason | None
    if route.lane == intent_routing.IntentLane.COGNITIVE:
        run_reason = ActionDisabledReason(
            code="COGNITIVE_GOAL_NO_EXTERNAL_RUN",
            message="目前這是一個思考／學習目標，沒有需要執行的外部工作。",
        )
    else:
        _loop, run_reason = _harness_loop_from_nodes(session, workspace, nodes)
        if run_reason is None and not control_authorized:
            run_reason = ActionDisabledReason(
                code="CONTROL_AUTH_REQUIRED",
                message="Run requires an authenticated PADOS control request.",
            )
    actions.append(
        SelectionAction(
            id="RUN",
            label="Run",
            allowed=run_reason is None,
            disabled_reasons=[run_reason] if run_reason else [],
            intent_class=route.intent_class.value,
            lane=route.lane.value,
            primary_message="執行只會在 PADOS 確認這是外部工作後出現。" if route.lane == intent_routing.IntentLane.COGNITIVE else "這會遵循既有的 Harness 治理流程。",
        )
    )
    return [action.model_dump(mode="json") for action in actions]


def capability_views() -> list[dict[str, Any]]:
    capabilities: list[dict[str, Any]] = []
    for capability_id, spec in capability_registry().items():
        capabilities.append(
            {
                "id": capability_id.value,
                "name": capability_id.value.replace("_", " ").title(),
                "description": spec.description,
                "capability_kind": "SKILL",
                "status": "REGISTERED",
                "version": "p6.9-registry-v1",
                "provenance": {"source": "pados.worker.registry", "source_version": "P6.9"},
                "scope": {
                    "minimum_risk": spec.minimum_risk_level.value,
                    "requires_isolation": spec.requires_isolation,
                },
                "evidence_summary": {
                    "validation_required": spec.requires_validation,
                    "required_approval_type": spec.required_approval_type,
                },
                "assurance": {
                    "lifecycle": "PROPOSED",
                    "confidence": "REGISTRY_DISCOVERED_NOT_ASSURANCE_VALIDATED",
                },
            }
        )
    return capabilities


def reuse_capability(
    session: Session,
    workspace_id: str,
    capability_id: str,
    payload: CapabilityReuseRequest,
) -> dict[str, Any]:
    available = {item["id"]: item for item in capability_views()}
    capability = available.get(capability_id)
    if capability is None:
        raise GraphError("CAPABILITY_NOT_FOUND", "capability not found", status_code=404)
    lineage = {
        "source_capability_id": capability_id,
        "source_version": capability["version"],
        "reuse_mode": payload.reuse_mode.value,
        "propagates_updates": payload.reuse_mode == ReuseMode.REFERENCE,
    }
    if payload.reuse_mode == ReuseMode.FORK:
        lineage["parent_capability_id"] = capability_id
    return create_node(
        session,
        workspace_id,
        SystemNodeCreate(
            semantic_type=NodeType.EXPERT,
            title=str(capability["name"]),
            content=str(capability["description"]),
            x=payload.x,
            y=payload.y,
            provenance=Provenance.SYSTEM_PROPOSED,
            source_capability_id=capability_id,
            reuse_mode=payload.reuse_mode,
            source_version=str(capability["version"]),
            parent_capability_id=capability_id if payload.reuse_mode == ReuseMode.FORK else None,
            lineage=lineage,
            metadata={"capability_kind": capability["capability_kind"]},
        ),
    )


def compile_plan(
    session: Session,
    workspace_id: str,
    node_ids: Sequence[str],
    *,
    control_authorized: bool = False,
) -> dict[str, Any]:
    context = selection_context(session, workspace_id, node_ids)
    nodes = context["nodes"]
    edges = context["edges"]
    workspace = _workspace(session, workspace_id)
    route = _route_for_nodes(nodes)
    if route.lane == intent_routing.IntentLane.COGNITIVE:
        plan = intent_routing.cognitive_plan(route)
        plan["selected_context"] = [
            {"id": node["id"], "type": node["semantic_type"], "title": node["title"]} for node in nodes
        ]
        intent = repository.insert_intent(
            session,
            workspace_id=workspace_id,
            action="PLAN",
            selected_node_ids=node_ids,
            selection_context=context,
            interpreted_goal=str(plan["interpreted_goal"]),
            plan=plan,
            governance={"lane": "COGNITIVE", "run_authority": "NONE", "direct_react_execution": False},
            status="PROPOSED",
        )
        return _safe_dict({"intent": intent, "plan": plan})
    _loop, run_reason = _harness_loop_from_nodes(session, workspace, nodes)
    if run_reason is None and not control_authorized:
        run_reason = ActionDisabledReason(
            code="CONTROL_AUTH_REQUIRED",
            message="Run requires an authenticated PADOS control request.",
        )
    goal_node = next((node for node in nodes if node["semantic_type"] == NodeType.ACTION.value), nodes[0])
    proposed_actions = []
    for index, node in enumerate(nodes, start=1):
        proposed_actions.append(
            {
                "sequence": index,
                "title": node["title"],
                "kind": "EXECUTE" if node["semantic_type"] == NodeType.ACTION.value else "ANALYZE",
                "node_id": node["id"],
                "capability_id": node.get("source_capability_id"),
            }
        )
    plan = {
        "interpreted_goal": goal_node["title"],
        "intent_class": route.intent_class.value,
        "lane": route.lane.value,
        "selected_context": [{"id": node["id"], "type": node["semantic_type"], "title": node["title"]} for node in nodes],
        "dependencies": [
            {
                "source": edge["source_node_id"],
                "target": edge["target_node_id"],
                "relationship": edge["relationship_type"],
            }
            for edge in edges
        ],
        "proposed_actions": proposed_actions,
        "proposed_executor": next(
            (
                {"capability_id": node["source_capability_id"], "mode": node.get("reuse_mode")}
                for node in nodes
                if node.get("source_capability_id")
            ),
            None,
        ),
        "governance_requirements": [
            "Harness approval before execution",
            "registered worker capability",
            "validation evidence for state-changing work",
        ],
        "blockers": [run_reason.code] if run_reason else [],
        "expected_output": "A governed result and evidence receipt linked to the selected graph context.",
    }
    intent = repository.insert_intent(
        session,
        workspace_id=workspace_id,
        action="PLAN",
        selected_node_ids=node_ids,
        selection_context=context,
        interpreted_goal=str(plan["interpreted_goal"]),
        plan=plan,
        governance={"run_authority": "PADOS_HARNESS", "direct_react_execution": False},
        status="PROPOSED",
    )
    return _safe_dict({"intent": intent, "plan": plan})


def run_selection(
    session: Session,
    workspace_id: str,
    node_ids: Sequence[str],
) -> dict[str, Any]:
    context = selection_context(session, workspace_id, node_ids)
    route = _route_for_nodes(context["nodes"])
    if route.lane == intent_routing.IntentLane.COGNITIVE:
        raise GraphError(
            "COGNITIVE_GOAL_NO_EXTERNAL_RUN",
            "目前這是一個思考／學習目標，沒有需要執行的外部工作。",
        )
    workspace = _workspace(session, workspace_id)
    loop, reason = _harness_loop_from_nodes(session, workspace, context["nodes"])
    if reason is not None or loop is None:
        raise GraphError(reason.code if reason else "RUN_BLOCKED", reason.message if reason else "Run is blocked")
    started = loop_service.start_loop(session, str(loop["id"]), owner_instance="graph-api")
    intent = repository.insert_intent(
        session,
        workspace_id=workspace_id,
        action="RUN",
        selected_node_ids=node_ids,
        selection_context=context,
        interpreted_goal="Start approved Harness loop",
        plan={"harness_loop_id": str(loop["id"])},
        governance={"authority": "PADOS_HARNESS", "approval_id": str(loop["approval_id"])},
        status="STARTED",
        harness_loop_id=str(loop["id"]),
    )
    return _safe_dict({"intent": intent, "loop": started})


def queue_cognitive_operation(
    session: Session,
    workspace_id: str,
    operation: CognitiveOperation,
    node_ids: Sequence[str],
    *,
    idea_node_id: str | None = None,
    model_selection: ModelSelection | None = None,
) -> dict[str, Any]:
    context = selection_context(session, workspace_id, node_ids)
    selected_nodes = context["nodes"]
    goal_node = next((node for node in selected_nodes if str(node.get("semantic_type")) == NodeType.IDEA.value), selected_nodes[0])
    goal_metadata = _output_metadata(goal_node)
    nested_goal = goal_metadata.get("cognitive_goal")
    nested_goal = nested_goal if isinstance(nested_goal, Mapping) else {}
    goal_text = str(
        goal_metadata.get("current_objective")
        or nested_goal.get("current_objective")
        or goal_node.get("content")
        or goal_node.get("title")
        or ""
    )
    prior_goal = {
        **dict(goal_metadata),
        "interpreted_goal": str(goal_node.get("title") or goal_text),
        "id": str(goal_node.get("id") or ""),
        "current_objective": goal_text,
    }
    goal = _GOAL_INTERPRETER.interpret(
        goal_text,
        intent_class=str(goal_metadata.get("intent_class") or "COGNITIVE"),
        prior_goals=[prior_goal] if goal_metadata.get("scope_id") else [],
    )
    stored_decomposition = goal_metadata.get("decomposition")
    decomposition = (
        [dict(item) for item in stored_decomposition if isinstance(item, Mapping)]
        if isinstance(stored_decomposition, list)
        else _PROBLEM_DECOMPOSER.decompose(goal)
    )
    if hasattr(session, "execute"):
        all_nodes = repository.list_nodes(session, workspace_id)
        all_edges = repository.list_edges(session, workspace_id)
    else:
        # Keep lightweight service tests that stub selection_context independent
        # of the SQLAlchemy repository while real sessions retain full grounding.
        all_nodes = list(selected_nodes)
        all_edges = list(context.get("edges", []))
    prior_output = next(
        (
            candidate
            for candidate in reversed(all_nodes)
            if str(candidate.get("id") or "") not in {str(node_id) for node_id in node_ids}
            if _is_living_synthesis(candidate)
            and (
                str(_output_metadata(candidate).get("scope_id") or "") == str(goal.parent_scope_id or "")
                or (
                    goal.scope_relation == ScopeRelation.SAME_SCOPE_UPDATE
                    and str(_output_metadata(candidate).get("scope_id") or "") == str(goal.scope_id)
                )
            )
        ),
        None,
    )
    previous_synthesis = (
        {
            "id": str(prior_output.get("id")),
            "version": _synthesis_version(prior_output),
            "title": str(prior_output.get("title") or ""),
            "content": str(prior_output.get("content") or "")[:1_200],
        }
        if prior_output is not None
        else None
    )
    assembly = _CONTEXT_ASSEMBLER.assemble(
        workspace_id=workspace_id,
        root_node_ids=node_ids,
        intent=operation.value,
        action=operation.value,
        nodes=all_nodes,
        edges=all_edges,
        previous_synthesis=previous_synthesis,
        prior_synthesis_version=(
            _synthesis_version(prior_output) if prior_output is not None else None
        ),
    )
    assembly["gaps"] = _GAP_DETECTOR.detect(goal, decomposition, assembly)
    context["context_assembly"] = assembly
    context["cognitive_goal"] = goal.as_dict()
    context["decomposition"] = decomposition
    context["synthesis_spec"] = _SYNTHESIS_PLANNER.plan(
        goal,
        decomposition,
        assembly,
    )
    if model_selection is not None:
        context["ai_runtime"] = model_selection.model_dump(mode="json")
    return _safe_dict(
        repository.create_cognitive_item(
            session,
            workspace_id=workspace_id,
            operation=operation.value,
            selected_node_ids=node_ids,
            selection_context=context,
            idea_node_id=idea_node_id,
        )
    )


def resolve_model_selection(
    settings: Settings,
    selection: ModelSelection,
) -> Settings:
    configured = {
        ModelSlot.THINKING: (settings.chat_provider, settings.chat_model),
    }
    if selection.slot not in configured:
        raise GraphError(
            "MODEL_SLOT_NOT_EXECUTABLE",
            "the selected model slot is not available for cognitive actions",
            status_code=400,
        )
    provider, configured_model = configured[selection.slot]
    if not provider or selection.provider.casefold() != provider.casefold():
        raise GraphError(
            "MODEL_PROVIDER_NOT_CONFIGURED",
            "the selected provider is not configured for this model slot",
            status_code=400,
        )
    if selection.provider.casefold() == "mock" and selection.model not in {"", *MOCK_MODEL_CHOICES}:
        raise GraphError(
            "MODEL_NOT_CONFIGURED",
            "the selected model is not configured for this provider",
            status_code=400,
        )
    model = selection.model or configured_model
    if selection.provider.casefold() != "mock" and not model:
        raise GraphError(
            "MODEL_NOT_CONFIGURED",
            "the selected model is not configured",
            status_code=400,
        )
    if selection.provider.casefold() != "mock" and model != configured_model:
        raise GraphError(
            "MODEL_NOT_CONFIGURED",
            "the selected model is not the model configured for this slot",
            status_code=400,
        )
    return settings.model_copy(
        update={"chat_provider": selection.provider.casefold(), "chat_model": model}
    )


def ai_runtime_settings(settings: Settings, *, discover: bool = False) -> dict[str, Any]:
    return ai_runtime_catalog(settings, discover=discover)


def incubate_idea(session: Session, workspace_id: str, node_id: str) -> dict[str, Any]:
    node = repository.get_node(session, workspace_id, node_id)
    if node["semantic_type"] != NodeType.IDEA.value:
        raise GraphError("NOT_AN_IDEA", "only an IDEA node may be incubated")
    updated = update_node(
        session,
        workspace_id,
        node_id,
        SystemNodeUpdate(idea_state=IdeaState.INCUBATING, version=int(node["version"])),
    )
    item = queue_cognitive_operation(
        session,
        workspace_id,
        CognitiveOperation.INCUBATE,
        [node_id],
        idea_node_id=node_id,
    )
    return {"node": updated, "work_item": item}


async def process_cognitive_item(
    settings: Settings,
    item_id: str,
) -> dict[str, Any]:
    try:
        with session_scope(settings) as session:
            item = repository.mark_cognitive_processing(session, item_id)
    except LookupError as exc:
        raise GraphError("COGNITIVE_ITEM_NOT_QUEUED", "cognitive item is not queued") from exc
    context = cast(dict[str, Any], json_safe(item["selection_context"]))
    operation = CognitiveOperation(str(item["operation"]))
    effective_settings = settings
    selected_runtime = context.get("ai_runtime")
    if isinstance(selected_runtime, Mapping):
        try:
            effective_settings = resolve_model_selection(
                settings,
                ModelSelection.model_validate(selected_runtime),
            )
        except (GraphError, ValueError) as exc:
            with session_scope(settings) as session:
                repository.fail_cognitive_item(
                    session,
                    item_id,
                    error_code="MODEL_SELECTION_INVALID",
                )
            if isinstance(exc, GraphError):
                raise GraphError(
                    exc.code,
                    exc.message,
                    status_code=exc.status_code,
                    write_occurred=True,
                ) from exc
            raise GraphError(
                "MODEL_SELECTION_INVALID",
                "the selected model configuration is invalid",
                status_code=400,
                write_occurred=True,
            ) from exc
    try:
        provider = _cognitive_provider(settings, effective_settings, str(item["workspace_id"]))
        specification = context.get("synthesis_spec")
        if not isinstance(specification, Mapping):
            specification = context
        prompt = cognition.build_prompt(operation, {"synthesis_spec": specification})
        estimated_provider_tokens = await provider.estimate_context(prompt)
        _enforce_provider_context_budget(specification, int(estimated_provider_tokens))
        response = await provider.chat(prompt, response_schema=cognition.COGNITIVE_RESPONSE_SCHEMA)
        measured_execution = _measured_evidence(provider)
        provider_response = cognition.parse_cognitive_response(response)
        cognition.validate_evidence_refs(
            provider_response,
            [
                str(item.get("evidence_id"))
                for item in specification.get("evidence", [])
                if isinstance(item, Mapping) and item.get("evidence_id")
            ],
        )
    except ExecutionBindingRefused as exc:
        with session_scope(settings) as session:
            repository.fail_cognitive_item(session, item_id, error_code=exc.refusal.code, status="BLOCKED")
        raise _measured_refusal_error(exc.refusal, write_occurred=True) from exc
    except Exception as exc:
        failure_code = _provider_failure_code(exc)
        failure_status = "BLOCKED" if failure_code == "PROVIDER_UNAVAILABLE" else "FAILED"
        with session_scope(settings) as session:
            repository.fail_cognitive_item(session, item_id, error_code=failure_code, status=failure_status)
        raise GraphError(
            failure_code,
            _provider_failure_message(failure_code),
            status_code=503 if failure_code == "PROVIDER_UNAVAILABLE" else 502,
            write_occurred=True,
        ) from exc
    candidate_response = provider_response
    candidates = [
        {
            "type": NodeType.OUTPUT.value,
            "title": str(candidate_response.get("title") or f"{operation.value.title()} candidate")[:240],
            "content": str(candidate_response["answer"]),
        }
    ]
    created: list[dict[str, Any]] = []
    nodes = context.get("nodes", [])
    assembly = context.get("context_assembly")
    assembly = assembly if isinstance(assembly, Mapping) else {}
    assembly = dict(assembly)
    provider_receipt = dict(assembly.get("context_receipt") or {})
    provider_receipt["provider_estimated_input_tokens"] = int(estimated_provider_tokens)
    assembly["context_receipt"] = provider_receipt
    assembled_nodes = assembly.get("nodes", [])
    assembled_node_ids = [
        str(node.get("id"))
        for node in assembled_nodes
        if isinstance(node, Mapping) and node.get("id")
    ]
    used_source_ids, reference_source_ids = _cognitive_source_ids(assembly)
    raw_root_ids = assembly.get("root_node_ids", [])
    root_node_ids = (
        [str(node_id) for node_id in raw_root_ids]
        if isinstance(raw_root_ids, Sequence) and not isinstance(raw_root_ids, (str, bytes, bytearray))
        else []
    )
    try:
        with session_scope(settings) as session:
            for index, candidate in enumerate(candidates):
                x, y = cognition.candidate_position(nodes, index)
                living = operation in {CognitiveOperation.THINK, CognitiveOperation.CRITIQUE} and candidate["type"] == NodeType.OUTPUT.value
                synthesis_id = str(uuid4()) if living else None
                metadata: dict[str, Any] = {
                    "cognitive_work_item_id": str(item["id"]),
                    "operation": operation.value,
                    "synthesis_mode": str(specification.get("intent") or operation.value),
                    "result_type": "LIVING_SYNTHESIS" if living else "COGNITIVE_RESULT",
                    "candidate_status": "PROVISIONAL",
                    "created_by": "AI",
                    "provider": effective_settings.chat_provider.casefold(),
                    "model": effective_settings.chat_model,
                    "cognitive_runtime_status": _cognitive_runtime_status(effective_settings, measured_execution),
                    "source_node_ids": used_source_ids or assembled_node_ids,
                    "reference_source_ids": reference_source_ids,
                    **_evidence_provenance(assembly.get("evidence", []), candidate_response),
                    "claims": candidate_response.get("claims", []),
                    "evidence_refs": candidate_response.get("evidence_refs", []),
                    "proposed_graph_changes": candidate_response.get("proposed_graph_changes", []),
                    "limitations": candidate_response.get("limitations", []),
                    "context_receipt": assembly.get("context_receipt", {}),
                    "creation_timestamp": datetime.now(UTC).isoformat(),
                    "lifecycle": "PROVISIONAL",
                }
                if measured_execution is not None:
                    metadata["measured_execution"] = measured_execution
                scope = specification.get("scope")
                scope = scope if isinstance(scope, Mapping) else {}
                if scope:
                    metadata.update(
                        {
                            "scope_id": scope.get("id"),
                            "scope_title": scope.get("title"),
                            "scope_relation": scope.get("relation"),
                            "objective_changed": scope.get("objective_changed", False),
                            "cognitive_goal": specification.get("goal", {}),
                            "decomposition": specification.get("subquestions", []),
                            "gaps": specification.get("gaps", []),
                            "evidence_count": len(specification.get("evidence", [])) if isinstance(specification.get("evidence"), list) else 0,
                            "reference_count": len(specification.get("references", [])) if isinstance(specification.get("references"), list) else 0,
                            "evidence_state": str(assembly.get("evidence_state") or "NO_SOURCE"),
                            "expected_structure": specification.get("expected_structure", []),
                        }
                    )
                prior_spec = specification.get("prior_synthesis")
                if isinstance(prior_spec, Mapping) and prior_spec.get("id"):
                    metadata.update(
                        {
                            "previous_synthesis_node_id": str(prior_spec["id"]),
                            "prior_synthesis_version": int(prior_spec.get("version") or 1),
                        }
                    )
                if living:
                    metadata.update(
                        {
                            "living_synthesis": True,
                            "synthesis_id": synthesis_id,
                            "synthesis_version": 1,
                            "synthesis_status": "CURRENT",
                            "context_fingerprint": assembly.get("context_fingerprint"),
                            "source_snapshot": assembly.get("source_snapshot", {}),
                            "source_summary": assembly.get("source_summary", {}),
                            "source_summary_text": _synthesis_summary(assembly),
                            "root_node_ids": root_node_ids,
                            "intent": operation.value,
                            "action": operation.value,
                            "auto_refresh_policy": AutoRefreshPolicy.PROMPT.value,
                            "stale_reason": None,
                        }
                    )
                created.append(
                    create_node(
                        session,
                        str(item["workspace_id"]),
                        SystemNodeCreate(
                            semantic_type=NodeType(candidate["type"]),
                            title=candidate["title"],
                            content=candidate["content"],
                            x=x,
                            y=y,
                            provenance=Provenance.AI_PROPOSED,
                            lineage={
                                "cognitive_work_item_id": str(item["id"]),
                                **(
                                    {"previous_output_id": str(prior_spec["id"])}
                                    if isinstance(prior_spec, Mapping) and prior_spec.get("id")
                                    else {}
                                ),
                            },
                            metadata=metadata,
                        ),
                    )
                )
                if root_node_ids and created[-1].get("id"):
                    for root_id in root_node_ids[:8]:
                        if str(root_id) == str(created[-1]["id"]):
                            continue
                        repository.create_edge(
                            session,
                            str(item["workspace_id"]),
                            SystemEdgeCreate(
                                source_node_id=str(root_id),
                                target_node_id=str(created[-1]["id"]),
                                relationship_type="PRODUCED",
                                label=str(specification.get("intent") or operation.value),
                                provenance=Provenance.SYSTEM_PROPOSED,
                                metadata={"cognitive_work_item_id": str(item["id"]), "candidate_status": "PROVISIONAL"},
                            ),
                        )
            provider_name = effective_settings.chat_provider.lower()
            completed = repository.complete_cognitive_item(
                session,
                item_id,
                provider=provider_name,
                candidates=[
                    {
                        "node_id": node["id"],
                        "status": "PROVISIONAL",
                        **({"measured_execution": measured_execution} if measured_execution is not None else {}),
                    }
                    for node in created
                ],
            )
    except Exception as exc:
        with session_scope(settings) as session:
            repository.fail_cognitive_item(
                session,
                item_id,
                error_code="COGNITIVE_PERSISTENCE_FAILED",
                status="FAILED",
            )
        raise GraphError(
            "COGNITIVE_PERSISTENCE_FAILED",
            "candidate persistence failed after provider completion",
            status_code=502,
            write_occurred=True,
        ) from exc
    return _safe_dict({"work_item": completed, "candidate_nodes": created})


def _versioned_synthesis_title(title: str, version: int) -> str:
    base = re.sub(r"\s+v\d+$", "", title.strip(), flags=re.IGNORECASE).strip() or "目前整理"
    return base if version == 1 else f"{base} v{version}"


def _history_for_synthesis(
    workspace_id: str,
    nodes: Sequence[Mapping[str, Any]],
    synthesis_id: str,
) -> list[dict[str, Any]]:
    matching = [
        node
        for node in nodes
        if _is_living_synthesis(node)
        and str(_output_metadata(node).get("synthesis_id") or node.get("id")) == synthesis_id
    ]
    latest_id = str(max(matching, key=_synthesis_version).get("id")) if matching else ""
    result: list[dict[str, Any]] = []
    for node in sorted(matching, key=_synthesis_version):
        metadata = _output_metadata(node)
        result.append(
            {
                "id": str(node.get("id")),
                "synthesis_id": synthesis_id,
                "version": _synthesis_version(node),
                "title": str(node.get("title") or ""),
                "status": "CURRENT" if str(node.get("id")) == latest_id else "SUPERSEDED",
                "created_at": node.get("created_at"),
                "context_fingerprint": metadata.get("context_fingerprint"),
            }
        )
    return result


async def refresh_living_synthesis(
    settings: Settings,
    workspace_id: str,
    node_id: str,
    *,
    model_selection: ModelSelection | None = None,
) -> dict[str, Any]:
    """Create the next immutable synthesis version when the semantic context is stale."""

    with session_scope(settings) as session:
        workspace = _workspace(session, workspace_id)
        try:
            requested = repository.get_node(session, workspace_id, node_id)
        except LookupError as exc:
            raise GraphError("SYNTHESIS_NOT_FOUND", "living synthesis not found", status_code=404) from exc
        if not _is_living_synthesis(requested):
            raise GraphError("LIVING_SYNTHESIS_REQUIRED", "the selected node is not a living synthesis", status_code=400)
        requested_metadata = _output_metadata(requested)
        synthesis_id = str(requested_metadata.get("synthesis_id") or requested.get("id"))
        nodes = repository.list_nodes(session, workspace_id)
        edges = repository.list_edges(session, workspace_id)
        versions = [
            node
            for node in nodes
            if _is_living_synthesis(node)
            and str(_output_metadata(node).get("synthesis_id") or node.get("id")) == synthesis_id
        ]
        latest = max(versions, key=_synthesis_version) if versions else requested
        latest_metadata = _output_metadata(latest)
        current_context = _synthesis_current_context(
            workspace_id=workspace_id,
            node=latest,
            nodes=nodes,
            edges=edges,
        )
        stale, reason = cognitive_context.compare_synthesis_context(latest_metadata, current_context)
        if not stale:
            current = dict(latest)
            current_metadata = dict(latest_metadata)
            current_metadata["synthesis_status"] = "CURRENT"
            current["metadata_json"] = current_metadata
            return _safe_dict(
                {
                    "synthesis": current,
                    "previous_synthesis": None,
                    "refreshed": False,
                    "status": "CURRENT",
                    "history": _history_for_synthesis(workspace_id, nodes, synthesis_id),
                }
            )
        root_ids = _root_ids_for_synthesis(latest)
        previous_version = _synthesis_version(latest)
        previous = {
            "id": str(latest.get("id")),
            "version": previous_version,
            "title": str(latest.get("title") or ""),
            "content": str(latest.get("content") or ""),
        }
    refreshed_context = _CONTEXT_ASSEMBLER.assemble(
            workspace_id=workspace_id,
            root_node_ids=root_ids,
            intent=str(latest_metadata.get("intent") or "THINK"),
            action=str(latest_metadata.get("action") or "THINK"),
            nodes=nodes,
            edges=edges,
            previous_synthesis=previous,
            prior_synthesis_version=previous_version,
        )
    goal_text = str(latest_metadata.get("current_objective") or latest.get("title") or "目前認知問題")
    goal = _GOAL_INTERPRETER.interpret(
        goal_text,
        intent_class=str(latest_metadata.get("intent_class") or "COGNITIVE"),
        prior_goals=[dict(latest_metadata)],
    )
    stored_decomposition = latest_metadata.get("decomposition")
    decomposition = (
        [dict(item) for item in stored_decomposition if isinstance(item, Mapping)]
        if isinstance(stored_decomposition, list)
        else _PROBLEM_DECOMPOSER.decompose(goal)
    )
    refreshed_context["gaps"] = _GAP_DETECTOR.detect(goal, decomposition, refreshed_context)
    synthesis_spec = _SYNTHESIS_PLANNER.plan(
        goal,
        decomposition,
        refreshed_context,
        previous_synthesis=previous,
    )
    effective_settings = settings
    if model_selection is not None:
        effective_settings = resolve_model_selection(settings, model_selection)
    try:
        provider = _cognitive_provider(
            settings,
            effective_settings,
            workspace_id,
            project_id=_workspace_project_scope(workspace, workspace_id),
        )
        prompt = cognition.build_prompt(CognitiveOperation.THINK, {"synthesis_spec": synthesis_spec})
        estimated_provider_tokens = await provider.estimate_context(prompt)
        _enforce_provider_context_budget(synthesis_spec, int(estimated_provider_tokens))
        response = await provider.chat(prompt, response_schema=cognition.COGNITIVE_RESPONSE_SCHEMA)
        measured_execution = _measured_evidence(provider)
        provider_response = cognition.parse_cognitive_response(response)
        cognition.validate_evidence_refs(
            provider_response,
            [
                str(item.get("evidence_id"))
                for item in synthesis_spec.get("evidence", [])
                if isinstance(item, Mapping) and item.get("evidence_id")
            ],
        )
    except ExecutionBindingRefused as exc:
        raise _measured_refusal_error(exc.refusal, write_occurred=False) from exc
    except Exception as exc:
        failure_code = _provider_failure_code(exc)
        raise GraphError(
            failure_code,
            _provider_failure_message(failure_code),
            status_code=503 if failure_code == "PROVIDER_UNAVAILABLE" else 502,
        ) from exc
    candidate = {
        "type": NodeType.OUTPUT.value,
        "title": str(provider_response.get("title") or latest.get("title") or "目前整理")[:240],
        "content": str(provider_response["answer"]),
    }
    new_version = previous_version + 1
    used_source_ids, reference_source_ids = _cognitive_source_ids(refreshed_context)
    context_receipt = dict(refreshed_context.get("context_receipt") or {})
    context_receipt["provider_estimated_input_tokens"] = int(estimated_provider_tokens)
    metadata = {
        "living_synthesis": True,
        "result_type": "LIVING_SYNTHESIS",
        "candidate_status": "PROVISIONAL",
        "lifecycle": "PROVISIONAL",
        "created_by": "AI",
        "provider": effective_settings.chat_provider.casefold(),
        "model": effective_settings.chat_model,
        "cognitive_runtime_status": _cognitive_runtime_status(effective_settings, measured_execution),
        "operation": CognitiveOperation.THINK.value,
        "synthesis_mode": str(synthesis_spec.get("intent") or CognitiveOperation.THINK.value),
        "synthesis_id": synthesis_id,
        "synthesis_version": new_version,
        "synthesis_status": "CURRENT",
        "context_fingerprint": refreshed_context.get("context_fingerprint"),
        "source_snapshot": refreshed_context.get("source_snapshot", {}),
        "source_summary": refreshed_context.get("source_summary", {}),
        "source_summary_text": _synthesis_summary(refreshed_context),
        "root_node_ids": root_ids,
        "source_node_ids": used_source_ids or root_ids,
        "reference_source_ids": reference_source_ids,
        **_evidence_provenance(refreshed_context.get("evidence", []), provider_response),
        "claims": provider_response.get("claims", []),
        "evidence_refs": provider_response.get("evidence_refs", []),
        "proposed_graph_changes": provider_response.get("proposed_graph_changes", []),
        "limitations": provider_response.get("limitations", []),
        "context_receipt": context_receipt,
        "intent": str(latest_metadata.get("intent") or "THINK"),
        "action": str(latest_metadata.get("action") or "THINK"),
        "prior_synthesis_version": previous_version,
        "previous_synthesis_node_id": str(latest.get("id")),
        "auto_refresh_policy": str(latest_metadata.get("auto_refresh_policy") or AutoRefreshPolicy.PROMPT.value),
        "stale_reason": None,
        "scope_id": goal.scope_id,
        "scope_title": goal.scope_title,
        "scope_relation": goal.scope_relation.value,
        "objective_changed": goal.objective_changed,
        "cognitive_goal": synthesis_spec.get("goal", {}),
        "decomposition": decomposition,
        "gaps": synthesis_spec.get("gaps", []),
        "evidence_count": len(synthesis_spec.get("evidence", [])),
        "reference_count": len(synthesis_spec.get("references", [])),
        "evidence_state": str(refreshed_context.get("evidence_state") or "NO_SOURCE"),
        "expected_structure": synthesis_spec.get("expected_structure", []),
        "creation_timestamp": datetime.now(UTC).isoformat(),
    }
    if measured_execution is not None:
        metadata["measured_execution"] = measured_execution
    with session_scope(settings) as session:
        created = create_node(
            session,
            workspace_id,
            SystemNodeCreate(
                semantic_type=NodeType.OUTPUT,
                title=_versioned_synthesis_title(str(candidate["title"] or latest.get("title") or "目前整理"), new_version),
                content=candidate["content"],
                x=float(latest.get("x") or 0) + 300,
                y=float(latest.get("y") or 0) + 150,
                provenance=Provenance.AI_PROPOSED,
                lineage={
                    "living_synthesis_id": synthesis_id,
                    "previous_output_id": str(latest.get("id")),
                    "context_fingerprint": refreshed_context.get("context_fingerprint"),
                },
                metadata=metadata,
            ),
        )
        updated_nodes = repository.list_nodes(session, workspace_id)
        history = _history_for_synthesis(workspace_id, updated_nodes, synthesis_id)
    return _safe_dict(
        {
            "synthesis": created,
            "previous_synthesis": previous,
            "refreshed": True,
            "status": "CURRENT",
            "stale_reason": reason,
            "history": history,
        }
    )


def confirm_living_synthesis(session: Session, workspace_id: str, node_id: str) -> dict[str, Any]:
    node = repository.get_node(session, workspace_id, node_id)
    if not _is_living_synthesis(node):
        raise GraphError("LIVING_SYNTHESIS_REQUIRED", "the selected node is not a living synthesis", status_code=400)
    metadata = dict(_output_metadata(node))
    metadata["human_confirmed_at"] = datetime.now(UTC).isoformat()
    updated = repository.update_node(
        session,
        workspace_id,
        node_id,
        SystemNodeUpdate(
            version=int(node["version"]),
            assurance_status=AssuranceStatus.HUMAN_CONFIRMED,
            metadata=metadata,
        ),
    )
    return _safe_dict({"synthesis": updated, "status": "HUMAN_CONFIRMED"})


def set_living_synthesis_policy(
    session: Session,
    workspace_id: str,
    node_id: str,
    policy: AutoRefreshPolicy,
) -> dict[str, Any]:
    node = repository.get_node(session, workspace_id, node_id)
    if not _is_living_synthesis(node):
        raise GraphError("LIVING_SYNTHESIS_REQUIRED", "the selected node is not a living synthesis", status_code=400)
    metadata = dict(_output_metadata(node))
    metadata["auto_refresh_policy"] = policy.value
    updated = repository.update_node(
        session,
        workspace_id,
        node_id,
        SystemNodeUpdate(version=int(node["version"]), metadata=metadata),
    )
    return _safe_dict({"synthesis": updated, "policy": policy.value})
