# SPDX-License-Identifier: AGPL-3.0-only
"""The startup readiness condition of the web dev stack (Vite dev server + ``pados serve``).

``full`` (the condition test drivers and launchers wait on):
  * the Vite dev server answers ``GET /__pados/dev-ready`` (web/vite-dev-readiness.ts) with 200 and
    ``{"ready": true, ...}``: every entry module and its static imports are transformed and the dependency optimizer
    has no scan or bundle in progress, so the first page load cannot trigger a "new dependencies optimized" reload;
    a 200 that is not that JSON (an older dev server's SPA fallback page) does not count;
  * the API answers ``GET /health/ready`` with 200 (``pados serve`` is up and its required lanes are reachable).
``html`` is the old condition (Playwright's ``webServer.url``): the dev server answers ``/`` with 200, which a cold
Vite does in a few hundred milliseconds, before any of the work above.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

MODES = ("full", "html")
DEV_READY_PATH = "/__pados/dev-ready"
API_READY_PATH = "/health/ready"


def _get(url: str, timeout: float) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:  # noqa: S310 - loopback URLs built by the caller
            return reply.status, reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, OSError):
        return 0, ""


def check_html(web_url: str, api_url: str, timeout: float = 10.0) -> dict[str, Any] | None:
    status, _ = _get(f"{web_url}/", timeout)
    return {"index": status} if status == 200 else None


def check_full(web_url: str, api_url: str, timeout: float = 60.0) -> dict[str, Any] | None:
    status, body = _get(f"{web_url}{DEV_READY_PATH}", timeout)
    try:
        report = json.loads(body) if status == 200 else {}
    except ValueError:
        report = {}
    if not (isinstance(report, dict) and report.get("ready") is True):
        return None
    api_status, _ = _get(f"{api_url}{API_READY_PATH}", timeout)
    if api_status != 200:
        return None
    return {"web": report, "api_ready": api_status}


CHECKS: dict[str, Callable[[str, str], dict[str, Any] | None]] = {"full": check_full, "html": check_html}


def wait_ready(web_url: str, api_url: str, *, mode: str = "full", deadline_s: float = 120.0,
               alive: Callable[[], bool] = lambda: True) -> dict[str, Any]:
    """Poll until the condition holds; refuse a dev server that exits or is not ready by the deadline."""
    if mode not in CHECKS:
        raise ValueError(f"unknown readiness mode {mode!r}")
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if not alive():
            raise RuntimeError("the Vite dev server exited before it was ready")
        state = CHECKS[mode](web_url, api_url)
        if state is not None:
            return state
        time.sleep(0.2)
    raise RuntimeError(f"the web dev stack was not ready within {deadline_s:.0f}s ({mode})")
