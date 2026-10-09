# SPDX-License-Identifier: AGPL-3.0-only
"""Remote Operations wiring.

``build_remote_components`` binds the remote service to existing authority.
``create_remote_app`` is the overlay-facing process: it serves ONLY
``/remote/v1`` and the static remote console, so the rest of the PADOS API
(control plane, corpus, DB-backed operator surfaces, model endpoints) is never
reachable from the overlay. The main API also mounts the same routes when
``PADOS_REMOTE_CONFIG`` is set.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from pados import __version__
from pados.config import Settings, get_settings, lane_settings
from pados.enterprise.classification import DataClassification, parse_classification
from pados.product_loop.state import P6_19BStateGateway
from pados.remote.config import RemoteConfig
from pados.remote.context import RemoteContextReconstructor
from pados.remote.deployment import RemoteDeploymentCheck, check_remote_deployment
from pados.remote.jobs import RemoteJobController
from pados.remote.ledger import IntentLedger
from pados.remote.live import LiveRemotePorts
from pados.remote.presence import WebAuthnPresenceVerifier
from pados.remote.routes import register_remote_routes
from pados.remote.service import RemoteOperationsService
from pados.remote.staging import StagingArea
from pados.remote.step_up import PostgresStepUpStore, StepUpService
from pados.state_proof import ObjectStore, StateProofService
from pados.state_proof.service import build_proof_engine


class _NoControlBackend:
    """Product-loop runs are bounded and synchronous: pause/cancel are unsupported."""

    supports_pause = False
    supports_cancel = False

    def __init__(self, ports: LiveRemotePorts) -> None:
        self._ports = ports

    def exists(self, run_id: str) -> bool:
        return self._ports.run_summary(run_id) is not None

    def is_terminal(self, run_id: str) -> bool:
        row = self._ports.run_summary(run_id)
        return bool(row and row.get("finished_at"))

    def pause(self, run_id: str) -> bool:
        return False

    def resume(self, run_id: str) -> bool:
        return False

    def request_cancel(self, run_id: str) -> bool:
        return False


def _nodes(settings: Settings) -> dict[str, Any]:
    from pados.compute_node.visibility import compute_node_visibility

    view = compute_node_visibility(settings.compute_nodes_config)
    # Remote users see node health only, never endpoints or model servers.
    for node in view.get("nodes", []):
        node.pop("last_execution", None)
    return {**view, "model_endpoints_exposed": False}


def build_remote_components(
    settings: Settings, config: RemoteConfig
) -> tuple[RemoteOperationsService | None, RemoteDeploymentCheck]:
    deployment = check_remote_deployment(settings, remote_bind_host=config.bind_host)
    if not deployment.ok:
        return None, deployment
    object_root = Path(settings.product_object_root).expanduser().resolve()
    proof_engine = build_proof_engine(lane_settings(settings, settings.product_database_name).database_url)
    operational_engine = build_proof_engine(
        lane_settings(settings, settings.operational_database_name).database_url
    )
    gateway = P6_19BStateGateway(StateProofService(proof_engine, ObjectStore(object_root)), operational_engine)
    ports = LiveRemotePorts(
        gateway=gateway,
        operational_engine=operational_engine,
        presentation_dir=config.ledger_path.parent / "decision-presentations",
    )

    def semantic(project_id: str) -> dict[str, Any] | None:
        if config.workspace_for(project_id) is None:
            return None
        resolved = dict(gateway.resolve_project_context(project_id))
        resolved.setdefault("scope_version", gateway.semantic_context(project_id)["scope_version"])
        return resolved

    def ceiling(project_id: str) -> DataClassification | None:
        with Session(operational_engine) as session:
            value = session.execute(
                text("SELECT classification FROM projects WHERE id::text = :id"), {"id": project_id}
            ).scalar_one_or_none()
        if value is None:
            return None
        try:
            return parse_classification(value)
        except ValueError:
            return None

    service = RemoteOperationsService(
        registry=config.registry,
        reconstructor=RemoteContextReconstructor(
            semantic_source=semantic, workspace_resolver=config.workspace_for
        ),
        staging=StagingArea(config.staging_root, scanner=None, project_ceiling=ceiling),
        jobs=RemoteJobController(_NoControlBackend(ports)),
        ledger=IntentLedger(config.ledger_path),
        ports=ports,
        step_up=StepUpService(PostgresStepUpStore(operational_engine), presence_verifier(config)),
    )
    return service, deployment


def presence_verifier(config: RemoteConfig) -> WebAuthnPresenceVerifier | None:
    """The production WebAuthn verifier; absent configuration fails every decision closed."""
    if config.presence_rp_id is None:
        return None
    return WebAuthnPresenceVerifier(rp_id=config.presence_rp_id, origins=config.presence_origins)


def mount_remote(app: FastAPI, settings: Settings) -> bool:
    """Mount /remote/v1 when configured. Unset config leaves Core unchanged."""
    if not settings.remote_config:
        return False
    config = RemoteConfig.load(settings.remote_config)
    service, deployment = build_remote_components(settings, config)
    connectivity = None
    if settings.connectivity_config:
        from pados.connectivity.runtime import connectivity_reader

        # Shared background runtime when active, else the per-route monitor.
        connectivity = connectivity_reader(settings.connectivity_config)
    register_remote_routes(
        app, settings, service=service, deployment=deployment, nodes=lambda: _nodes(settings),
        connectivity=connectivity, allowed_origins=config.allowed_origins,
    )
    return True


def create_remote_app() -> FastAPI:
    settings = get_settings()
    from pados.connectivity.runtime import connectivity_lifespan

    app = FastAPI(title="PADOS Remote Operations", version=__version__, docs_url=None, redoc_url=None,
                  lifespan=connectivity_lifespan(settings.connectivity_config))
    if not mount_remote(app, settings):
        raise RuntimeError("PADOS_REMOTE_CONFIG_REQUIRED")
    return app
