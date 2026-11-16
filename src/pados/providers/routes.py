# SPDX-License-Identifier: AGPL-3.0-only
"""``/control/providers``: the Control Center's AI provider list, setup and diagnostics (behind ``control_auth``).

Reads show configured providers with two separate levels (reachable vs authorized and measured); diagnostics probe
health, discovery and model identity through the existing paths; writes change only the owner's provider setup file
(non-secret settings and the enabled flag), which can only narrow. None of these routes authorizes a model, routes
work or accepts a result, and no response ever carries a key value (only the NAME of its environment variable).

SESSION SCOPES (as enforced by ``src/pados/identity/scopes.py``; least privilege):
  GET  /control/providers, GET /control/providers/{provider_id}  -> allowed for sessions (read only)
  POST /control/providers/{provider_id}/diagnostics              -> allowed for sessions (observes; writes only
                                                                     content-free attempt receipts)
  POST /control/providers/{provider_id}/disable                  -> allowed for sessions (only narrows)
  PUT  /control/providers/{provider_id}, POST .../enable         -> control token only (an endpoint or contract edit,
                                                                     or lifting a restriction the operator set)
"""
# No ``from __future__ import annotations``: FastAPI must see the route annotations built from the local
# ``control_auth`` dependency at definition time.
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from pados.config import Settings
from pados.enterprise.classification import DataClassification
from pados.logging_config import get_logger
from pados.providers.diagnostics import ProviderDiagnostics
from pados.providers.setup import (
    ProviderKind,
    Requirement,
    ResourceLimits,
    SetupError,
    parse_settings,
    set_enabled,
    upsert,
)

_LOG = get_logger("pados.providers.routes")


class ProviderUpdate(BaseModel):
    """The whole non-secret setting set of one provider (replace semantics), guarded by the setup revision."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    kind: ProviderKind
    display_name: str = Field(default="", max_length=80)
    endpoint: str | None = Field(default=None, max_length=200)
    contract_path: str | None = Field(default=None, max_length=400)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    context_window: int | None = Field(default=None, ge=256, le=2_000_000)
    capability_requirement: Requirement | None = None
    project_ids: list[str] = Field(default_factory=list, max_length=64)
    max_classification: DataClassification = DataClassification.PUBLIC
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    auth_env: str | None = Field(default=None, max_length=64)


class RevisionGuard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)


class DiagnosticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe_chat: bool = False
    model: str | None = Field(default=None, min_length=1, max_length=160)


def register_provider_routes(app: FastAPI, settings: Settings, *, control_auth: Any,
                             diagnostics: ProviderDiagnostics | None = None) -> ProviderDiagnostics:
    ControlAuth = Annotated[None, control_auth]
    service = diagnostics or ProviderDiagnostics(settings)

    def run(handler: Callable[[], Any]) -> Any:
        try:
            return handler()
        except SetupError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    def one(provider_id: str) -> dict[str, Any]:
        overview = service.overview()
        found = next((item for item in overview["providers"] if item["provider_id"] == provider_id), None)
        if found is None:
            raise SetupError("PROVIDER_NOT_CONFIGURED", 404)
        return dict(found)

    @app.get("/control/providers")
    def control_providers(_: ControlAuth) -> object:
        return run(lambda: {"status": "ok", **service.overview()})

    @app.get("/control/providers/{provider_id}")
    def control_provider(provider_id: str, _: ControlAuth) -> object:
        return run(lambda: {"status": "ok", "provider": one(provider_id)})

    @app.post("/control/providers/{provider_id}/diagnostics")
    def control_provider_diagnostics(provider_id: str, payload: DiagnosticsRequest, _: ControlAuth) -> object:
        def _diagnose() -> dict[str, Any]:
            view = service.diagnose(provider_id, probe_chat=payload.probe_chat, model=payload.model)
            _LOG.info("providers.diagnostics", provider_id=provider_id, state=view["reachability"]["state"])
            return {"status": "ok", "provider": view}

        return run(_diagnose)

    @app.put("/control/providers/{provider_id}")
    def control_provider_update(provider_id: str, payload: ProviderUpdate, _: ControlAuth) -> object:
        def _update() -> dict[str, Any]:
            document = payload.model_dump(mode="json", exclude={"expected_revision"})
            current = service.store.load().get(provider_id)
            settings_item = parse_settings({**document, "provider_id": provider_id,
                                            "enabled": current.enabled if current is not None else False})
            updated = service.store.update(payload.expected_revision, lambda setup: upsert(setup, settings_item))
            _LOG.info("providers.updated", provider_id=provider_id, revision=updated.revision)
            return {"status": "ok", "setup_revision": updated.revision, "provider": one(provider_id)}

        return run(_update)

    def toggle(provider_id: str, payload: RevisionGuard, enabled: bool) -> dict[str, Any]:
        def change(setup: Any) -> Any:
            if setup.get(provider_id) is None:
                # a provider detected from settings becomes an explicit setup entry the first time it is toggled
                detected = next((item for item, source in service.providers(setup)
                                 if item.provider_id == provider_id and source == "settings"), None)
                if detected is None:
                    raise SetupError("PROVIDER_NOT_CONFIGURED", 404)
                setup = upsert(setup, detected)
            return set_enabled(setup, provider_id, enabled)

        updated = service.store.update(payload.expected_revision, change)
        _LOG.info("providers.toggled", provider_id=provider_id, enabled=enabled, revision=updated.revision)
        return {"status": "ok", "setup_revision": updated.revision, "provider": one(provider_id)}

    @app.post("/control/providers/{provider_id}/enable")
    def control_provider_enable(provider_id: str, payload: RevisionGuard, _: ControlAuth) -> object:
        return run(lambda: toggle(provider_id, payload, True))

    @app.post("/control/providers/{provider_id}/disable")
    def control_provider_disable(provider_id: str, payload: RevisionGuard, _: ControlAuth) -> object:
        return run(lambda: toggle(provider_id, payload, False))

    return service
