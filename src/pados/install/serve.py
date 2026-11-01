# SPDX-License-Identifier: AGPL-3.0-only
"""Serve the existing PADOS API (``pados.api.app``) plus the prebuilt web UI, when the runtime ships it.

The UI is static files only (``<app root>/web-dist``, built with base ``/ui/``); every request it makes goes to
the same Core API on the same loopback origin. Binding is checked by Core's ``assert_localhost_binding``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pados.install.database import app_root

UI_MOUNT = "/ui"
# Written by the launcher (`pados down`) with the instance id; the matching server shuts down gracefully (RC2 UX-2).
STOP_FILE = Path("state") / "api.stop"


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


def stop_requested(path: Path, instance_id: str) -> bool:
    """True only when the stop file names exactly this instance; another instance's request is ignored."""
    try:
        return path.read_text(encoding="utf-8").strip() == instance_id
    except OSError:
        return False


def watch_stop_file(server: Any, path: Path, instance_id: str, *, interval: float = 0.5) -> None:
    """Poll for this instance's stop request, then ask uvicorn to drain and exit (no forced termination)."""
    while not getattr(server, "should_exit", False):
        if stop_requested(path, instance_id):
            path.unlink(missing_ok=True)
            server.should_exit = True
            return
        time.sleep(interval)


def run(instance_id: str, home: Path | None = None) -> None:
    """Run in the foreground; ``instance_id`` is on the command line so the launcher can verify identity."""
    import threading

    import uvicorn

    from pados.config import get_settings
    from pados.control.security import assert_localhost_binding

    settings = get_settings()
    assert_localhost_binding(settings)
    server = uvicorn.Server(uvicorn.Config(create_app(), host=settings.api_host, port=settings.api_port,
                                           log_level="info"))
    if home is not None:
        (home / STOP_FILE).unlink(missing_ok=True)  # a request left by an interrupted `pados down` is not for us
        threading.Thread(target=watch_stop_file, args=(server, home / STOP_FILE, instance_id), daemon=True,
                         name="pados-stop-request").start()
    server.run()
