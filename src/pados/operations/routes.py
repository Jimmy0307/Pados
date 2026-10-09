# SPDX-License-Identifier: AGPL-3.0-only
"""Local read-only HTTP projection for Mission Control and the action inbox."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.orm import Session

from pados.config import Settings
from pados.corpus import operator as corpus_operator
from pados.corpus.operator_config import operational_settings
from pados.db import get_engine
from pados.operations import service


def register_operations_routes(app: FastAPI, settings: Settings) -> None:
    def guard(request: Request) -> None:
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail="local operator surfaces only")
        if settings.api_host not in {"127.0.0.1", "localhost"}:
            raise HTTPException(status_code=503, detail="operator surfaces require localhost binding")

    def read(handler: Callable[[Session, Settings], dict[str, Any]]) -> dict[str, Any]:
        bound = operational_settings(corpus_operator.configuration(), base=settings)
        engine = get_engine(bound)
        try:
            with Session(engine) as session:
                return handler(session, bound)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"PADOS authority read unavailable ({type(exc).__name__})") from exc
        finally:
            engine.dispose()

    @app.get("/api/mission-control")
    def mission_control(request: Request) -> dict[str, Any]:
        guard(request)
        return read(service.mission_control)

    @app.get("/api/decision-inbox")
    def decision_inbox(request: Request) -> dict[str, Any]:
        guard(request)
        return read(lambda session, _settings: service.decision_inbox(session))
