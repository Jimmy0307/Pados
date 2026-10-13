# SPDX-License-Identifier: AGPL-3.0-only
"""Remote Operations HTTP surface (``/remote/v1``).

Intent, projection, upload and control endpoints only. There is no shell,
SQL, filesystem-browse, model-proxy or raw provider endpoint. Every request is
authenticated as a remote actor; when the deployment guard fails, every route
fails closed with 503 and the list of violations.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from pados import __version__
from pados.config import Settings
from pados.remote.auth import RemoteSession, authorize_project
from pados.remote.deployment import RemoteDeploymentCheck, listener_attestation, listener_violation
from pados.remote.intent import RemoteIntentError
from pados.remote.service import RemoteOperationsService
from pados.remote.presence import PresenceError, b64url_decode, user_handle_for
from pados.remote.staging import StagingError
from pados.remote.step_up import SESSION_HEADER, StepUpError

ACTOR_HEADER = "X-PADOS-Remote-Actor"
PROOF_HEADER = "X-PADOS-Remote-Proof"
PREFIX = "/remote/v1"
MAX_UPLOAD_BODY = 64 * 1024 * 1024
# Module level so FastAPI can resolve the (postponed) annotations.
Actor = Annotated[str | None, Header(alias=ACTOR_HEADER)]
Credential = Annotated[str | None, Header(alias=PROOF_HEADER)]
SessionHandle = Annotated[str | None, Header(alias=SESSION_HEADER)]


async def bounded_body(request: Request) -> bytes:
    """Read the request body but never buffer more than ``MAX_UPLOAD_BODY`` bytes.

    The Content-Length pre-check alone is bypassed by a chunked body without one. A single
    oversized chunk is refused before it is appended.
    """
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_UPLOAD_BODY - len(body):  # checked before copying the chunk
            raise HTTPException(status_code=413, detail="UPLOAD_TOO_LARGE")
        body.extend(chunk)
    return bytes(body)


def register_remote_routes(
    app: FastAPI,
    settings: Settings,
    *,
    service: RemoteOperationsService | None,
    deployment: RemoteDeploymentCheck,
    nodes: Callable[[], dict[str, Any]],
    connectivity: Callable[[], dict[str, Any]] | None = None,
    allowed_origins: tuple[str, ...] = (),
) -> None:
    @app.middleware("http")
    async def listener_guard(request: Request, call_next: Callable[[Request], Any]) -> Any:
        # F-05: the deployment check validates configuration; this validates the real socket.
        if request.url.path.startswith(PREFIX):
            violation = listener_violation(request.scope.get("server"), bind_host=deployment.bind_host,
                                           attestation=listener_attestation())
            if violation is not None:
                return JSONResponse(status_code=503, content={"detail": {"remote": "DISABLED",
                                                                          "violations": [violation]}})
        return await call_next(request)

    def connectivity_view() -> dict[str, Any]:
        if connectivity is None:
            return {"status": "CONNECTIVITY_NOT_CONFIGURED", "home_base": "RUNNING", "semantic_authority": "NONE"}
        return connectivity()

    def session_for(
        actor: str | None, presented: str | None, handle: str | None = None
    ) -> tuple[RemoteOperationsService, RemoteSession]:
        if not deployment.ok or service is None:
            raise HTTPException(status_code=503, detail={"remote": "DISABLED", **deployment.to_dict()})
        try:
            session = service.registry.authenticate(actor, presented)
            if handle is not None:
                # The actor credential is still required; the handle only binds context.
                if service.step_up is None:
                    raise StepUpError("APPROVAL_STEP_UP_NOT_CONFIGURED")
                bound = service.step_up.resolve_session(session.actor.actor_id, handle)
                session = RemoteSession(actor=session.actor, session_id=bound, server_bound=True)
            return service, session
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    async def dispatch(svc: RemoteOperationsService, session: RemoteSession, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await svc.submit(session, body)
        except RemoteIntentError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=getattr(exc, "code", str(exc))) from exc

    @app.post(f"{PREFIX}/intents")
    async def submit_intent(
        body: dict[str, Any], actor: Actor = None, credential: Credential = None, handle: SessionHandle = None
    ) -> dict[str, Any]:
        svc, session = session_for(actor, credential, handle)
        return await dispatch(svc, session, body)

    def step_up_call(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return call()
        except RemoteIntentError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=getattr(exc, "code", str(exc))) from exc
        except PresenceError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc

    @app.post(f"{PREFIX}/sessions")
    def open_session(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        return step_up_call(lambda: svc.open_session(session))

    @app.post(f"{PREFIX}/approvals/{{approval_ref}}/step-up")
    def issue_step_up(
        approval_ref: str, body: dict[str, Any], actor: Actor = None, credential: Credential = None,
        handle: SessionHandle = None,
    ) -> dict[str, Any]:
        svc, session = session_for(actor, credential, handle)
        if set(body) - {"action"}:
            raise HTTPException(status_code=422, detail="CLIENT_BINDING_FIELDS_FORBIDDEN")
        return step_up_call(lambda: svc.issue_step_up(session, approval_ref, str(body.get("action", ""))))

    @app.post(f"{PREFIX}/approvals/{{approval_ref}}/step-up/{{challenge_id}}/verify")
    def verify_step_up(
        approval_ref: str, challenge_id: str, body: dict[str, Any], actor: Actor = None,
        credential: Credential = None, handle: SessionHandle = None,
    ) -> dict[str, Any]:
        svc, session = session_for(actor, credential, handle)
        assertion = body.get("assertion")
        if set(body) - {"assertion"} or not isinstance(assertion, dict):
            raise HTTPException(status_code=422, detail="STEP_UP_ASSERTION_REQUIRED")
        return step_up_call(lambda: svc.verify_step_up(session, approval_ref, challenge_id, assertion))

    @app.post(f"{PREFIX}/presence/enrollment-options")
    def enrollment_options(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        step_up = svc.step_up
        if step_up is None or step_up.verifier is None:
            raise HTTPException(status_code=503, detail="APPROVAL_PRESENCE_NOT_CONFIGURED")
        challenge = step_up.enrollment_challenge(session.actor.actor_id)  # server-held, single-use
        return {"public_key": step_up.verifier.registration_options(
            challenge, actor_id=session.actor.actor_id, user_handle=user_handle_for(session.actor.actor_id)),
            "enrollment": "DESCRIPTOR_ONLY_OPERATOR_MUST_ADD_TO_REMOTE_CONFIG"}

    @app.post(f"{PREFIX}/presence/enrollment-descriptor")
    def enrollment_descriptor(body: dict[str, Any], actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        step_up = svc.step_up
        if step_up is None or step_up.verifier is None:
            raise HTTPException(status_code=503, detail="APPROVAL_PRESENCE_NOT_CONFIGURED")
        registration = body.get("registration")
        if set(body) - {"registration", "challenge", "label"} or not isinstance(registration, dict):
            raise HTTPException(status_code=422, detail="REGISTRATION_REQUIRED")
        try:
            presented = b64url_decode(body.get("challenge"), code="REGISTRATION_CHALLENGE_INVALID")
            challenge = step_up.take_enrollment_challenge(session.actor.actor_id, presented)
            descriptor = step_up.verifier.enrollment_descriptor(
                registration, challenge=challenge, label=str(body.get("label") or session.actor.actor_id))
        except StepUpError as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc
        except PresenceError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc
        return {"actor_id": session.actor.actor_id, "descriptor": descriptor}

    def control_route(action: str) -> Callable[..., Any]:
        async def handler(
            run_id: str, body: dict[str, Any], actor: Actor = None, credential: Credential = None,
            handle: SessionHandle = None,
        ) -> dict[str, Any]:
            svc, session = session_for(actor, credential, handle)
            return await dispatch(svc, session, {**body, "intent_type": action, "target_run_id": run_id})

        return handler

    for action in ("PAUSE", "RESUME", "CANCEL"):
        app.post(f"{PREFIX}/jobs/{{run_id}}/{action.lower()}")(control_route(action))

    def decision_route(action: str) -> Callable[..., Any]:
        async def handler(
            approval_ref: str, body: dict[str, Any], actor: Actor = None, credential: Credential = None,
            handle: SessionHandle = None,
        ) -> dict[str, Any]:
            svc, session = session_for(actor, credential, handle)
            return await dispatch(svc, session, {**body, "intent_type": action, "approval_ref": approval_ref})

        return handler

    app.post(f"{PREFIX}/approvals/{{approval_ref}}/approve")(decision_route("APPROVE"))
    app.post(f"{PREFIX}/approvals/{{approval_ref}}/reject")(decision_route("REJECT"))

    @app.get(f"{PREFIX}/attention")
    def attention(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        return svc.attention(session)

    @app.get(f"{PREFIX}/work-map")
    def work_map(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        return svc.work_map(session)

    @app.get(f"{PREFIX}/mission-control")
    def mission_control(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        return {**svc.attention(session), "nodes": nodes()}

    @app.get(f"{PREFIX}/jobs")
    def jobs(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        view = svc.attention(session)
        return {"jobs": view["running"] + view["waiting_approval"] + view["failed"] + view["completed"]}

    @app.get(f"{PREFIX}/jobs/{{run_id}}")
    def job_detail(run_id: str, actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        detail = svc.job_detail(session, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="TARGET_RUN_NOT_FOUND")
        return detail

    @app.get(f"{PREFIX}/jobs/{{run_id}}/graph")
    def job_graph(run_id: str, actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        graph = svc.run_graph(session, run_id)
        if graph is None:
            raise HTTPException(status_code=404, detail="TARGET_RUN_NOT_FOUND")
        return graph

    @app.get(f"{PREFIX}/approvals")
    def approvals(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        return {"pending": svc.attention(session)["waiting_approval"]}

    @app.post(f"{PREFIX}/uploads")
    async def upload(
        request: Request,
        project_id: Annotated[str, Query()],
        name: Annotated[str, Query()],
        classification: Annotated[str, Query()],
        actor: Actor = None,
        credential: Credential = None,
    ) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BODY:
            raise HTTPException(status_code=413, detail="UPLOAD_TOO_LARGE")
        try:
            authorize_project(session, project_id)
            staged = svc.staging.stage(
                project_id=project_id,
                actor_id=session.actor.actor_id,
                name=name,
                content=await bounded_body(request),
                classification=classification,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (StagingError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=getattr(exc, "code", str(exc))) from exc
        return staged.to_dict()

    @app.get(f"{PREFIX}/uploads/{{staged_id}}")
    def upload_status(staged_id: str, actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, session = session_for(actor, credential)
        record = svc.staging.get(staged_id)
        if record is None or record["project_id"] not in session.actor.projects:
            raise HTTPException(status_code=404, detail="STAGED_FILE_NOT_FOUND")
        return {**record, "integrity_verified": svc.staging.verify(staged_id)}

    @app.get(f"{PREFIX}/nodes")
    def node_status(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        session_for(actor, credential)
        return nodes()

    @app.get(f"{PREFIX}/home-base")
    def home_base(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        svc, _session = session_for(actor, credential)
        return {
            "version": __version__,
            "environment": settings.env,
            "network_model": deployment.network_model.value,
            "deployment_ok": deployment.ok,
            "malware_scanner_configured": svc.staging.scanner_configured,
            "intent_ledger_intact": svc.ledger.verify(),
            "remote_shell": "NOT_PROVIDED",
            "home_base": "RUNNING",
            "connectivity": connectivity_view(),
        }

    @app.get(f"{PREFIX}/connectivity")
    def connectivity_status(actor: Actor = None, credential: Credential = None) -> dict[str, Any]:
        session_for(actor, credential)
        return connectivity_view()

    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=[ACTOR_HEADER, PROOF_HEADER, SESSION_HEADER, "Content-Type"],
            max_age=300,
        )
