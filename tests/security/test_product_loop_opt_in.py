# SPDX-License-Identifier: AGPL-3.0-only
"""A local proof database must not activate the experimental Product Loop routes by itself."""
from __future__ import annotations

from fastapi import FastAPI

from pados.config import Settings
from pados.product_loop.routes import register_product_loop_routes


def _paths(app: FastAPI) -> set[str]:
    return {route.path for route in app.routes}


def test_configured_proof_database_does_not_mount_product_loop_without_opt_in(tmp_path) -> None:
    app = FastAPI()
    settings = Settings(
        PADOS_PRODUCT_DATABASE_NAME="pados_dogfood_proof",
        PADOS_PRODUCT_WORKSPACE_ROOT=str(tmp_path / "workspace"),
        PADOS_PRODUCT_OBJECT_ROOT=str(tmp_path / "objects"),
    )

    register_product_loop_routes(app, settings, service=object())  # type: ignore[arg-type]

    assert "/product-loop/runs" not in _paths(app)


def test_product_loop_routes_require_explicit_enablement(tmp_path) -> None:
    app = FastAPI()
    settings = Settings(
        PADOS_PRODUCT_DATABASE_NAME="pados_dogfood_proof",
        PADOS_PRODUCT_WORKSPACE_ROOT=str(tmp_path / "workspace"),
        PADOS_PRODUCT_OBJECT_ROOT=str(tmp_path / "objects"),
        PADOS_PRODUCT_LOOP_ENABLED=True,
    )

    register_product_loop_routes(app, settings, service=object())  # type: ignore[arg-type]

    assert "/product-loop/runs" in _paths(app)
