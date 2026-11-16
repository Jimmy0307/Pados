# SPDX-License-Identifier: AGPL-3.0-only
"""The web dev stack's startup readiness condition (scripts/dogfood/web_readiness.py) against local fake servers."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.dogfood import web_readiness


@contextmanager
def fake(routes: dict[str, tuple[int, str]]) -> Iterator[str]:
    """A loopback server answering each path with (status, body); anything else is the SPA fallback page."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            status, body = routes.get(self.path, (200, "<!doctype html><div id=root></div>"))
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


READY = json.dumps({"ready": True, "entryModules": ["/src/main.tsx"], "optimizedDeps": ["react"]})


def test_full_condition_needs_the_dev_ready_report_and_the_api_readiness() -> None:
    with fake({web_readiness.DEV_READY_PATH: (200, READY)}) as web, \
            fake({web_readiness.API_READY_PATH: (200, "{}")}) as api:
        state = web_readiness.check_full(web, api)
    assert state is not None
    assert state["web"]["optimizedDeps"] == ["react"]
    assert state["api_ready"] == 200


def test_a_spa_fallback_page_is_not_readiness() -> None:
    # An older dev server without the endpoint answers every unknown path with index.html and 200.
    with fake({}) as web, fake({web_readiness.API_READY_PATH: (200, "{}")}) as api:
        assert web_readiness.check_html(web, api) == {"index": 200}
        assert web_readiness.check_full(web, api) is None


def test_an_api_that_is_not_ready_is_not_readiness() -> None:
    with fake({web_readiness.DEV_READY_PATH: (200, READY)}) as web, \
            fake({web_readiness.API_READY_PATH: (503, "{}")}) as api:
        assert web_readiness.check_full(web, api) is None


def test_a_not_ready_report_or_a_dead_dev_server_fails_the_wait() -> None:
    with fake({web_readiness.DEV_READY_PATH: (503, json.dumps({"ready": False}))}) as web, \
            fake({web_readiness.API_READY_PATH: (200, "{}")}) as api:
        assert web_readiness.check_full(web, api) is None
        with pytest.raises(RuntimeError, match="not ready within"):
            web_readiness.wait_ready(web, api, deadline_s=0.5)
        with pytest.raises(RuntimeError, match="exited"):
            web_readiness.wait_ready(web, api, alive=lambda: False)
    with pytest.raises(ValueError, match="unknown readiness mode"):
        web_readiness.wait_ready("http://127.0.0.1:9", "http://127.0.0.1:9", mode="nope")
