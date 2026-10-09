# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request

from pados.config import Settings
from pados.control.security import assert_request_safe, authorize_token
from pados.db import session_scope
from pados.graph import service
from pados.graph.schemas import (
    CapabilityReuseRequest,
    CognitiveRequest,
    EdgeCreate,
    GroupCreate,
    IntentRequest,
    NodeCreate,
    NodeUpdate,
    SelectionRequest,
    SynthesisPolicyRequest,
    SynthesisRefreshRequest,
    UrlIngestRequest,
    WorkspaceCreate,
)


def register_graph_routes(app: FastAPI, settings: Settings) -> None:
    graph_settings = settings.model_copy(update={"db_name": settings.operational_database_name})

    def guard(request: Request) -> None:
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail={"code": "LOCAL_ONLY", "message": "graph API is local-only"})
        if settings.api_host not in {"127.0.0.1", "localhost"}:
            raise HTTPException(status_code=503, detail={"code": "LOCAL_BINDING_REQUIRED", "message": "graph API requires localhost binding"})

    def fail(exc: service.GraphError) -> HTTPException:
        return HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "write_occurred": exc.write_occurred},
        )

    def has_control_authority(request: Request) -> bool:
        try:
            authorize_token(settings, request.headers.get("X-PADOS-Control-Token"))
        except PermissionError:
            return False
        return True

    def require_control_authority(request: Request) -> None:
        if not has_control_authority(request):
            raise HTTPException(
                status_code=403,
                detail={"code": "CONTROL_AUTH_REQUIRED", "message": "Run requires a valid PADOS control token."},
            )

    @app.get("/api/graph/workspaces")
    def graph_workspaces(request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", "workspaces": service.list_workspaces(session)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces", status_code=201)
    def graph_create_workspace(payload: WorkspaceCreate, request: Request) -> dict[str, Any]:
        guard(request)
        assert_request_safe(payload.model_dump(mode="json"), max_bytes=settings.control_request_max_bytes)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", "workspace": service.create_workspace(session, payload)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.get("/api/graph/workspaces/{workspace_id}")
    def graph_workspace(workspace_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", **service.workspace_bundle(session, workspace_id)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/nodes", status_code=201)
    def graph_create_node(workspace_id: str, payload: NodeCreate, request: Request) -> dict[str, Any]:
        guard(request)
        assert_request_safe(payload.model_dump(mode="json"), max_bytes=settings.control_request_max_bytes)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", "node": service.create_node(session, workspace_id, payload)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/ingest/url", status_code=201)
    def graph_ingest_url(
        workspace_id: str,
        payload: UrlIngestRequest,
        request: Request,
    ) -> dict[str, Any]:
        guard(request)
        assert_request_safe(payload.model_dump(mode="json"), max_bytes=settings.control_request_max_bytes)
        try:
            with session_scope(graph_settings) as session:
                service.ensure_workspace(session, workspace_id)
            fetched = service.retrieve_url_resource(payload)
            with session_scope(graph_settings) as session:
                return {
                    "status": "ok",
                    **service.persist_url_resource(session, workspace_id, payload, fetched),
                }
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/intent", status_code=201)
    def graph_intent(workspace_id: str, payload: IntentRequest, request: Request) -> dict[str, Any]:
        guard(request)
        assert_request_safe(payload.model_dump(mode="json"), max_bytes=settings.control_request_max_bytes)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", **service.interpret_intent(session, workspace_id, payload)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.patch("/api/graph/workspaces/{workspace_id}/nodes/{node_id}")
    def graph_update_node(workspace_id: str, node_id: str, payload: NodeUpdate, request: Request) -> dict[str, Any]:
        guard(request)
        assert_request_safe(payload.model_dump(mode="json"), max_bytes=settings.control_request_max_bytes)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", "node": service.update_node(session, workspace_id, node_id, payload)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/nodes/{node_id}/artifact", status_code=201)
    def graph_output_artifact(workspace_id: str, node_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", **service.create_output_artifact(session, workspace_id, node_id)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/syntheses/{node_id}/refresh", status_code=201)
    async def graph_refresh_synthesis(
        workspace_id: str,
        node_id: str,
        payload: SynthesisRefreshRequest,
        request: Request,
    ) -> dict[str, Any]:
        guard(request)
        assert_request_safe(payload.model_dump(mode="json"), max_bytes=settings.control_request_max_bytes)
        try:
            return {
                "status": "ok",
                **await service.refresh_living_synthesis(
                    graph_settings,
                    workspace_id,
                    node_id,
                    model_selection=payload.model_selection,
                ),
            }
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/syntheses/{node_id}/confirm", status_code=200)
    def graph_confirm_synthesis(workspace_id: str, node_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", **service.confirm_living_synthesis(session, workspace_id, node_id)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.patch("/api/graph/workspaces/{workspace_id}/syntheses/{node_id}/policy")
    def graph_synthesis_policy(
        workspace_id: str,
        node_id: str,
        payload: SynthesisPolicyRequest,
        request: Request,
    ) -> dict[str, Any]:
        guard(request)
        assert_request_safe(payload.model_dump(mode="json"), max_bytes=settings.control_request_max_bytes)
        try:
            with session_scope(graph_settings) as session:
                return {
                    "status": "ok",
                    **service.set_living_synthesis_policy(session, workspace_id, node_id, payload.policy),
                }
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.delete("/api/graph/workspaces/{workspace_id}/nodes/{node_id}", status_code=204)
    def graph_delete_node(workspace_id: str, node_id: str, request: Request) -> None:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                service.delete_node(session, workspace_id, node_id)
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/edges", status_code=201)
    def graph_create_edge(workspace_id: str, payload: EdgeCreate, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", "edge": service.create_edge(session, workspace_id, payload)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.delete("/api/graph/workspaces/{workspace_id}/edges/{edge_id}", status_code=204)
    def graph_delete_edge(workspace_id: str, edge_id: str, request: Request) -> None:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                service.delete_edge(session, workspace_id, edge_id)
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/groups", status_code=201)
    def graph_create_group(workspace_id: str, payload: GroupCreate, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", "group": service.create_group(session, workspace_id, payload)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.get("/api/graph/workspaces/{workspace_id}/search")
    def graph_search(
        workspace_id: str,
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", "nodes": service.search(session, workspace_id, q, limit)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/selection/actions")
    def graph_selection_actions(workspace_id: str, payload: SelectionRequest, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                actions = service.selection_actions(
                    session,
                    graph_settings,
                    workspace_id,
                    payload.node_ids,
                    control_authorized=has_control_authority(request),
                )
                return {"status": "ok", "actions": actions}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/selection/plan")
    def graph_plan(workspace_id: str, payload: SelectionRequest, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {
                    "status": "ok",
                    **service.compile_plan(
                        session,
                        workspace_id,
                        payload.node_ids,
                        control_authorized=has_control_authority(request),
                    ),
                }
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/selection/search")
    def graph_search_sources(workspace_id: str, payload: SelectionRequest, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", **service.search_sources(session, workspace_id, payload.node_ids)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/selection/run")
    def graph_run(workspace_id: str, payload: SelectionRequest, request: Request) -> dict[str, Any]:
        guard(request)
        require_control_authority(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", **service.run_selection(session, workspace_id, payload.node_ids)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/selection/cognitive", status_code=201)
    async def graph_cognitive(workspace_id: str, payload: CognitiveRequest, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            if payload.model_selection is not None:
                service.resolve_model_selection(graph_settings, payload.model_selection)
            with session_scope(graph_settings) as session:
                item = service.queue_cognitive_operation(
                    session,
                    workspace_id,
                    payload.operation,
                    payload.node_ids,
                    model_selection=payload.model_selection,
                )
            return {"status": "ok", **await service.process_cognitive_item(graph_settings, str(item["id"]))}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.post("/api/graph/workspaces/{workspace_id}/ideas/{node_id}/incubate", status_code=201)
    def graph_incubate(workspace_id: str, node_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", **service.incubate_idea(session, workspace_id, node_id)}
        except (LookupError, service.GraphError) as exc:
            graph_exc = exc if isinstance(exc, service.GraphError) else service.GraphError("NODE_NOT_FOUND", "node not found", status_code=404)
            raise fail(graph_exc) from exc

    @app.post("/api/graph/incubation/{item_id}/process")
    async def graph_process_incubation(item_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            return {"status": "ok", **await service.process_cognitive_item(graph_settings, item_id)}
        except service.GraphError as exc:
            raise fail(exc) from exc

    @app.get("/api/graph/capabilities")
    def graph_capabilities(request: Request) -> dict[str, Any]:
        guard(request)
        return {"status": "ok", "capabilities": service.capability_views()}

    @app.get("/api/graph/ai-runtime")
    def graph_ai_runtime(
        request: Request,
        discover: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        guard(request)
        return {
            "status": "ok",
            **service.ai_runtime_settings(graph_settings, discover=discover),
        }

    @app.post("/api/graph/workspaces/{workspace_id}/capabilities/{capability_id}/reuse", status_code=201)
    def graph_reuse_capability(
        workspace_id: str,
        capability_id: str,
        payload: CapabilityReuseRequest,
        request: Request,
    ) -> dict[str, Any]:
        guard(request)
        try:
            with session_scope(graph_settings) as session:
                return {"status": "ok", "node": service.reuse_capability(session, workspace_id, capability_id, payload)}
        except service.GraphError as exc:
            raise fail(exc) from exc
