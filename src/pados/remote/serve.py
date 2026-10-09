# SPDX-License-Identifier: AGPL-3.0-only
"""Guarded launcher for the overlay-facing Remote Operations process.

``python -m pados.remote.serve [--port N]`` is the only supported way to serve
``/remote/v1`` on a non-loopback address. It loads ``PADOS_REMOTE_CONFIG``, runs
the deployment guard, creates the one listening socket itself bound to exactly
``bind_host`` (a literal loopback or private-overlay IP, never a wildcard),
verifies ``getsockname()``, records the listener attestation and hands only that
socket to a single-worker uvicorn server. A plain ``uvicorn`` launch has no
attestation, so its remote routes answer only on loopback.
"""
from __future__ import annotations

import argparse
import ipaddress
import socket
import sys
from collections.abc import Callable, Sequence
from typing import Any

from pados.remote.deployment import attest_listener, check_remote_deployment, classify_bind_address

DEFAULT_PORT = 8089


class LaunchRefused(PermissionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def bind_listener(host: str, port: int) -> socket.socket:
    """Create a listening TCP socket bound to exactly ``host`` and verify the bound address."""
    category = classify_bind_address(host)
    if category not in {"LOOPBACK", "PRIVATE_OVERLAY"}:
        raise LaunchRefused(f"REMOTE_BIND_REFUSED:{category}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:  # "localhost" is loopback but not a literal IP
        raise LaunchRefused("REMOTE_BIND_REFUSED:BIND_ADDRESS_MUST_BE_LITERAL_IP") from exc
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((str(address), port))
        sock.set_inheritable(False)
        sock.listen(128)
        bound = ipaddress.ip_address(str(sock.getsockname()[0]).split("%", 1)[0])
        if bound != address:
            raise LaunchRefused("REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS")
    except BaseException:
        sock.close()
        raise
    return sock


def _default_app() -> Any:
    from pados.remote.app import create_remote_app

    return create_remote_app()


def _default_server(app: Any) -> Any:
    import uvicorn

    # No host/port/fd/uds here: the server only ever serves the socket passed to run().
    return uvicorn.Server(uvicorn.Config(app, workers=1, proxy_headers=False, server_header=False,
                                         log_level="warning"))


def serve(*, port: int = DEFAULT_PORT, app_factory: Callable[[], Any] = _default_app,
          server_factory: Callable[[Any], Any] = _default_server) -> int:
    from pados.config import get_settings
    from pados.remote.config import RemoteConfig

    settings = get_settings()
    if not settings.remote_config:
        raise LaunchRefused("PADOS_REMOTE_CONFIG_REQUIRED")
    config = RemoteConfig.load(settings.remote_config)
    check = check_remote_deployment(settings, remote_bind_host=config.bind_host)
    if not check.ok:
        raise LaunchRefused(",".join(check.violations))
    sock = bind_listener(config.bind_host, port)
    try:
        attest_listener(sock, bind_host=config.bind_host)
        server_factory(app_factory()).run(sockets=[sock])
    finally:
        sock.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pados.remote.serve")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        return serve(port=args.port)
    except LaunchRefused as exc:
        print(f"PADOS remote launch refused: {exc.code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
