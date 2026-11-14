# SPDX-License-Identifier: AGPL-3.0-only
"""The live dogfood E2E waits for ``pados serve`` to answer /api/health.

/api/health opens fresh engines for two databases and reads metrics, so one answer takes about 2-3.5 seconds on the
validation hosts (1.8 s on the VMware VM, 2.0-3.5 s on Windows). A 2-second per-probe timeout therefore abandoned
answers that were on their way and failed the whole run on a healthy server (VMware run -claude-20261110-r2). The wait
must allow a slow answer while still refusing a server that exits or never becomes healthy.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.dogfood.local_dogfood_e2e import wait_until_healthy


class _Server:
    """Stands in for the ``pados serve`` process: only ``poll`` and ``returncode`` are read."""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def _http(status: int, delay: float) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_: object) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/api/health"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def slow_healthy() -> Iterator[str]:
    yield from _http(200, delay=2.5)


@pytest.fixture
def never_healthy() -> Iterator[str]:
    yield from _http(503, delay=0.0)


def test_a_healthy_server_that_answers_slower_than_two_seconds_is_accepted(slow_healthy: str) -> None:
    started = time.monotonic()
    wait_until_healthy(slow_healthy, _Server(), "serve_test", deadline_s=20.0)  # type: ignore[arg-type]
    assert time.monotonic() - started < 10.0


def test_a_server_that_never_becomes_healthy_is_refused_at_the_deadline(never_healthy: str) -> None:
    with pytest.raises(RuntimeError, match="^pados serve did not become healthy$"):
        wait_until_healthy(never_healthy, _Server(), "serve_test", deadline_s=2.0)  # type: ignore[arg-type]


class _DiesAfterFirstProbe(_Server):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def poll(self) -> int | None:
        self.calls += 1
        if self.calls > 1:
            self.returncode = 7
        return self.returncode


def test_a_server_that_dies_while_waiting_is_refused_without_waiting_for_the_deadline(never_healthy: str) -> None:
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"^pados serve exited early \(7\); see serve_test\.log$"):
        wait_until_healthy(never_healthy, _DiesAfterFirstProbe(), "serve_test", deadline_s=30.0)  # type: ignore[arg-type]
    assert time.monotonic() - started < 5.0


def test_a_server_that_exited_is_refused_at_once(never_healthy: str) -> None:
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"^pados serve exited early \(3\); see serve_test\.log$"):
        wait_until_healthy(never_healthy, _Server(returncode=3), "serve_test", deadline_s=30.0)  # type: ignore[arg-type]
    assert time.monotonic() - started < 5.0


# --- the server a run starts must be the one that answers, and must not outlive a failed start ----------------------
# (VMware run -claude-20261111 at eb112a3: a server left by the previous failed run still held the port and answered
# the health probe, so the run talked to a stale server with another token and a removed database.)


def test_a_port_that_another_server_holds_is_refused() -> None:
    import socket

    from scripts.dogfood.local_dogfood_e2e import refuse_busy_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match=rf"^API port {port} is already in use"):
            refuse_busy_port(port)


def test_a_free_port_is_accepted() -> None:
    import socket

    from scripts.dogfood.local_dogfood_e2e import refuse_busy_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    refuse_busy_port(port)  # nothing listens there now


def test_a_server_that_fails_to_become_healthy_is_stopped_not_left_running(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from scripts.dogfood import local_dogfood_e2e as e2e

    started: list[_Server] = []
    stopped: list[_Server] = []

    def fake_popen(*_args: object, **_kwargs: object) -> _Server:
        started.append(_Server())
        return started[-1]

    def not_healthy(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("pados serve did not become healthy")

    monkeypatch.setattr(e2e, "refuse_busy_port", lambda _port: None)
    monkeypatch.setattr(e2e.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(e2e, "wait_until_healthy", not_healthy)
    monkeypatch.setattr(e2e, "stop_server", stopped.append)
    with pytest.raises(RuntimeError, match="^pados serve did not become healthy$"):
        e2e.start_server(SimpleNamespace(out=tmp_path), {}, 1, "serve_test")  # type: ignore[arg-type]
    assert started and stopped == started
