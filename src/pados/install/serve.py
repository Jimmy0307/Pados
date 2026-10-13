# SPDX-License-Identifier: AGPL-3.0-only
"""Serve the existing PADOS API (``pados.api.app``) plus the prebuilt web UI, when the runtime ships it.

The UI is static files only (``<app root>/web-dist``, built with base ``/ui/``); every request it makes goes to
the same Core API on the same loopback origin. Binding is checked by Core's ``assert_localhost_binding``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pados.install.database import app_root

UI_MOUNT = "/ui"


def ui_directory(root: Path | None = None) -> Path | None:
    candidate = (root or app_root()) / "web-dist"
    return candidate if (candidate / "index.html").is_file() else None


def create_app() -> Any:
    from fastapi.staticfiles import StaticFiles

    from pados.api.app import create_app as create_core_app

    application = create_core_app()
    ui = ui_directory()
    if ui is not None:
        application.mount(UI_MOUNT, StaticFiles(directory=ui, html=True), name="pados-ui")
    return application


def run(instance_id: str) -> None:
    """Run in the foreground; ``instance_id`` is on the command line so the launcher can verify identity."""
    import uvicorn

    from pados.config import get_settings
    from pados.control.security import assert_localhost_binding

    settings = get_settings()
    assert_localhost_binding(settings)
    del instance_id  # identity marker only (visible in the process command line)
    uvicorn.run(create_app(), host=settings.api_host, port=settings.api_port, log_level="info")
